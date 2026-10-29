"""OneBot managed-process baseline and durable source authority.

This owns no queue and holds no database transaction over an external await.
Host composition supplies domain inventories and exact nonstandard API targets;
unknown sources/targets are rejected, never upgraded to the current route epoch.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import secrets
import time

import psycopg

from ..agent.control import RouteAuthorityLost
from ..conversation_scope import ConversationScope
from ..platform_types import AccountKey, ActorKey, EndpointKey
from .onebot_route import PhysicalCall, TransportObservation
from .onebot_route_context import issue_source, require_source, source_context
from .qq_official.migration_routes import physical_scope
from .qq_official.migration_types import BaselineEvidence, MigrationDenied, digest


class DedicatedBaseline:
    """Actual session lock; evidence is available only while this object owns it."""
    def __init__(self, dsn, account: AccountKey, *, deployment_digest: str, clock=time.time):
        if account.platform != "onebot-v11" or len(deployment_digest) != 64:
            raise ValueError("invalid_onebot_baseline")
        self.dsn, self.account, self.clock = dsn, account, clock
        self.deployment_digest = deployment_digest
        self.runtime_id = secrets.token_hex(16)
        self.key = int.from_bytes(hashlib.sha256(("onebot-migration:" + account.key).encode()).digest()[:8], "big", signed=True)
        self.connection = None
        self.pid = None

    def acquire(self):
        if self.connection is not None:
            raise MigrationDenied("baseline_already_started")
        conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (self.key,)).fetchone()[0]:
                conn.close()
                return False
            self.pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            self.connection = conn
            return True
        except BaseException:
            conn.close()
            raise

    def evidence(self, cursor, source, *, account, now):
        if source is not self or account != self.account or self.connection is None or self.connection.closed:
            raise MigrationDenied("baseline_unavailable")
        # RouteService also verifies the actual session lock using pg_locks.
        return BaselineEvidence(account.key, self.runtime_id, self.pid, self.key,
                                self.deployment_digest, now + 120)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def job_digest(row):
    return digest([row["job_id"], row["kind"], row["idempotency_key"], row["scope_key"], row["payload_json"], row["created_at"]])


def delivery_digest(row):
    return digest([row[key] for key in ("delivery_id", "idempotency_key", "source_scope_key", "source_canonical_message_id",
        "turn_id", "target_platform", "target_kind", "target_native_conversation_id", "reply_to_native_message_id", "body_json", "created_at")])


def reminder_digest(row):
    return digest([row[key] for key in ("reminder_id", "scope_key", "platform", "conversation_kind", "native_conversation_id",
                                       "creator_native_user_id", "creator_principal_id", "message", "scheduled_for", "created_at")])


class SourceJournal:
    def __init__(self, routes, transaction, *, clock=time.time):
        self.routes, self.transaction, self.clock = routes, transaction, clock
        self.domain_current = None
        self.terminal_current = None

    def _now(self):
        return int(self.clock())

    def _fresh(self, cursor, endpoint, scope):
        self.routes.lock(cursor, endpoints=(endpoint,), scopes=(scope.key, physical_scope(endpoint).key))
        route = self.routes._route(cursor, scope.key) or self.routes._route(cursor, physical_scope(endpoint).key)
        epoch = route["epoch"] if route else 0
        self.routes.assert_active_route(cursor, canonical_scope=scope, endpoint=endpoint,
                                        route_epoch=epoch, operation="execute")
        return epoch

    def create(self, cursor, *, baseline, endpoint, scope, actor, kind, ref, revision,
               fingerprint, deadline, job_id=None, delivery_id=None, inventory=False,
               parent_source_id=None, task_id=None, routine_id=None):
        now = self._now()
        if (endpoint.account != baseline.account or endpoint.account.platform != "onebot-v11"
                or (actor is not None and not actor.matches_endpoint(endpoint))
                or kind not in {"handler", "job", "delivery", "reminder", "approval", "maintenance", "subagent", "routine"}
                or not isinstance(ref, str) or not 1 <= len(ref.encode()) <= 128
                or type(revision) is not int or revision <= 0
                or not isinstance(fingerprint, str) or len(fingerprint) != 64
                or type(deadline) is not int or not now < deadline < 2**63):
            raise MigrationDenied("invalid_work_source")
        epoch = self._fresh(cursor, endpoint, scope)
        if parent_source_id is not None:
            parent = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=? FOR SHARE",
                                    (parent_source_id,)).fetchone()
            if (parent is None or parent["state"] not in {"ready", "executing", "completed"}
                    or parent["deadline_at"] < deadline
                    or (parent["account_key"], parent["endpoint_key"], parent["canonical_scope_key"], parent["actor_key"], parent["route_epoch"]) != (
                        endpoint.account.key, endpoint.key, scope.key, actor.key if actor else None, epoch)):
                raise MigrationDenied("source_parent_mismatch")
        current = self.routes._baseline(cursor, endpoint.account.key, now)
        if current["runtime_id"] != baseline.runtime_id:
            raise MigrationDenied("baseline_changed")
        if inventory and self.routes._route(cursor, scope.key) is not None:
            raise MigrationDenied("inventory_after_migration")
        if kind == "job":
            row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (job_id,)).fetchone()
            if (row is None or row["status"] != "pending" or row["attempts"] != 0
                    or job_digest(row) != fingerprint):
                raise MigrationDenied("job_inventory_not_unstarted")
        identifier = secrets.token_hex(16)
        extra_columns = ",parent_source_id,task_id,routine_id" if (parent_source_id or task_id or routine_id) else ""
        extra_placeholders = ",?,?,?" if extra_columns else ""
        extra_values = (parent_source_id, task_id, routine_id) if extra_columns else ()
        cursor.execute("""INSERT INTO migration_work_sources(source_id,account_key,endpoint_key,canonical_scope_key,
            actor_key,source_kind,source_ref,source_revision,source_digest,route_epoch,baseline_runtime_id,
            job_id,delivery_id,state,deadline_at,created_at,updated_at""" + extra_columns + ")\n"
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?,?" + extra_placeholders + ") ON CONFLICT DO NOTHING",
            (identifier, endpoint.account.key, endpoint.key, scope.key, actor.key if actor else None,
             kind, ref, revision, fingerprint, epoch, baseline.runtime_id, job_id, delivery_id, deadline, now, now, *extra_values))
        row = cursor.execute("""SELECT * FROM migration_work_sources WHERE account_key=? AND source_kind=?
            AND source_ref=? AND source_revision=?""", (endpoint.account.key, kind, ref, revision)).fetchone()
        if row is None or (row["source_digest"], row["endpoint_key"], row["canonical_scope_key"], row["actor_key"],
                            row["job_id"], row["delivery_id"], row["route_epoch"]) != (
                            fingerprint, endpoint.key, scope.key, actor.key if actor else None, job_id, delivery_id, epoch):
            raise MigrationDenied("work_source_conflict")
        return row["source_id"]

    def claim(self, cursor, source_id, *, baseline, job=None):
        row = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            raise MigrationDenied("work_source_missing")
        endpoint, scope = EndpointKey.parse(row["endpoint_key"]), ConversationScope.from_key(row["canonical_scope_key"])
        self.routes.lock(cursor, endpoints=(endpoint,), scopes=(scope.key,))
        self.routes.assert_active_route(cursor, canonical_scope=scope, endpoint=endpoint,
                                        route_epoch=row["route_epoch"], operation="execute")
        current = self.routes._baseline(cursor, endpoint.account.key, self._now())
        if current["runtime_id"] != baseline.runtime_id or endpoint.account != baseline.account:
            raise MigrationDenied("baseline_changed")
        row = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=? FOR UPDATE", (source_id,)).fetchone()
        result_digest = None
        persisted_result = row["job_id"] is not None and cursor.execute("""SELECT 1 FROM routine_runs
            WHERE job_id=? AND status IN ('generated','enqueued')""", (row["job_id"],)).fetchone()
        if (row["state"] in {"ready", "completed"} and job is not None
                and (row["state"] == "completed" or persisted_result) and not self._unresolved(cursor, row)):
            result_digest = self._routine_result(cursor, row)[1]
        if persisted_result and result_digest is None:
            raise MigrationDenied("result_source_not_ready")
        if (row["state"] != "ready" and result_digest is None) or row["deadline_at"] <= self._now():
            raise MigrationDenied("work_source_not_ready")
        if row["job_id"] is not None:
            self._job(cursor, row, job)
        elif job is not None:
            raise MigrationDenied("work_source_job_mismatch")
        generation = job.lease_generation if job else None
        owner = job.lease_owner if job else None
        if row["source_kind"] in {"delivery", "reminder"}:
            generation = self._outgoing(cursor, row)["attempts"]
            owner = baseline.runtime_id
        cursor.execute("""UPDATE migration_work_sources SET state='executing',active_runtime_id=?,
            worker_owner=?,worker_generation=?,updated_at=? WHERE source_id=?""",
            (baseline.runtime_id, owner, generation, self._now(), source_id))
        return issue_source(self, source_id=source_id, endpoint=endpoint, scope=scope,
                            actor=ActorKey.parse(row["actor_key"]) if row["actor_key"] else None,
                            route_epoch=row["route_epoch"], runtime_id=baseline.runtime_id, deadline_at=row["deadline_at"],
                            worker_owner=owner, worker_generation=generation, result_digest=result_digest)

    @staticmethod
    def _unresolved(cursor, row):
        return cursor.execute("""SELECT 1 FROM migration_dispatch_activities WHERE source_id=?
            AND state IN ('dispatched','unknown') LIMIT 1""", (row["source_id"],)).fetchone() is not None

    def _routine_result(self, cursor, row):
        """Only the original routine job's durable output, never another turn."""
        actual = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        parent = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=?",
                                (row.get("parent_source_id"),)).fetchone()
        if actual is None or actual["kind"] != "agent.routine" or parent is None or parent["source_kind"] != "routine":
            raise MigrationDenied("result_source_unavailable")
        run = cursor.execute("SELECT * FROM routine_runs WHERE job_id=?", (row["job_id"],)).fetchone()
        if (run is None or run["status"] not in {"generated", "enqueued"} or not run["body_json"]
                or run["routine_id"] != parent["routine_id"] or run["version"] != parent["source_revision"]
                or json.loads(actual["payload_json"]).get("run_id") != run["run_id"]):
            raise MigrationDenied("result_source_changed")
        return run, digest([run[key] for key in ("run_id", "routine_id", "version", "job_id", "spec_json",
                                                "scheduled_at", "attempt_id", "body_json", "turn_id")])

    def _job(self, cursor, row, job=None):
        actual = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE", (row["job_id"],)).fetchone()
        owner = job.lease_owner if job else row["worker_owner"]
        generation = job.lease_generation if job else row["worker_generation"]
        if (actual is None or actual["status"] != "running" or actual["lease_owner"] != owner
                or actual["lease_generation"] != generation or actual["lease_until"] <= self._now()
                or job_digest(actual) != row["source_digest"] or (job is not None and job.job_id != row["job_id"])):
            raise MigrationDenied("work_source_job_lost")
        if row.get("parent_source_id"):
            parent = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=?", (row["parent_source_id"],)).fetchone()
            if parent is None or self.domain_current is None:
                raise MigrationDenied("source_parent_unavailable")
            self.domain_current(cursor, parent)

    def _outgoing(self, cursor, row):
        if row["source_kind"] == "delivery":
            actual = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=? FOR UPDATE", (row["delivery_id"],)).fetchone()
            fingerprint = delivery_digest(actual) if actual else None
        else:
            actual = cursor.execute("SELECT * FROM reminders WHERE reminder_id=? FOR UPDATE", (int(row["source_ref"]),)).fetchone()
            fingerprint = reminder_digest(actual) if actual else None
        if (actual is None or actual["status"] != "sending" or actual["lease_until"] <= self._now()
                or fingerprint != row["source_digest"]
                or (row["worker_generation"] is not None and actual["attempts"] != row["worker_generation"])):
            raise MigrationDenied("outgoing_source_changed")
        if row['source_kind']=='delivery' and row.get('parent_source_id'):
            parent=cursor.execute('SELECT * FROM migration_work_sources WHERE source_id=?',
                                  (row['parent_source_id'],)).fetchone()
            if parent is not None and parent['source_kind']=='subagent':
                if self.terminal_current is None:
                    raise MigrationDenied('terminal_result_source_unavailable')
                self.terminal_current(cursor,row,parent,actual)
        return actual

    def require(self, cursor, proof, *, operation="execute", **expected):
        require_source(proof, self)
        self.routes.assert_active_route(cursor, canonical_scope=proof.scope, endpoint=proof.endpoint,
                                        route_epoch=proof.route_epoch, operation=operation)
        baseline = self.routes._baseline(cursor, proof.endpoint.account.key, self._now())
        row = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=? FOR UPDATE", (proof.source_id,)).fetchone()
        if (row is None or row["state"] != "executing" or row["deadline_at"] <= self._now()
                or baseline["runtime_id"] != proof.runtime_id or row["active_runtime_id"] != proof.runtime_id
                or (row["worker_owner"], row["worker_generation"]) != (proof.worker_owner, proof.worker_generation)
                or (row["endpoint_key"], row["canonical_scope_key"], row["route_epoch"], row["deadline_at"], row["actor_key"]) != (
                    proof.endpoint.key, proof.scope.key, proof.route_epoch, proof.deadline_at, proof.actor.key if proof.actor else None)):
            raise MigrationDenied("work_source_stale")
        if expected.get("source_endpoint", proof.endpoint) != proof.endpoint or expected.get("source_scope", proof.scope) != proof.scope:
            raise MigrationDenied("work_source_mismatch")
        if row["job_id"] is not None:
            self._job(cursor, row)
        if row["source_kind"] in {"delivery", "reminder"}:
            self._outgoing(cursor, row)
        if proof.result_digest is not None:
            if self._routine_result(cursor, row)[1] != proof.result_digest:
                raise MigrationDenied("result_source_changed")
        return row

    def current(self, proof):
        with self.transaction() as cursor:
            self.require(cursor, proof)
        return True

    def finish(self, proof, *, uncertain=False, deferred=False):
        require_source(proof, self)
        # Historical closure only; this never authorizes another effect.
        with self.transaction() as cursor:
            state = "needs_attention" if uncertain else "completed"
            if deferred:
                state = "needs_attention"
                try:
                    row = self.require(cursor, proof)
                except (MigrationDenied, PermissionError):
                    pass
                else:
                    if row["job_id"] is not None and not self._unresolved(cursor, row):
                        state = "completed" if proof.result_digest else "ready"
            cursor.execute("""UPDATE migration_work_sources SET state=?,updated_at=?
                WHERE source_id=? AND active_runtime_id=? AND state='executing'
                AND worker_owner IS NOT DISTINCT FROM ? AND worker_generation IS NOT DISTINCT FROM ?""",
                (state, self._now(), proof.source_id, proof.runtime_id, proof.worker_owner, proof.worker_generation))

    def begin_effect(self, cursor, proof, *, scope, domain, reference):
        require_source(proof, self)
        if (proof.result_digest is not None or proof.scope.key != scope.key or domain not in {"project.lifecycle", "project.io"}
                or not isinstance(reference, str) or not 1 <= len(reference) <= 96):
            raise MigrationDenied("project_source_mismatch")
        self.require(cursor, proof)
        permit = self.routes.begin_dispatch(cursor, source_endpoint=proof.endpoint, source_scope=proof.scope,
            physical_endpoint=proof.endpoint, route_epoch=proof.route_epoch, target_route_epoch=proof.route_epoch,
            operation_ref=domain+":"+reference, operation="execute", source=proof, now=self._now())
        cursor.execute("UPDATE migration_dispatch_activities SET source_id=? WHERE activity_id=?", (proof.source_id, permit.activity_id))
        return permit

    def effect_receipt(self, cursor, source, *, activity, now):
        """Historical domain receipt verifier; never accepts a model result dict."""
        from .qq_official.migration_types import ExecutionReceiptEvidence
        if not isinstance(source, tuple) or len(source) != 2:
            raise MigrationDenied("project_receipt_source_required")
        domain, reference = source
        if activity["operation_ref"] != domain+":"+reference:
            raise MigrationDenied("project_receipt_source_mismatch")
        bound = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=?", (activity["source_id"],)).fetchone()
        if bound is None or bound["account_key"] != activity["source_account_key"]:
            raise MigrationDenied("project_receipt_source_mismatch")
        if domain == "project.lifecycle":
            try:
                job_id, step_index = (int(value) for value in reference.split(":"))
            except (ValueError, TypeError):
                raise MigrationDenied("project_receipt_source_mismatch") from None
            row = cursor.execute("""SELECT s.*,p.scope_key,p.bot_native_user_id FROM project_lifecycle_steps s
                JOIN project_lifecycle_runs r ON r.job_id=s.job_id JOIN projects p ON p.project_id=r.project_id
                WHERE s.job_id=? AND s.step_index=?""", (job_id, step_index)).fetchone()
            if (row is None or bound["job_id"] != job_id or row["status"] != "succeeded"
                    or not isinstance(row["evidence"], dict) or row["evidence"].get("verified") is not True):
                raise MigrationDenied("project_receipt_unverified")
            value = [row["job_id"], row["step_index"], row["request_id"], row["dispatch_lease"], row["arguments"], row["evidence"]]
        elif domain == "project.io":
            if not reference.isdecimal():
                raise MigrationDenied("project_receipt_source_mismatch")
            row = cursor.execute("""SELECT i.*,p.scope_key,p.bot_native_user_id FROM project_io i
                JOIN projects p USING(project_id) WHERE operation_id=?""", (int(reference),)).fetchone()
            if row is None or row["state"] != "completed" or row["result"] is None or row["evidence"] is None:
                raise MigrationDenied("project_receipt_unverified")
            actor = ActorKey.parse(bound["actor_key"]) if bound["actor_key"] else None
            if actor is None or cursor.execute("""SELECT 1 FROM principal_identities WHERE principal_id=?
                AND platform='onebot-v11' AND native_user_id=?""", (row["actor_principal_id"], actor.native_user_id)).fetchone() is None:
                raise MigrationDenied("project_receipt_actor_mismatch")
            value = [row["operation_id"], row["token"], row["arguments_sha256"], row["result"], row["evidence"]]
        else:
            raise MigrationDenied("project_receipt_domain_required")
        account = AccountKey.parse(bound["account_key"])
        if row["scope_key"] != bound["canonical_scope_key"] or row["bot_native_user_id"] != account.app_id:
            raise MigrationDenied("project_receipt_namespace_mismatch")
        return ExecutionReceiptEvidence(domain, reference, digest(value), "accepted")


