"""Read persisted OneBot provenance before a managed runtime adopts old work.

No account is inferred from an account-less history key. These records are
observations, not issued authority. SourceJournal remains the sole issuer.
Unknown work and missing original absolute deadlines require separate review.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re

from ..conversation_scope import ConversationScope
from ..platform_types import ActorKey, EndpointKey
from .onebot_migration_runtime import delivery_digest, job_digest
from .qq_official.migration_types import digest


@dataclass(frozen=True, repr=False)
class DomainSource:
    bot_native_id: str
    scope: ConversationScope
    actor_native_id: str | None
    kind: str
    ref: str
    revision: int
    fingerprint: str
    deadline: int | None
    state: str
    endpoint_kind: str
    endpoint_native_id: str
    task_id: int | None = None
    routine_id: int | None = None
    job_id: int | None = None
    delivery_id: int | None = None


@dataclass(frozen=True)
class InventorySummary:
    scanned: int = 0
    adopted: int = 0
    existing: int = 0
    unknown: int = 0
    terminal: int = 0
    missing_deadline: int = 0
    unattributed: int = 0
    other_account: int = 0
    unavailable: int = 0
    truncated: bool = False


def _object(value):
    if isinstance(value, str):
        if len(value.encode()) > 262144:
            raise ValueError("source_metadata_too_large")
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("source_metadata_invalid")
    return value


def _native(value):
    if type(value) not in {str, int} or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError("onebot_native_identity_missing")
    return str(value)


def _positive(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("source_integer_invalid")
    return value


def _deadline(value):
    return None if value is None else _positive(value)


def _scope(value, bot):
    scope = ConversationScope.from_key(value)
    if scope.platform != "onebot-v11" or (scope.account is not None and scope.account.app_id != bot):
        raise ValueError("source_scope_mismatch")
    return scope


def _event(value, bot):
    value = _object(value)
    if _native(value.get("self_id")) != bot:
        raise ValueError("source_bot_mismatch")
    actor = _native(value.get("user_id"))
    kind = value.get("message_type")
    if kind not in {"group", "private"}:
        raise ValueError("source_endpoint_missing")
    target = _native(value.get("group_id")) if kind == "group" else actor
    return actor, kind, target


def _project(cursor, identifier, scope, bot):
    if identifier is None:
        return
    row = cursor.execute("SELECT scope_key,bot_native_user_id FROM projects WHERE project_id=?", (identifier,)).fetchone()
    if row is None or row["scope_key"] != scope.key or _native(row["bot_native_user_id"]) != bot:
        raise ValueError("source_project_mismatch")


def _task(cursor, task_id, revision):
    task_id, revision = _positive(task_id), _positive(revision)
    task = cursor.execute("SELECT * FROM subagent_tasks WHERE task_id=?", (task_id,)).fetchone()
    control = cursor.execute("SELECT * FROM subagent_controls WHERE task_id=?", (task_id,)).fetchone()
    if task is None or control is None or task["account_key"] is not None or control["revision"] != revision:
        return None
    dispatch = _object(control["dispatch_json"])
    bot = _native(dispatch.get("bot_id"))
    scope = _scope(task["scope_key"], bot)
    owner = _native(task["requester_user_id"])
    original_actor, kind, target = _event(dispatch.get("event"), bot)
    if original_actor != owner:
        raise ValueError("source_task_owner_mismatch")
    execution = dispatch.get("execution")
    actor = owner
    if execution is not None:
        execution = _object(execution)
        actor, execution_kind, execution_target = _event(execution.get("event"), bot)
        if (execution.get("revision") != revision or _native(execution.get("actor_user_id")) != actor
                or (execution_kind, execution_target) != (kind, target)):
            raise ValueError("source_revision_mismatch")
        principal = cursor.execute("""SELECT principal_id FROM principal_identities
            WHERE platform='onebot-v11' AND native_user_id=? AND account_key=''
                AND identity_kind='' AND identity_namespace=''""", (actor,)).fetchone()
        if principal is None or principal["principal_id"] != execution.get("actor_principal_id"):
            raise ValueError("source_revision_identity_missing")
        if actor != owner:
            if kind != "group" or cursor.execute("SELECT 1 FROM routine_children WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("source_collaboration_unavailable")
            grant = cursor.execute("""SELECT grant_version FROM subagent_collaborators
                WHERE task_id=? AND principal_id=? AND enabled""", (task_id, principal["principal_id"])).fetchone()
            if grant is None or grant["grant_version"] != execution.get("grant_version"):
                raise ValueError("source_collaboration_revoked")
    elif revision != 1:
        # A later revision without its execution envelope has no current actor
        # evidence. Historical checkpoints alone do not grant fresh execution.
        raise ValueError("source_revision_identity_missing")
    _project(cursor, task["project_id"], scope, bot)
    runs = cursor.execute("SELECT status,attempt FROM subagent_runs WHERE task_id=?", (task_id,)).fetchall()
    uncertain = any(row["attempt"] > 0 or row["status"] != "pending" for row in runs)
    uncertain = uncertain or bool(cursor.execute("""SELECT 1 FROM subagent_events WHERE task_id=?
        AND event_type='agent.tool_started' LIMIT 1""", (task_id,)).fetchone())
    uncertain = uncertain or bool(cursor.execute("SELECT 1 FROM subagent_external_calls WHERE task_id=? LIMIT 1", (task_id,)).fetchone())
    state = ("terminal" if task["cancel_requested"] or task["status"] in {"completed", "partial", "failed", "cancelled"}
             else "safe" if task["status"] in {"received", "queued"} and not uncertain else "unknown")
    fingerprint = digest(["onebot-subagent-v1", task_id, task["trace_id"], scope.key, task["conversation_id"],
        owner, task["trigger_message_id"], task["objective"], task["project_id"], task["project_base_version"],
        revision, _object(control["policy_json"]), dispatch])
    return DomainSource(bot, scope, actor, "subagent", str(task_id), revision, fingerprint,
        _deadline(dispatch.get("deadline")), state, kind, target, task_id=task_id)


def resolve_task(cursor, task_id, revision):
    """Current revision only; malformed or unproven namespaces return None."""
    try:
        return _task(cursor, task_id, revision)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def _routine(cursor, routine_id, revision):
    routine_id, revision = _positive(routine_id), _positive(revision)
    row = cursor.execute("SELECT * FROM agent_routines WHERE routine_id=?", (routine_id,)).fetchone()
    if row is None or row["platform"] != "onebot-v11" or row["account_key"] is not None or row["version"] != revision:
        return None
    bot, actor = _native(row["bot_id"]), _native(row["creator_id"])
    scope = _scope(row["scope_key"], bot)
    kind, target = row["kind"], _native(row["conversation_id"])
    if kind not in {"group", "private"} or (kind == "private" and target != actor):
        raise ValueError("source_routine_identity_mismatch")
    _project(cursor, row["project_id"], scope, bot)
    dispatch = _object(row["dispatch"])
    if dispatch.get("event") is not None and _event(dispatch["event"], bot) != (actor, kind, target):
        raise ValueError("source_routine_event_mismatch")
    fields = ("routine_id", "scope_key", "platform", "kind", "conversation_id", "bot_id", "creator_id", "name", "prompt",
              "cron", "timezone", "version", "max_tokens", "daily_tokens", "daily_runs", "project_id", "routine_kind", "spec", "dispatch")
    fingerprint = digest(["onebot-routine-v1", {key: row[key] for key in fields}])
    state = "safe" if row["enabled"] and row["state"] == "active" else "terminal"
    return DomainSource(bot, scope, actor, "routine", str(routine_id), revision, fingerprint,
        _deadline(dispatch.get("deadline")), state, kind, target, routine_id=routine_id)


def resolve_routine(cursor, routine_id, revision):
    try:
        return _routine(cursor, routine_id, revision)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def _run_source(cursor, run):
    source = resolve_routine(cursor, run["routine_id"], run["version"])
    if source is None:
        return None
    spec = _object(run["spec_json"])
    if (spec.get("routine_id"), spec.get("version"), spec.get("bot_id"), spec.get("creator_id"), spec.get("scope_key"),
        spec.get("kind"), spec.get("conversation_id")) != (run["routine_id"], run["version"], source.bot_native_id,
        source.actor_native_id, source.scope.key, source.endpoint_kind, source.endpoint_native_id):
        raise ValueError("source_frozen_routine_mismatch")
    deadline = _deadline(run["deadline_at"])
    if source.deadline is not None:
        deadline = min(source.deadline, deadline) if deadline is not None else source.deadline
    state = source.state if run["status"] == "queued" and not run["attempt_id"] else "unknown"
    if run["status"] in {"succeeded", "failed", "cancelled", "expired", "enqueued", "generated"}:
        state = "terminal"
    return replace(source, deadline=deadline, state=state)


def resolve_job(cursor, job_id):
    """Only existing built-in job contracts, with exact frozen parent evidence."""
    try:
        row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=?", (_positive(job_id),)).fetchone()
        if row is None:
            return None
        payload = _object(row["payload_json"])
        if row["kind"] == "subagent.workflow" and set(payload) == {"task_id", "revision"}:
            source = resolve_task(cursor, payload["task_id"], payload["revision"])
        elif row["kind"] == "agent.routine" and set(payload) == {"run_id"}:
            run = cursor.execute("SELECT * FROM routine_runs WHERE run_id=? AND job_id=?", (payload["run_id"], job_id)).fetchone()
            source = _run_source(cursor, run) if run is not None else None
        else:
            return None
        if source is None or row["scope_key"] != source.scope.key:
            return None
        state = source.state
        if row["status"] != "pending" or row["attempts"] != 0:
            state = "terminal" if row["status"] in {"succeeded", "failed", "cancelled"} else "unknown"
        return replace(source, kind="job", ref=str(job_id), revision=1, fingerprint=job_digest(row),
                       state=state, task_id=None, routine_id=None, job_id=job_id)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def resolve_delivery(cursor, delivery_id):
    """Legacy target IDs alone never identify which bot may send a delivery."""
    try:
        row = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=?", (_positive(delivery_id),)).fetchone()
        if row is None or row["account_key"] is not None or row["target_platform"] != "onebot-v11":
            return None
        runs = cursor.execute("SELECT * FROM routine_runs WHERE delivery_id=?", (delivery_id,)).fetchall()
        match = re.fullmatch(r"subagent-final:([1-9][0-9]*):([1-9][0-9]*)", row["idempotency_key"])
        if len(runs) == 1 and match is None:
            source = _run_source(cursor, runs[0])
        elif not runs and match:
            source = resolve_task(cursor, int(match[1]), int(match[2]))
            task = cursor.execute("""SELECT t.trigger_message_id,t.status,c.final_queued_revision
                FROM subagent_tasks t JOIN subagent_controls c ON c.task_id=t.task_id
                WHERE t.task_id=?""", (int(match[1]),)).fetchone()
            if (task is None or task["final_queued_revision"] != int(match[2])
                    or task["status"] not in {"completed", "partial", "failed", "cancelled"}
                    or row["source_canonical_message_id"] != task["trigger_message_id"]):
                return None
        else:
            return None
        if source is None or (row["source_scope_key"], row["target_kind"], row["target_native_conversation_id"]) != (
                source.scope.key, source.endpoint_kind, source.endpoint_native_id):
            return None
        attempted = cursor.execute("SELECT 1 FROM delivery_attempts WHERE delivery_id=? LIMIT 1", (delivery_id,)).fetchone()
        state = ("terminal" if row["status"] in {"committed", "sent", "accepted", "cancelled", "expired", "failed"}
                 else "safe" if row["status"] == "pending" and row["attempts"] == 0 and not attempted else "unknown")
        return replace(source, kind="delivery", ref=str(delivery_id), revision=1, fingerprint=delivery_digest(row),
                       state=state, task_id=None, routine_id=None, delivery_id=delivery_id)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


class OneBotSourceInventory:
    """Bounded initial DB inventory, never an approval or readiness certificate."""
    def __init__(self, *, limit=5000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("invalid_inventory_limit")
        self.limit = limit

    def __call__(self, cursor, baseline, sources):
        counts = {field: 0 for field in InventorySummary.__dataclass_fields__ if field != "truncated"}
        truncated = False
        queries = (
            ("SELECT t.task_id,c.revision FROM subagent_tasks t JOIN subagent_controls c ON c.task_id=t.task_id WHERE t.account_key IS NULL ORDER BY t.task_id LIMIT ?", resolve_task),
            ("SELECT routine_id,version FROM agent_routines WHERE account_key IS NULL ORDER BY routine_id LIMIT ?", resolve_routine),
            ("SELECT job_id FROM durable_jobs WHERE kind IN ('subagent.workflow','agent.routine') ORDER BY job_id LIMIT ?", resolve_job),
            ("SELECT delivery_id FROM deliveries WHERE account_key IS NULL AND target_platform='onebot-v11' ORDER BY delivery_id LIMIT ?", resolve_delivery),
        )
        for sql, resolver in queries:
            rows = cursor.execute(sql, (self.limit + 1,)).fetchall()
            truncated = truncated or len(rows) > self.limit
            for row in rows[:self.limit]:
                counts["scanned"] += 1
                value = resolver(cursor, *tuple(row.values()))
                if value is None:
                    counts["unattributed"] += 1
                    continue
                if value.bot_native_id != baseline.account.app_id:
                    counts["other_account"] += 1
                    continue
                if value.state != "safe":
                    counts[value.state] += 1
                    continue
                if value.deadline is None:
                    counts["missing_deadline"] += 1
                    continue
                if value.deadline <= sources._now():
                    counts["terminal"] += 1
                    continue
                endpoint = EndpointKey(baseline.account, value.endpoint_kind, value.endpoint_native_id)
                actor = ActorKey(baseline.account, "user", "", value.actor_native_id)
                existing = cursor.execute("""SELECT * FROM migration_work_sources
                    WHERE account_key=? AND source_kind=? AND source_ref=? AND source_revision=?""",
                    (baseline.account.key, value.kind, value.ref, value.revision)).fetchone()
                if existing is not None:
                    exact = (existing["source_digest"], existing["endpoint_key"], existing["canonical_scope_key"],
                             existing["actor_key"], existing["job_id"], existing["delivery_id"]) == (
                        value.fingerprint, endpoint.key, value.scope.key, actor.key, value.job_id, value.delivery_id)
                    counts["existing" if exact else "unavailable"] += 1
                    continue
                # Restart inventory never assigns a later epoch to a source
                # created before an existing migration route.
                if sources.routes._route(cursor, value.scope.key) is not None:
                    counts["unavailable"] += 1
                    continue
                parent_id = None
                if value.kind == "job":
                    jobrow = cursor.execute("SELECT kind,payload_json FROM durable_jobs WHERE job_id=?", (value.job_id,)).fetchone()
                    payload = _object(jobrow["payload_json"])
                    if jobrow["kind"] == "subagent.workflow":
                        parent_kind, parent_ref, parent_revision = "subagent", str(payload["task_id"]), payload["revision"]
                    else:
                        run = cursor.execute("SELECT routine_id,version FROM routine_runs WHERE run_id=?", (payload["run_id"],)).fetchone()
                        parent_kind, parent_ref, parent_revision = "routine", str(run["routine_id"]), run["version"]
                    parent = cursor.execute("""SELECT source_id,deadline_at FROM migration_work_sources
                        WHERE account_key=? AND source_kind=? AND source_ref=? AND source_revision=?""",
                        (baseline.account.key, parent_kind, parent_ref, parent_revision)).fetchone()
                    if parent is None:
                        counts["unavailable"] += 1
                        continue
                    parent_id = parent["source_id"]
                    value = replace(value, deadline=min(value.deadline, parent["deadline_at"]))
                sources.create(cursor, baseline=baseline, endpoint=endpoint, scope=value.scope, actor=actor,
                    kind=value.kind, ref=value.ref, revision=value.revision, fingerprint=value.fingerprint,
                    deadline=value.deadline, job_id=value.job_id, delivery_id=value.delivery_id, inventory=True,
                    parent_source_id=parent_id, task_id=value.task_id, routine_id=value.routine_id)
                counts["adopted"] += 1
        return InventorySummary(**counts, truncated=truncated)
