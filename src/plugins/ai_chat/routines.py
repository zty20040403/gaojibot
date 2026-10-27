"""Owned cron schedules with transactional queueing and lease-fenced checkpoints."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

from src.bot_storage import DatabaseSource, StoreCursor, StoreRow, open_store_connection
from .conversation_scope import ConversationScope
from .delivery import DeliveryStore
from .message_ir import MessageBody, TextNode, body_from_json, body_to_json
from .quota import UsageStore
from .storage.jobs import DurableJob, DurableJobStore
from .ledger import MessageLedger
from .projects import ProjectAccess, ProjectStore

if TYPE_CHECKING:
    from .agent import ContextPacket
    from .agent.execution import EntryDecision
    from .subagents import SubAgentStore, TaskRecord
    from .turn_journal import TurnJournal, TurnStatus

ROUTINE_JOB_KIND = "agent.routine"


def cron_time(
    expression: str, timezone: str, timestamp: int, *, previous: bool = False
) -> int:
    expression = " ".join(expression.split())
    # Deliberately exclude seconds, shell commands and random/hashed extensions.
    if (
        len(expression) > 120
        or len(expression.split()) != 5
        or not re.fullmatch(r"[0-9*,/ \-]+", expression)
    ):
        raise ValueError("Use five numeric cron fields: minute hour day month weekday")
    try:
        zone = ZoneInfo(timezone)
        iterator = croniter(
            expression,
            datetime.fromtimestamp(timestamp, zone),
            max_years_between_matches=8,
        )
        result = (
            iterator.get_prev(datetime) if previous else iterator.get_next(datetime)
        )
    except (
        CroniterBadCronError,
        CroniterBadDateError,
        ZoneInfoNotFoundError,
        ValueError,
        OverflowError,
    ) as exc:
        raise ValueError(
            "Invalid cron/timezone or no occurrence within eight years"
        ) from exc
    return int(result.timestamp())


@dataclass(frozen=True)
class Routine:
    routine_id: int
    scope_key: str
    platform: str
    kind: str
    conversation_id: str
    bot_id: str
    creator_id: str
    name: str
    prompt: str
    cron: str
    timezone: str
    enabled: bool
    version: int
    max_tokens: int
    daily_tokens: int
    daily_runs: int
    next_due_at: int
    created_at: int
    updated_at: int
    project_id: int | None = None
    creator_principal_id: int | None = None
    routine_kind: str = "schedule"
    state: str = "active"
    spec: dict = field(default_factory=dict)
    dispatch: dict = field(default_factory=dict)
    consecutive_failures: int = 0

    @property
    def handle(self) -> str:
        return f"routine#{self.routine_id}"

    @property
    def scope(self) -> ConversationScope:
        return ConversationScope(
            self.platform,
            self.kind,
            self.conversation_id,
            actor_native_user_id=self.creator_id,
            bot_native_user_id=self.bot_id,
        )


@dataclass(frozen=True)
class RoutineRun:
    run_id: str
    routine_id: int
    version: int
    scheduled_at: int
    missed_from: int
    spec: Routine
    job_id: int | None
    turn_id: int | None
    status: str
    attempt_id: str
    body_json: str
    delivery_id: int | None
    last_error: str
    created_at: int
    updated_at: int
    deadline_at: int | None


class RoutineLeaseLost(RuntimeError):
    pass


class RoutineEffectUncertain(BaseException):
    """Stop the Agent, not just one tool: repeating an unknown effect is unsafe."""

class RoutineDeferred(BaseException):
    """Suspend the turn without allowing model/tool exception handlers to retry."""

    delay_seconds = 30


class RoutineChildPending(RoutineDeferred):
    """Suspend the entire parent turn until its durable child can continue."""

    delay_seconds = 15


class RoutineStore:
    def __init__(
        self,
        source: DatabaseSource,
        *,
        jobs: DurableJobStore,
        outbox: DeliveryStore,
        usage: UsageStore,
        max_per_owner: int = 5,
    ) -> None:
        _, self._connection = open_store_connection(source)
        self._source = source
        self._lock = threading.RLock()
        self.jobs, self.outbox, self.usage = jobs, outbox, usage
        self.max_per_owner = min(max(int(max_per_owner), 1), 20)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[StoreCursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _owner(scope: ConversationScope) -> tuple[str, str, str]:
        if (
            scope.platform != "onebot-v11"
            or any(
                not str(value).isdigit() or int(value) <= 0
                for value in (
                    scope.native_conversation_id,
                    scope.actor_native_user_id,
                    scope.bot_native_user_id,
                )
            )
            or (
                scope.kind == "private"
                and scope.native_conversation_id != scope.actor_native_user_id
            )
        ):
            raise ValueError("Routines require an explicit QQ scope, bot and creator")
        return scope.key, scope.bot_native_user_id, scope.actor_native_user_id

    def set(
        self,
        scope: ConversationScope,
        *,
        name: str,
        prompt: str,
        cron: str,
        timezone: str = "Asia/Shanghai",
        max_tokens: int = 100000,
        daily_tokens: int = 300000,
        daily_runs: int = 4,
        expected_version: int | None = None,
        now: int | None = None,
    ) -> Routine:
        owner = self._owner(scope)
        timestamp = int(time.time()) if now is None else now
        name, prompt, cron = name.strip(), prompt.strip(), " ".join(cron.split())
        if not 1 <= len(name) <= 64 or not 1 <= len(prompt) <= 4000:
            raise ValueError("Routine name must be 1-64 characters and prompt 1-4000")
        if (
            not 1024 <= max_tokens <= 200000
            or not max_tokens <= daily_tokens <= 2000000
            or not 1 <= daily_runs <= 24
        ):
            raise ValueError("Routine budget is outside the permitted bounds")
        due = cron_time(cron, timezone, timestamp)
        principal_id = self._creator_principal(scope, timestamp)
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?,0))",
                ("routine-scope:" + scope.key,),
            )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                ("routine-owner:" + ":".join(owner),),
            )
            existing = cursor.execute(
                """SELECT * FROM agent_routines
                WHERE scope_key=? AND bot_id=? AND creator_id=? AND name=? FOR UPDATE""",
                (*owner, name),
            ).fetchone()
            config = dict(
                prompt=prompt,
                cron=cron,
                timezone=timezone,
                max_tokens=max_tokens,
                daily_tokens=daily_tokens,
                daily_runs=daily_runs,
            )
            if existing is not None:
                if (
                    existing["routine_kind"] != "schedule"
                    or existing["state"] in {"pending_confirmation", "deleted"}
                    or existing["project_id"] is not None
                    or existing["spec"]
                    or existing["dispatch"]
                ):
                    raise ValueError(
                        "Use the confirmation/update flow for this routine"
                    )
                if existing["enabled"] and all(
                    existing[key] == value for key, value in config.items()
                ):
                    if (
                        expected_version is not None
                        and expected_version != existing["version"]
                    ):
                        raise ValueError(
                            "Routine version changed; list it again before editing"
                        )
                    return Routine(**dict(existing))
                if expected_version != existing["version"]:
                    raise ValueError(
                        "Routine version changed; list it again before editing"
                    )
            if existing is None or not existing["enabled"]:
                self._check_scope_limit(cursor, scope)
                count = cursor.execute(
                    "SELECT COUNT(*) AS n FROM agent_routines WHERE scope_key=? AND bot_id=? AND creator_id=? AND enabled",
                    owner,
                ).fetchone()["n"]
                if count >= self.max_per_owner:
                    raise ValueError(
                        "Too many enabled routines for this creator in this conversation"
                    )
            if existing is None:
                if expected_version not in (None, 0):
                    raise ValueError("Routine does not exist")
                row = cursor.execute(
                    """INSERT INTO agent_routines
                    (scope_key, platform, kind, conversation_id, bot_id, creator_id, name, prompt,
                     cron, timezone, enabled, version, max_tokens, daily_tokens, daily_runs, next_due_at, created_at, updated_at,
                     creator_principal_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, 1, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
                    (
                        scope.key,
                        scope.platform,
                        scope.kind,
                        scope.native_conversation_id,
                        scope.bot_native_user_id,
                        scope.actor_native_user_id,
                        name,
                        prompt,
                        cron,
                        timezone,
                        max_tokens,
                        daily_tokens,
                        daily_runs,
                        due,
                        timestamp,
                        timestamp,
                        principal_id,
                    ),
                ).fetchone()
            else:
                self._cancel_runs(cursor, existing["routine_id"], timestamp)
                row = cursor.execute(
                    """UPDATE agent_routines SET prompt=?, cron=?, timezone=?,
                    enabled=TRUE, state='active', version=version+1, max_tokens=?, daily_tokens=?, daily_runs=?,
                    next_due_at=?, updated_at=? WHERE routine_id=? RETURNING *""",
                    (
                        prompt,
                        cron,
                        timezone,
                        max_tokens,
                        daily_tokens,
                        daily_runs,
                        due,
                        timestamp,
                        existing["routine_id"],
                    ),
                ).fetchone()
            return Routine(**dict(row))

    def _creator_principal(self, scope, now):
        ledger = MessageLedger(self._source)
        try:
            return ledger.ensure_principal_identity(
                scope.platform, scope.actor_native_user_id, "", seen_at=now
            )
        finally:
            ledger.close()

    @staticmethod
    def _check_scope_limit(cursor, scope):
        count = cursor.execute(
            """SELECT count(*) FROM agent_routines WHERE scope_key=?
            AND state IN ('active','pending_confirmation')""",
            (scope.key,),
        ).fetchone()[0]
        if count >= (10 if scope.kind == "group" else 5):
            raise ValueError("Conversation routine limit reached")

    def create_pending(
        self,
        scope,
        *,
        name,
        prompt,
        cron="0 0 * * *",
        timezone="Asia/Shanghai",
        routine_kind="schedule",
        spec=None,
        dispatch=None,
        project_id=None,
        access: ProjectAccess | None = None,
        now=None,
    ):
        """Persist a draft only. M4 adds source validation, watch polling and UI."""
        owner = self._owner(scope)
        now = int(time.time()) if now is None else now
        name, prompt = name.strip(), prompt.strip()
        spec, dispatch = dict(spec or {}), dict(dispatch or {})
        if (
            not 1 <= len(name) <= 64
            or not 1 <= len(prompt) <= 4000
            or routine_kind not in {"schedule", "watch"}
        ):
            raise ValueError("Invalid routine draft")
        if len(json.dumps(spec)) > 16000 or len(json.dumps(dispatch)) > 16000:
            raise ValueError("Routine metadata exceeds its limit")
        due = cron_time(cron, timezone, now)
        principal = self._creator_principal(scope, now)
        with self._transaction() as cursor:
            # Scope lock also serializes drafts created by different group members.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?,0))",
                ("routine-scope:" + scope.key,),
            )
            self._check_scope_limit(cursor, scope)
            if project_id is not None:
                if (
                    access is None
                    or access.scope.key != scope.key
                    or access.principal_id != principal
                ):
                    raise PermissionError(
                        "Project access must match the routine creator"
                    )
                ProjectStore._get(cursor, access, project_id, edit=True)
            row = cursor.execute(
                """INSERT INTO agent_routines(scope_key,platform,kind,conversation_id,bot_id,creator_id,
                name,prompt,cron,timezone,enabled,version,max_tokens,daily_tokens,daily_runs,next_due_at,created_at,updated_at,
                creator_principal_id,project_id,routine_kind,state,spec,dispatch)
                VALUES (?,?,?,?,?,?,?,?,?,?,FALSE,1,100000,300000,?,?,?, ?,?,?,?,'pending_confirmation',?::jsonb,?::jsonb) RETURNING *""",
                (
                    scope.key,
                    scope.platform,
                    scope.kind,
                    scope.native_conversation_id,
                    owner[1],
                    owner[2],
                    name,
                    prompt,
                    cron,
                    timezone,
                    12 if routine_kind == "watch" else 24,
                    due,
                    now,
                    now,
                    principal,
                    project_id,
                    routine_kind,
                    json.dumps(spec),
                    json.dumps(dispatch),
                ),
            ).fetchone()
            if routine_kind == "watch":
                cursor.execute(
                    "INSERT INTO watch_state(routine_id) VALUES (?)",
                    (row["routine_id"],),
                )
            return Routine(**dict(row))

    def confirm(
        self,
        scope,
        routine_id,
        *,
        expected_version,
        access: ProjectAccess | None = None,
        now=None,
    ):
        owner = self._owner(scope)
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?,0))",
                ("routine-scope:" + scope.key,),
            )
            row = cursor.execute(
                """SELECT * FROM agent_routines WHERE scope_key=? AND bot_id=? AND creator_id=?
                AND routine_id=? FOR UPDATE""",
                (*owner, routine_id),
            ).fetchone()
            if row is None:
                raise PermissionError("Routine not owned in this conversation")
            if (
                row["version"] != expected_version
                or row["state"] != "pending_confirmation"
            ):
                raise ValueError("Draft changed or was already confirmed")
            if row["project_id"] is not None:
                if (
                    access is None
                    or access.scope.key != scope.key
                    or access.principal_id != row["creator_principal_id"]
                ):
                    raise PermissionError(
                        "Project access must match the routine creator"
                    )
                ProjectStore._get(cursor, access, row["project_id"], edit=True)
            result = cursor.execute(
                """UPDATE agent_routines SET state='active',enabled=TRUE,version=version+1,
                next_due_at=?,updated_at=? WHERE routine_id=? RETURNING *""",
                (cron_time(row["cron"], row["timezone"], now), now, routine_id),
            ).fetchone()
            return Routine(**dict(result))

    def list_owned(
        self, scope: ConversationScope, *, include_disabled: bool = True
    ) -> list[Routine]:
        owner = self._owner(scope)
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM agent_routines
                WHERE scope_key=? AND bot_id=? AND creator_id=?"""
                + ("" if include_disabled else " AND enabled")
                + " ORDER BY routine_id LIMIT 100",
                owner,
            ).fetchall()
        return [Routine(**dict(row)) for row in rows]

    def cancel(
        self,
        scope: ConversationScope,
        routine_id: int,
        *,
        expected_version: int,
        now: int | None = None,
    ) -> bool:
        owner = self._owner(scope)
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            row = cursor.execute(
                """SELECT * FROM agent_routines WHERE scope_key=? AND bot_id=? AND creator_id=?
                AND routine_id=? FOR UPDATE""",
                (*owner, routine_id),
            ).fetchone()
            if row is None:
                return False
            if row["version"] != expected_version:
                raise ValueError(
                    "Routine version changed; list it again before cancelling"
                )
            if not row["enabled"] and row["state"] != "pending_confirmation":
                return False
            cursor.execute(
                """UPDATE agent_routines SET enabled=FALSE,
                state=CASE WHEN state='pending_confirmation' THEN 'deleted' ELSE 'paused' END,
                version=version+1, updated_at=? WHERE routine_id=?""",
                (timestamp, routine_id),
            )
            self._cancel_runs(cursor, routine_id, timestamp)
            return True

    @staticmethod
    def _cancel_runs(cursor: StoreCursor, routine_id: int, timestamp: int) -> None:
        rows = cursor.execute(
            """SELECT * FROM routine_runs WHERE routine_id=?
            AND status IN ('queued','running','generated','enqueued') FOR UPDATE""",
            (routine_id,),
        ).fetchall()
        for row in rows:
            cursor.execute(
                """UPDATE durable_jobs SET status='cancelled', lease_owner='', lease_until=NULL,
                updated_at=?, finished_at=? WHERE job_id=? AND status IN ('pending','running','failed')""",
                (timestamp, timestamp, row["job_id"]),
            )
            if row["delivery_id"] is not None:
                # A message already being sent cannot be revoked by cancelling its schedule.
                cursor.execute(
                    """UPDATE deliveries SET status='cancelled', lease_until=NULL, updated_at=?
                    WHERE delivery_id=? AND status IN ('pending','failed')""",
                    (timestamp, row["delivery_id"]),
                )
            if row["status"] != "enqueued":
                cursor.execute(
                    "UPDATE routine_runs SET status='cancelled', updated_at=? WHERE run_id=?",
                    (timestamp, row["run_id"]),
                )

    def materialize_due(
        self, *, now: int | None = None, limit: int = 50
    ) -> list[RoutineRun]:
        timestamp = int(time.time()) if now is None else now
        result = []
        with self._transaction() as cursor:
            rows = cursor.execute(
                """SELECT * FROM agent_routines WHERE enabled AND state='active' AND routine_kind='schedule' AND next_due_at<=?
                ORDER BY next_due_at, routine_id LIMIT ? FOR UPDATE SKIP LOCKED""",
                (timestamp, min(max(limit, 1), 100)),
            ).fetchall()
            for row in rows:
                spec = Routine(**dict(row))
                due = max(
                    spec.next_due_at,
                    cron_time(spec.cron, spec.timezone, timestamp + 1, previous=True),
                )
                next_due = cron_time(spec.cron, spec.timezone, timestamp)
                cursor.execute(
                    "UPDATE agent_routines SET next_due_at=?, updated_at=? WHERE routine_id=?",
                    (next_due, timestamp, spec.routine_id),
                )
                active = cursor.execute(
                    "SELECT 1 FROM routine_runs WHERE routine_id=? AND status IN ('queued','running','generated')",
                    (spec.routine_id,),
                ).fetchone()
                if active:
                    continue
                run_id = uuid.uuid4().hex
                inserted = cursor.execute(
                    """INSERT INTO routine_runs
                    (run_id, routine_id, version, scheduled_at, missed_from, spec_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    ON CONFLICT(routine_id, version, scheduled_at) DO NOTHING""",
                    (
                        run_id,
                        spec.routine_id,
                        spec.version,
                        due,
                        spec.next_due_at,
                        json.dumps(asdict(spec), ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
                if inserted.rowcount != 1:
                    continue
                job, _ = self.jobs.enqueue(
                    kind=ROUTINE_JOB_KIND,
                    idempotency_key=f"routine-run:{run_id}",
                    scope_key=spec.scope_key,
                    payload={"run_id": run_id},
                    max_attempts=3,
                    resume_on_lease_loss=True,
                    now=timestamp,
                    transaction_cursor=cursor,
                )
                cursor.execute(
                    "UPDATE routine_runs SET job_id=? WHERE run_id=?",
                    (job.job_id, run_id),
                )
                result.append(
                    self._run(
                        cursor.execute(
                            "SELECT * FROM routine_runs WHERE run_id=?", (run_id,)
                        ).fetchone()
                    )
                )
        return result

    def get_run(self, run_id: str) -> RoutineRun | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM routine_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run(row) if row is not None else None

    def _fenced(
        self, cursor: StoreCursor, job: DurableJob, timestamp: int,
        *, allow_terminal: bool = False,
    ) -> RoutineRun:
        run_id = str(job.payload.get("run_id") or "")
        row = cursor.execute(
            "SELECT * FROM routine_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or job.kind != ROUTINE_JOB_KIND or row["job_id"] != job.job_id:
            raise RoutineLeaseLost("Routine job envelope does not match its run")
        routine = cursor.execute(
            "SELECT * FROM agent_routines WHERE routine_id=? FOR UPDATE",
            (row["routine_id"],),
        ).fetchone()
        leased = cursor.execute(
            """SELECT job_id FROM durable_jobs WHERE job_id=? AND status='running'
            AND lease_owner=? AND attempts=? AND lease_until>? FOR UPDATE""",
            (job.job_id, job.lease_owner, job.attempts, timestamp),
        ).fetchone()
        row = cursor.execute(
            "SELECT * FROM routine_runs WHERE run_id=? FOR UPDATE", (run_id,)
        ).fetchone()
        if (
            leased is None
            or not routine["enabled"]
            or row["version"] != routine["version"]
            or routine["scope_key"] != job.scope_key
            or row["status"] == "cancelled"
            or (not allow_terminal and row["status"] in {"failed", "skipped"})
        ):
            raise RoutineLeaseLost(
                "Routine is disabled, superseded or no longer leased"
            )
        return self._run(row)

    def execution_run(self, job: DurableJob) -> RoutineRun:
        """Validate ownership before selecting execution or terminal recovery."""
        with self._transaction() as cursor:
            return self._fenced(cursor, job, int(time.time()), allow_terminal=True)

    def prepare_child(
        self, job: DurableJob, attempt_id: str, *, subagents: SubAgentStore,
        intent: dict, packet: ContextPacket, decision: EntryDecision | None,
        dispatch: dict, max_parallelism: int, max_steps: int,
    ) -> TaskRecord:
        """Save ownership and all restart inputs in the same database commit."""
        encoded = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if not intent or len(encoded.encode()) > 256000:
            raise ValueError("Routine child intent is empty or too large")
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        timestamp = int(time.time())
        created = False
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, timestamp)
            if (run.status != "running" or run.attempt_id != attempt_id
                    or run.deadline_at is None or run.deadline_at <= timestamp):
                raise RoutineLeaseLost("Routine attempt is no longer executing")
            if (packet.scope_key != run.spec.scope_key or str(packet.requester_user_id) != run.spec.creator_id
                    or str(dispatch.get("bot_id", "")) != run.spec.bot_id):
                raise PermissionError("Routine child must belong to its original conversation, creator and bot")
            existing = cursor.execute("SELECT * FROM routine_children WHERE run_id=? AND fingerprint=?",
                (run.run_id, fingerprint)).fetchone()
            if existing is not None:
                if existing["intent_json"] != encoded:
                    raise ValueError("Routine child intent fingerprint collision")
                row = cursor.execute("SELECT * FROM subagent_tasks WHERE task_id=?", (existing["task_id"],)).fetchone()
                task = subagents._task_row(row)
                if task.scope_key != run.spec.scope_key or str(task.requester_user_id) != run.spec.creator_id:
                    raise PermissionError("Saved routine child ownership changed")
            else:
                task = subagents.prepare_task(
                    packet=packet, decision=decision, dispatch={**dispatch, "deadline": run.deadline_at},
                    max_parallelism=max_parallelism, max_steps=max_steps, transaction_cursor=cursor,
                )
                cursor.execute("""INSERT INTO routine_children (run_id,fingerprint,task_id,intent_json,created_at)
                    VALUES (?,?,?,?,?)""", (run.run_id, fingerprint, task.task_id, encoded, timestamp))
                created = True
        if created:
            subagents._notify_changed(task.task_id)
        return task

    def child_receipts(self, job: DurableJob) -> list[dict]:
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, int(time.time()))
            rows = cursor.execute("""SELECT c.task_id,c.intent_json,t.objective,t.status,t.result_json,t.last_error
                FROM routine_children c JOIN subagent_tasks t ON t.task_id=c.task_id
                WHERE c.run_id=? ORDER BY c.created_at,c.task_id""", (run.run_id,)).fetchall()
        return [{"task_id": row["task_id"], "intent": json.loads(row["intent_json"]),
                 "objective": row["objective"], "status": row["status"],
                 "result": json.loads(row["result_json"]), "error": row["last_error"]} for row in rows]

    def begin_attempt(
        self, job: DurableJob, *, timeout_seconds: int = 600, now: int | None = None
    ) -> RoutineRun:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ValueError("Routine execution timeout must be between 1 and 3600 seconds")
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, timestamp)
            if run.status in {"generated", "enqueued"}:
                return run
            if run.deadline_at is not None and run.deadline_at <= timestamp:
                cursor.execute(
                    """UPDATE routine_runs SET status='failed', last_error='routine deadline expired',
                    updated_at=? WHERE run_id=?""", (timestamp, run.run_id)
                )
                return self._run(cursor.execute(
                    "SELECT * FROM routine_runs WHERE run_id=?", (run.run_id,)
                ).fetchone())
            attempt_id = uuid.uuid4().hex
            reserved = self.usage.reserve_routine(
                run_id=run.run_id,
                attempt_id=attempt_id,
                now=timestamp,
                transaction_cursor=cursor,
            )
            cursor.execute(
                """UPDATE routine_runs SET status=?, attempt_id=?, last_error=?, updated_at=?,
                deadline_at=COALESCE(deadline_at,?)
                WHERE run_id=?""",
                (
                    "running" if reserved else "skipped",
                    attempt_id if reserved else "",
                    "" if reserved else "routine daily budget exhausted",
                    timestamp,
                    timestamp + timeout_seconds,
                    run.run_id,
                ),
            )
            return self._run(
                cursor.execute(
                    "SELECT * FROM routine_runs WHERE run_id=?", (run.run_id,)
                ).fetchone()
            )

    def owns_attempt(
        self, job: DurableJob, attempt_id: str, *, now: int | None = None
    ) -> bool:
        timestamp = int(time.time()) if now is None else now
        with self._lock:
            row = self._connection.execute(
                """SELECT 1 FROM routine_runs r JOIN agent_routines a USING(routine_id)
                JOIN durable_jobs j ON j.job_id=r.job_id WHERE r.run_id=? AND r.attempt_id=? AND r.status='running'
                AND a.enabled AND a.version=r.version AND a.scope_key=? AND j.job_id=?
                AND j.status='running' AND j.lease_owner=? AND j.attempts=? AND j.lease_until>?""",
                (
                    str(job.payload.get("run_id") or ""),
                    attempt_id,
                    job.scope_key,
                    job.job_id,
                    job.lease_owner,
                    job.attempts,
                    timestamp,
                ),
            ).fetchone()
        return row is not None

    def save_result(
        self,
        job: DurableJob,
        attempt_id: str,
        body: MessageBody,
        *,
        turn_id: int | None = None,
        now: int | None = None,
        transaction_cursor: StoreCursor | None = None,
    ) -> RoutineRun:
        encoded = body_to_json(body)
        if not body.nodes or len(encoded.encode("utf-8")) > 2_000_000:
            raise ValueError(
                "Routine result is empty or exceeds the durable message limit"
            )
        timestamp = int(time.time()) if now is None else now
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self._transaction()) as cursor:
            run = self._fenced(cursor, job, timestamp)
            if run.attempt_id != attempt_id or run.status not in {
                "running",
                "generated",
            }:
                raise RoutineLeaseLost("Routine attempt changed")
            if run.status == "generated":
                if run.body_json != encoded or run.turn_id != turn_id:
                    raise ValueError("A generated routine result is immutable")
                return run
            cursor.execute(
                "UPDATE routine_runs SET status='generated', body_json=?, turn_id=?, updated_at=? WHERE run_id=?",
                (encoded, turn_id, timestamp, run.run_id),
            )
            return self._run(
                cursor.execute(
                    "SELECT * FROM routine_runs WHERE run_id=?", (run.run_id,)
                ).fetchone()
            )

    def complete_execution(
        self,
        job: DurableJob,
        attempt_id: str,
        *,
        journal: TurnJournal,
        turn_id: int | None,
        status: TurnStatus,
        body: MessageBody | None,
        final_text: str = "",
        trace_payload: dict | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> RoutineRun:
        """Commit execution evidence with its output, before any delivery attempt."""
        if status not in {"succeeded", "silence", "aborted"}:
            raise ValueError("Routine completion must be succeeded, silence or aborted")
        if (body is None) != (status == "silence"):
            raise ValueError("Only a quiet completion can omit its output")
        if turn_id is None and status != "aborted":
            raise ValueError("Successful routine execution requires a journal turn")
        timestamp = int(time.time())
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, timestamp)
            if run.attempt_id != attempt_id or run.status != "running":
                raise RoutineLeaseLost("Routine attempt changed")
            if turn_id is not None:
                turn = cursor.execute(
                    "SELECT scope_key,status FROM agent_turns WHERE turn_id=? FOR UPDATE",
                    (turn_id,),
                ).fetchone()
                if run.turn_id != turn_id or turn is None or turn["scope_key"] != run.spec.scope_key:
                    raise ValueError("Routine completion must use its bound conversation turn")
                if turn["status"] != "running":
                    raise RoutineLeaseLost("Routine journal turn is already terminal")
            if body is None:
                cursor.execute(
                    "UPDATE routine_runs SET status='quiet',updated_at=? WHERE run_id=?",
                    (timestamp, run.run_id),
                )
            else:
                self.save_result(job, attempt_id, body, turn_id=turn_id,
                                 now=timestamp, transaction_cursor=cursor)
            if turn_id is not None:
                journal.finish_turn(
                    turn_id, status=status, final_text=final_text,
                    trace_payload=trace_payload, input_tokens=input_tokens,
                    output_tokens=output_tokens, total_tokens=input_tokens + output_tokens,
                    finished_at=timestamp, transaction_cursor=cursor,
                )
            return self._run(cursor.execute(
                "SELECT * FROM routine_runs WHERE run_id=?", (run.run_id,)
            ).fetchone())

    def bind_turn(self, job: DurableJob, attempt_id: str, turn_id: int) -> None:
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, int(time.time()))
            if run.attempt_id != attempt_id or run.status != "running":
                raise RoutineLeaseLost("Routine attempt changed")
            turn = cursor.execute(
                "SELECT scope_key FROM agent_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if turn is None or turn["scope_key"] != run.spec.scope_key:
                raise ValueError("Routine turn must belong to its conversation")
            cursor.execute(
                "UPDATE routine_runs SET turn_id=? WHERE run_id=?",
                (turn_id, run.run_id),
            )

    def claim_effect(
        self,
        job: DurableJob,
        attempt_id: str,
        name: str,
        arguments: dict,
        *,
        now: int | None = None,
    ) -> tuple[str, str | None]:
        """Claim one non-repeatable tool intent, or return its saved receipt.

        Repeated identical side effects within one scheduled occurrence reuse the
        receipt, including across attempts. Read/idempotent tools do not use this.
        """
        timestamp = int(time.time()) if now is None else now
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode()) > 256000:
            raise ValueError("Routine tool arguments exceed the durable limit")
        key = hashlib.sha256((name + "\n" + encoded).encode()).hexdigest()
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, timestamp)
            if run.attempt_id != attempt_id or run.status != "running":
                raise RoutineLeaseLost("Routine attempt changed")
            existing = cursor.execute(
                "SELECT * FROM routine_effects WHERE run_id=? AND fingerprint=?",
                (run.run_id, key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["tool_name"] != name
                    or existing["arguments_json"] != encoded
                ):
                    raise ValueError("Routine effect fingerprint collision")
                if existing["state"] == "completed":
                    return key, str(existing["result"])
                raise RoutineEffectUncertain(
                    f"{name}: previous execution has no durable receipt"
                )
            cursor.execute(
                """INSERT INTO routine_effects
                (run_id,fingerprint,attempt_id,tool_name,arguments_json,state,started_at)
                VALUES (?,?,?,?,?,'started',?)""",
                (run.run_id, key, attempt_id, name, encoded, timestamp),
            )
        return key, None

    def finish_effect(
        self,
        job: DurableJob,
        attempt_id: str,
        key: str,
        result: str,
        *,
        now: int | None = None,
    ) -> None:
        if not isinstance(result, str) or len(result.encode()) > 2_000_000:
            raise ValueError("Routine tool result exceeds the durable limit")
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, timestamp)
            if run.attempt_id != attempt_id or run.status != "running":
                raise RoutineLeaseLost("Routine attempt changed")
            row = cursor.execute(
                "SELECT * FROM routine_effects WHERE run_id=? AND fingerprint=? AND attempt_id=?",
                (run.run_id, key, attempt_id),
            ).fetchone()
            if row is None:
                raise RoutineLeaseLost("Routine effect belongs to another attempt")
            if row["state"] == "completed":
                if row["result"] != result:
                    raise ValueError("Routine effect receipt is immutable")
                return
            cursor.execute(
                """UPDATE routine_effects SET state='completed', result=?, finished_at=?
                WHERE run_id=? AND fingerprint=?""",
                (result, timestamp, run.run_id, key),
            )

    def recovery_receipts(self, job: DurableJob) -> list[dict]:
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, int(time.time()))
            rows = cursor.execute(
                "SELECT tool_name,arguments_json,state,result FROM routine_effects WHERE run_id=? ORDER BY started_at,fingerprint",
                (run.run_id,),
            ).fetchall()
        unknown = [str(row["tool_name"]) for row in rows if row["state"] != "completed"]
        if unknown:
            raise RoutineEffectUncertain(
                "Missing execution receipts: " + ", ".join(unknown)
            )
        return [
            dict(
                tool=row["tool_name"],
                arguments=json.loads(row["arguments_json"]),
                result=row["result"],
            )
            for row in rows
        ]

    def queue_result(self, job: DurableJob, *, now: int | None = None) -> int:
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            run = self._fenced(cursor, job, timestamp)
            if run.status == "enqueued":
                return run.delivery_id
            if run.status != "generated":
                raise ValueError("Routine has no generated result to deliver")
            delivery, _ = self.outbox.enqueue(
                idempotency_key=f"routine-final:{run.run_id}",
                source_scope_key=run.spec.scope_key,
                target_scope=run.spec.scope,
                body=body_from_json(run.body_json),
                turn_id=run.turn_id,
                now=timestamp,
                transaction_cursor=cursor,
            )
            cursor.execute(
                "UPDATE routine_runs SET status='enqueued', delivery_id=?, updated_at=? WHERE run_id=?",
                (delivery.delivery_id, timestamp, run.run_id),
            )
            return delivery.delivery_id

    def reconcile_terminal_jobs(
        self, *, now: int | None = None, limit: int = 50
    ) -> int:
        """Unblock failed schedules and recover already generated results.

        A worker can stop between settling a job and settling its run. Hold the
        same routine/job/run lock order as normal execution and cancellation.
        """
        timestamp = int(time.time()) if now is None else now
        reconciled = 0
        with self._transaction() as cursor:
            routines = cursor.execute(
                """SELECT a.* FROM agent_routines a WHERE EXISTS (
                       SELECT 1 FROM routine_runs r JOIN durable_jobs j ON j.job_id=r.job_id
                       WHERE r.routine_id=a.routine_id AND r.status IN ('queued','running','generated')
                         AND j.status IN ('failed','cancelled','succeeded'))
                   ORDER BY a.routine_id LIMIT ? FOR UPDATE OF a SKIP LOCKED""",
                (min(max(limit, 1), 100),),
            ).fetchall()
            for spec in routines:
                run = cursor.execute(
                    """SELECT * FROM routine_runs WHERE routine_id=?
                       AND status IN ('queued','running','generated')""",
                    (spec["routine_id"],),
                ).fetchone()
                if run is None:
                    continue
                job = cursor.execute(
                    "SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE",
                    (run["job_id"],),
                ).fetchone()
                if job is None or job["status"] not in {
                    "failed",
                    "cancelled",
                    "succeeded",
                }:
                    continue
                cursor.execute(
                    "SELECT run_id FROM routine_runs WHERE run_id=? FOR UPDATE",
                    (run["run_id"],),
                )
                cancelled = (
                    job["status"] == "cancelled"
                    or not spec["enabled"]
                    or spec["version"] != run["version"]
                )
                if run["status"] == "generated" and not cancelled:
                    cursor.execute(
                        """UPDATE durable_jobs SET status='pending',attempts=0,next_attempt_at=?,
                           lease_owner='',lease_until=NULL,finished_at=NULL,updated_at=?
                           WHERE job_id=?""",
                        (timestamp, timestamp, run["job_id"]),
                    )
                else:
                    error = (
                        "routine cancelled"
                        if cancelled
                        else (
                            job["last_error"] or "job ended without a persisted result"
                        )
                    )
                    cursor.execute(
                        "UPDATE routine_runs SET status=?,last_error=?,updated_at=? WHERE run_id=?",
                        (
                            "cancelled" if cancelled else "failed",
                            error[:2000],
                            timestamp,
                            run["run_id"],
                        ),
                    )
                reconciled += 1
        return reconciled

    def queue_terminal_notices(self, *, now: int | None = None, limit: int = 50) -> int:
        """Do not lose failure/budget notices between job settlement and polling."""
        timestamp = int(time.time()) if now is None else now
        count = 0
        with self._transaction() as cursor:
            owners = cursor.execute(
                """SELECT a.routine_id FROM agent_routines a WHERE a.enabled AND EXISTS (
                    SELECT 1 FROM routine_runs r WHERE r.routine_id=a.routine_id
                    AND r.version=a.version AND r.status IN ('failed','skipped') AND r.delivery_id IS NULL)
                ORDER BY a.routine_id LIMIT ? FOR UPDATE OF a SKIP LOCKED""",
                (min(max(limit, 1), 100),),
            ).fetchall()
            for owner in owners:
                rows = cursor.execute(
                    """SELECT r.* FROM routine_runs r JOIN agent_routines a USING(routine_id)
                    WHERE r.routine_id=? AND r.version=a.version AND r.status IN ('failed','skipped')
                    AND r.delivery_id IS NULL ORDER BY r.scheduled_at LIMIT ? FOR UPDATE OF r""",
                    (owner["routine_id"], min(max(limit - count, 1), 100)),
                ).fetchall()
                for row in rows:
                    run = self._run(row)
                    if run.status == "skipped":
                        day = datetime.fromtimestamp(
                            timestamp, ZoneInfo("Asia/Shanghai")
                        ).replace(hour=0, minute=0, second=0, microsecond=0)
                        previous = cursor.execute(
                            """SELECT delivery_id FROM routine_runs WHERE routine_id=? AND status='skipped'
                            AND delivery_id IS NOT NULL AND updated_at>=? AND updated_at<?
                            ORDER BY updated_at,run_id LIMIT 1""",
                            (
                                run.routine_id,
                                int(day.timestamp()),
                                int((day + timedelta(days=1)).timestamp()),
                            ),
                        ).fetchone()
                        if previous is not None:
                            cursor.execute(
                                "UPDATE routine_runs SET delivery_id=? WHERE run_id=?",
                                (previous["delivery_id"], run.run_id),
                            )
                            continue
                    reason = (
                        "今日运行次数或 Token 预算已用完，本次未执行。"
                        if run.status == "skipped"
                        else "本次执行的总时限已到，恢复后没有重新执行。已提交的远程操作请核对回执。"
                        if run.last_error == "routine deadline expired"
                        else "本次执行失败，未确认全部完成。请在控制台查看任务和工具回执。"
                    )
                    body = MessageBody(
                        (
                            TextNode(
                                0, f"{run.spec.handle}「{run.spec.name}」：{reason}"
                            ),
                        )
                    )
                    delivery, _ = self.outbox.enqueue(
                        idempotency_key=f"routine-final:{run.run_id}",
                        source_scope_key=run.spec.scope_key,
                        target_scope=run.spec.scope,
                        body=body,
                        turn_id=run.turn_id,
                        now=timestamp,
                        transaction_cursor=cursor,
                    )
                    cursor.execute(
                        "UPDATE routine_runs SET delivery_id=?,updated_at=? WHERE run_id=?",
                        (delivery.delivery_id, timestamp, run.run_id),
                    )
                    count += 1
                if count >= limit:
                    break
        return count

    @staticmethod
    def _run(row: StoreRow) -> RoutineRun:
        values = dict(row)
        values["spec"] = Routine(**json.loads(values.pop("spec_json")))
        return RoutineRun(**values)