# Only host bootstrap metadata can execute without an application source.
_BOOTSTRAP_READS = frozenset({"get_login_info", "get_status", "get_version_info"})
_SEND_GROUP = frozenset({"send_group_msg", "send_group_forward_msg", "upload_group_file"})
_SEND_PRIVATE = frozenset({"send_private_msg", "send_private_forward_msg", "upload_private_file"})
_READ_GROUP = frozenset({"get_group_info", "get_group_member_info", "get_group_member_list",
                         "get_group_file_system_info", "get_group_root_files", "get_group_files_by_folder",
                         "get_group_file_url"})


def _native(value):
    if type(value) not in {int, str} or not str(value).isdecimal() or int(value) <= 0:
        raise MigrationDenied("physical_target_required")
    return str(value)


class OneBotMigrationRuntime:
    """Host-installed boundary; missing inventories/target resolvers deny effects."""
    def __init__(self, *, sources: SourceJournal, baselines, canonical_scope,
                 target_resolver=None, inventory=None, database_calls=asyncio.to_thread):
        self.sources, self.routes = sources, sources.routes
        self.baselines = dict(baselines)
        self.canonical_scope, self.target_resolver = canonical_scope, target_resolver
        self.database_calls = database_calls
        self.inventory = inventory
        self.sources.domain_current = self._domain_current
        from .onebot_results import require_terminal_delivery
        self.sources.terminal_current = lambda cursor,row,parent,delivery: require_terminal_delivery(self,cursor,row,parent,delivery)
        self._renewal = None

    def _resolve_domain(self, cursor, kind, identifier, revision):
        from . import onebot_source_inventory as inventory
        resolver = inventory.resolve_task if kind == "subagent" else inventory.resolve_routine
        value = resolver(cursor, identifier, revision)
        if value is None:
            raise MigrationDenied("domain_source_unavailable")
        return value

    def _domain_current(self, cursor, row):
        if row["source_kind"] not in {"subagent", "routine"}:
            if row["state"] not in {"executing", "completed"}:
                raise MigrationDenied("source_parent_unavailable")
            return
        value = self._resolve_domain(cursor, row["source_kind"], int(row["source_ref"]), row["source_revision"])
        account = AccountKey.parse(row["account_key"])
        endpoint = EndpointKey.parse(row["endpoint_key"])
        if (row["state"] not in {"ready", "completed"} or row["deadline_at"] <= self.sources._now()
                or value.bot_native_id != account.app_id or value.scope.key != row["canonical_scope_key"]
                or (value.endpoint_kind, value.endpoint_native_id) != (endpoint.kind, endpoint.native_conversation_id)
                or value.fingerprint != row["source_digest"]
                or ActorKey.parse(row["actor_key"]).native_user_id != value.actor_native_id):
            raise MigrationDenied("domain_source_changed")
        if row["source_kind"] == "subagent":
            task = cursor.execute("SELECT status,cancel_requested FROM subagent_tasks WHERE task_id=?", (int(row["source_ref"]),)).fetchone()
            if task is None or task["cancel_requested"] or task["status"] in {"failed", "cancelled"}:
                raise MigrationDenied("domain_source_cancelled")
        elif value.state != "safe":
            raise MigrationDenied("domain_source_cancelled")

    def guard_due_routines(self, cursor, *, now, limit):
        candidates = cursor.execute("""SELECT routine_id,version FROM agent_routines WHERE platform='onebot-v11'
            AND enabled AND state='active' AND routine_kind='schedule' AND next_due_at<=?
            ORDER BY next_due_at,routine_id LIMIT ?""", (now, limit)).fetchall()
        parents = []
        for candidate in candidates:
            parent = cursor.execute("""SELECT * FROM migration_work_sources WHERE source_kind='routine'
                AND routine_id=? AND source_revision=?""", (candidate["routine_id"], candidate["version"])).fetchone()
            if parent is not None:
                parents.append(parent)
        self.routes.lock(cursor, endpoints=tuple(EndpointKey.parse(row["endpoint_key"]) for row in parents),
                         scopes=tuple(row["canonical_scope_key"] for row in parents))
        allowed = []
        for parent in parents:
            try:
                self._domain_current(cursor, parent)
                self.routes.assert_active_route(cursor, canonical_scope=parent["canonical_scope_key"],
                    endpoint=EndpointKey.parse(parent["endpoint_key"]), route_epoch=parent["route_epoch"], operation="execute")
            except (MigrationDenied, PermissionError):
                continue
            allowed.append(parent["routine_id"])
        return tuple(allowed)

    def capture_domain(self, cursor, kind, identifier, revision, now):
        if kind == "subagent":
            row = cursor.execute("SELECT account_key FROM subagent_tasks WHERE task_id=?", (identifier,)).fetchone()
            if row is not None and row["account_key"] is not None:
                return  # Official task authority has its own explicit source union.
        value = self._resolve_domain(cursor, kind, identifier, revision)
        from .onebot_route_context import active_source_route
        parent = active_source_route.get()
        require_source(parent, self.sources)
        if parent.result_digest is not None:
            raise MigrationDenied("result_source_cannot_execute")
        self.sources.require(cursor, parent)
        if (value.bot_native_id != parent.endpoint.account.app_id or value.scope.key != parent.scope.key
                or (value.endpoint_kind, value.endpoint_native_id) != (parent.endpoint.kind, parent.endpoint.native_conversation_id)
                or parent.actor is None or value.actor_native_id != parent.actor.native_user_id):
            raise MigrationDenied("domain_source_identity_mismatch")
        deadline = min(parent.deadline_at, value.deadline) if value.deadline else parent.deadline_at
        baseline = self.baselines[parent.endpoint.account.key]
        return self.sources.create(cursor, baseline=baseline, endpoint=parent.endpoint, scope=parent.scope,
            actor=parent.actor, kind=kind, ref=str(identifier), revision=revision, fingerprint=value.fingerprint,
            deadline=deadline, parent_source_id=parent.source_id,
            task_id=identifier if kind == "subagent" else None, routine_id=identifier if kind == "routine" else None)

    def capture_created_job(self, cursor, job, *, scope):
        from .onebot_route_context import active_source_route
        parent = active_source_route.get()
        require_source(parent, self.sources)
        if parent.result_digest is not None:
            raise MigrationDenied("result_source_cannot_execute")
        self.sources.require(cursor, parent)
        if parent.scope.key != scope.key or job.scope_key != scope.key:
            raise MigrationDenied("source_job_scope_mismatch")
        row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=?", (job.job_id,)).fetchone()
        if row is None:
            raise MigrationDenied("source_job_missing")
        return self.sources.create(cursor, baseline=self.baselines[parent.endpoint.account.key], endpoint=parent.endpoint,
            scope=parent.scope, actor=parent.actor, kind="job", ref=str(job.job_id), revision=1,
            fingerprint=job_digest(row), deadline=parent.deadline_at, job_id=job.job_id, parent_source_id=parent.source_id)

    def capture_delivery(self, cursor, delivery, now):
        from .onebot_route_context import active_source_route
        parent = active_source_route.get()
        if parent is None:
            from .onebot_results import capture_terminal_delivery
            return capture_terminal_delivery(self,cursor,delivery,now)
        require_source(parent, self.sources)
        self.sources.require(cursor, parent)
        row = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery.delivery_id,)).fetchone()
        if row is None or row["account_key"] is not None or row["source_scope_key"] != parent.scope.key:
            raise MigrationDenied("delivery_source_mismatch")
        if parent.result_digest is not None:
            source_row = cursor.execute("SELECT * FROM migration_work_sources WHERE source_id=?", (parent.source_id,)).fetchone()
            result, _ = self.sources._routine_result(cursor, source_row)
            if (row["idempotency_key"] != f"routine-final:{result['run_id']}"
                    or row["body_json"] != result["body_json"] or row["turn_id"] != result["turn_id"]
                    or row["source_canonical_message_id"] is not None or row["reply_to_native_message_id"]
                    or (row["target_platform"], row["target_kind"], row["target_native_conversation_id"]) != (
                        parent.endpoint.account.platform, parent.endpoint.kind, parent.endpoint.native_conversation_id)):
                raise MigrationDenied("result_delivery_mismatch")
        return self.sources.create(cursor, baseline=self.baselines[parent.endpoint.account.key], endpoint=parent.endpoint,
            scope=parent.scope, actor=parent.actor, kind="delivery", ref=str(delivery.delivery_id), revision=1,
            fingerprint=delivery_digest(row), deadline=parent.deadline_at,
            delivery_id=delivery.delivery_id, parent_source_id=parent.source_id)

    def capture_reminder(self, cursor, reminder, now):
        from .onebot_route_context import active_source_route
        parent = active_source_route.get()
        require_source(parent, self.sources)
        if parent.result_digest is not None:
            raise MigrationDenied("result_source_cannot_execute")
        self.sources.require(cursor, parent)
        row = cursor.execute("SELECT * FROM reminders WHERE reminder_id=?", (reminder.reminder_id,)).fetchone()
        if (row is None or row["scope_key"] != parent.scope.key or parent.actor is None
                or row["creator_native_user_id"] != parent.actor.native_user_id
                or row["scheduled_for"] >= parent.deadline_at):
            raise MigrationDenied("reminder_source_mismatch_or_expired")
        return self.sources.create(cursor, baseline=self.baselines[parent.endpoint.account.key], endpoint=parent.endpoint,
            scope=parent.scope, actor=parent.actor, kind="reminder", ref=str(reminder.reminder_id), revision=1,
            fingerprint=reminder_digest(row), deadline=parent.deadline_at, parent_source_id=parent.source_id)

    def outgoing_account(self, kind, identifier):
        with self.sources.transaction() as cursor:
            rows = cursor.execute("SELECT account_key FROM migration_work_sources WHERE source_kind=? AND source_ref=?",
                                  (kind, str(identifier))).fetchall()
        if len(rows) != 1:
            raise MigrationDenied("outgoing_source_missing")
        return AccountKey.parse(rows[0]["account_key"])

    @asynccontextmanager
    async def outgoing_context(self, kind, identifier, bot):
        def claim():
            with self.sources.transaction() as cursor:
                row = cursor.execute("SELECT * FROM migration_work_sources WHERE source_kind=? AND source_ref=?",
                                     (kind, str(identifier))).fetchone()
                if row is None:
                    raise MigrationDenied("outgoing_source_missing")
                baseline = self.baselines.get(row["account_key"])
                if baseline is None or baseline.account.app_id != str(bot.self_id):
                    raise MigrationDenied("outgoing_account_mismatch")
                return self.sources.claim(cursor, row["source_id"], baseline=baseline)
        try:
            proof = await self.database_calls(claim)
        except Exception:
            raise RouteAuthorityLost("outgoing_source_unavailable") from None
        uncertain = True
        try:
            with source_context(proof, lambda: self.sources.current(proof)):
                yield proof
            uncertain = False
        finally:
            await self.database_calls(self.sources.finish, proof, uncertain=uncertain)

    def derive_job(self, cursor, job, *, kind, identifier, revision, now):
        parent = cursor.execute("""SELECT * FROM migration_work_sources WHERE source_kind=? AND source_ref=?
            AND source_revision=?""", (kind, str(identifier), revision)).fetchone()
        if parent is None:
            raise MigrationDenied("domain_source_not_recorded")
        self._domain_current(cursor, parent)
        endpoint = EndpointKey.parse(parent["endpoint_key"])
        baseline = self.baselines.get(endpoint.account.key)
        if baseline is None:
            raise MigrationDenied("onebot_baseline_required")
        row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=?", (job.job_id,)).fetchone()
        if row is None:
            raise MigrationDenied("source_job_missing")
        return self.sources.create(cursor, baseline=baseline, endpoint=endpoint,
            scope=ConversationScope.from_key(parent["canonical_scope_key"]), actor=ActorKey.parse(parent["actor_key"]),
            kind="job", ref=str(job.job_id), revision=1, fingerprint=job_digest(row), deadline=parent["deadline_at"],
            job_id=job.job_id, parent_source_id=parent["source_id"])

    def _register(self, baseline, *, initial=False):
        with self.sources.transaction() as cursor:
            self.routes.register_baseline(cursor, account=baseline.account, source=baseline, now=self.sources._now())
            if initial:
                if self.inventory is None:
                    raise MigrationDenied("source_inventory_unconfigured")
                # Inventory validates original domain rows under these same
                # locks. Incomplete/unknown domains must fail closed.
                self.inventory(cursor, baseline, self.sources)
                cursor.execute("""UPDATE migration_work_sources SET state='needs_attention',updated_at=?
                    WHERE account_key=? AND state='executing' AND active_runtime_id<>?""",
                    (self.sources._now(), baseline.account.key, baseline.runtime_id))

    async def start(self):
        if self._renewal is not None:
            return
        try:
            for baseline in self.baselines.values():
                if not await self.database_calls(baseline.acquire):
                    raise MigrationDenied("baseline_already_owned")
                await self.database_calls(self._register, baseline, initial=True)
        except BaseException:
            for baseline in self.baselines.values():
                await self.database_calls(baseline.close)
            raise
        self._renewal = asyncio.create_task(self._renew(), name="onebot-route-baselines")

    async def _renew(self):
        while True:
            await asyncio.sleep(30)
            for baseline in self.baselines.values():
                # Failure leaves the persisted expiration unchanged. No work is
                # authorized after expiry; a dead session is never reacquired.
                try:
                    await self.database_calls(self._register, baseline)
                except Exception:
                    continue

    async def connected(self, bot):
        baseline = self._baseline(str(bot.self_id))
        await self.database_calls(self._register, baseline)

    async def close(self):
        if self._renewal is not None:
            self._renewal.cancel()
            await asyncio.gather(self._renewal, return_exceptions=True)
            self._renewal = None
        for baseline in self.baselines.values():
            await self.database_calls(baseline.close)
        from .onebot_route import uninstall_boundary
        from .onebot_route_context import uninstall_source_runtime
        uninstall_boundary(self)
        uninstall_source_runtime(self)

    def _baseline(self, bot_id):
        matches = [value for value in self.baselines.values() if value.account.app_id == bot_id]
        if len(matches) != 1:
            raise MigrationDenied("onebot_baseline_required")
        return matches[0]

    def _handler(self, name, bot, event):
        baseline = self._baseline(str(bot.self_id))
        if event is None or str(event.self_id) != baseline.account.app_id:
            raise MigrationDenied("authenticated_event_required")
        group = getattr(event, "group_id", None)
        endpoint = EndpointKey(baseline.account, "group" if group is not None else "private",
                               _native(group if group is not None else event.user_id))
        actor = ActorKey(baseline.account, "user", "", _native(event.user_id))
        native = getattr(event, "message_id", None)
        # Do not hash plaintext approval/OTP content: a short secret remains
        # enumerable even if only its unkeyed hash is retained. Immutable
        # authenticated source metadata suffices for this once-only handler.
        metadata = {key: getattr(event, key, None) for key in (
            "post_type", "notice_type", "sub_type", "operator_id", "target_id", "time")}
        fingerprint = digest([name, endpoint.key, actor.key, native, metadata])
        ref = digest([name, str(native) if native is not None else fingerprint])
        scope = self.canonical_scope(event)
        with self.sources.transaction() as cursor:
            source_id = self.sources.create(cursor, baseline=baseline, endpoint=endpoint, scope=scope, actor=actor,
                kind="handler", ref=ref, revision=1, fingerprint=fingerprint, deadline=self.sources._now()+3600)
            return self.sources.claim(cursor, source_id, baseline=baseline)

    @asynccontextmanager
    async def handler_context(self, name, bot, event):
        proof = await self.database_calls(self._handler, name, bot, event)
        uncertain = True
        try:
            with source_context(proof, lambda: self.sources.current(proof)):
                yield proof
            uncertain = False
        finally:
            await self.database_calls(self.sources.finish, proof, uncertain=uncertain)

    def _job(self, job):
        with self.sources.transaction() as cursor:
            row = cursor.execute("SELECT source_id,account_key FROM migration_work_sources WHERE job_id=?", (job.job_id,)).fetchone()
            if row is None:
                raise MigrationDenied("job_source_not_inventoried")
            baseline = self.baselines.get(row["account_key"])
            if baseline is None:
                raise MigrationDenied("onebot_baseline_required")
            return self.sources.claim(cursor, row["source_id"], baseline=baseline, job=job)

    @asynccontextmanager
    async def job_context(self, job):
        from ..workers.durable_jobs import JobDeferred
        try:
            proof = await self.database_calls(self._job, job)
        except Exception:
            # No epoch refresh or blind retry. An explicit new reviewed source
            # is required if the original source cannot be resumed safely.
            raise RouteAuthorityLost("job_source_unavailable") from None
        uncertain = True
        deferred = False
        try:
            with source_context(proof, lambda: self.sources.current(proof)):
                yield proof
            uncertain = False
        except JobDeferred:
            deferred = True
            raise
        finally:
            await self.database_calls(self.sources.finish, proof, uncertain=uncertain, deferred=deferred)

    def _begin(self, call: PhysicalCall, proof):
        if call.api in _BOOTSTRAP_READS:
            return None
        require_source(proof, self.sources)
        if proof.result_digest is not None:
            raise MigrationDenied("result_source_cannot_send_directly")
        if str(call.bot_id) != proof.endpoint.account.app_id:
            raise MigrationDenied("physical_account_mismatch")
        if call.api in _SEND_GROUP | _READ_GROUP or (call.api == "send_msg" and (
                call.message_type == "group" or (call.message_type is None and call.group_id is not None and call.user_id is None))):
            endpoint = EndpointKey(proof.endpoint.account, "group", _native(call.group_id))
        elif call.api in _SEND_PRIVATE or (call.api == "send_msg" and (
                call.message_type == "private" or (call.message_type is None and call.user_id is not None and call.group_id is None))):
            endpoint = EndpointKey(proof.endpoint.account, "private", _native(call.user_id))
        elif self.target_resolver is not None:
            endpoint = self.target_resolver(call, proof)
            if not isinstance(endpoint, EndpointKey) or endpoint.account != proof.endpoint.account:
                raise MigrationDenied("physical_target_required")
        else:
            raise MigrationDenied("unsupported_managed_api")
        with self.sources.transaction() as cursor:
            self.routes.lock(cursor, endpoints=(proof.endpoint, endpoint),
                scopes=(proof.scope.key, physical_scope(endpoint).key))
            if call.api in _READ_GROUP:
                self.sources.require(cursor, proof, operation="read")
                self.routes.assert_active_route(cursor, canonical_scope=physical_scope(endpoint), endpoint=endpoint,
                    route_epoch=proof.route_epoch if endpoint == proof.endpoint else 0, operation="read")
                return None
            row = self.sources.require(cursor, proof, operation="send")
            attempt_id = None
            if row["source_kind"] == "delivery":
                outgoing = self.sources._outgoing(cursor, row)
                if (outgoing["target_platform"], outgoing["target_kind"], outgoing["target_native_conversation_id"]) != (
                        endpoint.account.platform, endpoint.kind, endpoint.native_conversation_id):
                    raise MigrationDenied("delivery_physical_target_mismatch")
                attempt = cursor.execute("SELECT attempt_id FROM delivery_attempts WHERE delivery_id=? AND attempt=? AND state='sending'",
                                         (outgoing["delivery_id"], outgoing["attempts"])).fetchone()
                if attempt is None:
                    raise MigrationDenied("delivery_attempt_missing")
                attempt_id = attempt["attempt_id"]
            seq = row["effect_sequence"] + 1
            # A secondary destination never inherits the primary route's epoch.
            # Until explicitly bound by host evidence, only epoch zero is valid.
            target_epoch = proof.route_epoch if endpoint == proof.endpoint else 0
            permit = self.routes.begin_dispatch(cursor, source_endpoint=proof.endpoint, source_scope=proof.scope,
                physical_endpoint=endpoint, route_epoch=proof.route_epoch, target_route_epoch=target_epoch,
                operation_ref=f"source:{proof.source_id}:{seq}", operation="send", source=proof,
                now=self.sources._now(), attempt_id=attempt_id)
            cursor.execute("UPDATE migration_dispatch_activities SET source_id=? WHERE activity_id=?",
                           (proof.source_id, permit.activity_id))
            cursor.execute("UPDATE migration_work_sources SET effect_sequence=?,updated_at=? WHERE source_id=?",
                           (seq, self.sources._now(), proof.source_id))
            return permit

    async def begin(self, call, source):
        try:
            return await self.database_calls(self._begin, call, source)
        except Exception:
            raise RouteAuthorityLost("onebot_route_unavailable") from None

    def _observe(self, permit, observation: TransportObservation):
        native = observation.message_id or observation.file_id
        # ActionFailed can be emitted after a transport loss. Without an exact
        # platform contract it is not proof of rejection and cannot release work.
        status = "accepted" if observation.returned and native else "unknown"
        with self.sources.transaction() as cursor:
            self.routes.observe_dispatch(cursor, proof=permit, status=status, native_id=native,
                                         now=self.sources._now())

    async def observe(self, permit, observation):
        await self.database_calls(self._observe, permit, observation)
