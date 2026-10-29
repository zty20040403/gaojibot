"""Caller-owned, doubly fenced turn execution transitions.

No queue, model, journal creation or network call lives here. The shared strict
orchestrator lifecycle joins these primitives to its journal/usage transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import threading
import weakref

from src.plugins.ai_chat.adapters.qq_official.ingest import turn_dispatch_key, turn_dispatch_lane
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.message_ir import MessageBody, body_from_json, body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJob


class ExecutionError(RuntimeError):
    """Fixed diagnostics only. An exception requires caller transaction rollback."""


class ExecutionFenceLost(ExecutionError):
    """Map this to False in active_job_fence.check; never retry model execution."""


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ExecutionHandle:
    execution_id: str
    job_id: int
    inbox_id: str = field(repr=False)
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    canonical_message_id: int
    source_native_message_id: str = field(repr=False)
    scope_key: str = field(repr=False)
    endpoint_generation: int
    route_epoch: int
    account_fencing_epoch: int
    worker_owner: str = field(repr=False)
    job_lease_generation: int
    execution_revision: int
    deadline_at: int
    result_purpose: str
    execution_kind: str


@dataclass(frozen=True)
class ExecutionDecision:
    state: str
    reason: str = ""
    handle: ExecutionHandle | None = field(default=None, repr=False)
    journal_turn_id: int | None = None
    plan_id: str | None = None


_issued = weakref.WeakValueDictionary()
_lock = threading.Lock()
_RESULTS = frozenset({"succeeded", "silence", "crashed", "aborted", "denied", "cancelled"})


def _time(now):
    if type(now) is not int or not 0 <= now <= 2**63 - 1:
        raise ExecutionError("invalid_execution_time")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reason(reason):
    if not isinstance(reason, str) or re.fullmatch(r"[a-z0-9_]{1,80}", reason) is None:
        raise ExecutionError("invalid_execution_reason")


def _issue(row):
    handle = ExecutionHandle(row["execution_id"], row["job_id"], row["inbox_id"],
        EndpointKey.parse(row["endpoint_key"]), ActorKey.parse(row["requester_actor_key"]),
        row["canonical_message_id"], row["source_native_message_id"], row["scope_key"], row["endpoint_generation"], row["route_epoch"],
        row["account_fencing_epoch"], row["worker_owner"], row["job_lease_generation"],
        row["execution_revision"], row["deadline_at"], row["result_purpose"], row["execution_kind"])
    with _lock:
        _issued[id(handle)] = handle
    return handle


def _require(handle):
    with _lock:
        if not isinstance(handle, ExecutionHandle) or _issued.get(id(handle)) is not handle:
            raise ExecutionFenceLost("unissued_execution_handle")


def _has_bot_reply(cursor, endpoint, message):
    target = message["reply_to_native_message_id"]
    if not target:
        return False
    source = cursor.execute("""SELECT ref_msg_idx FROM qq_official_messages
        WHERE account_key=? AND endpoint_key=? AND native_message_id=?
          AND canonical_message_id=? AND direction='inbound'""",
        (endpoint.account.key, endpoint.key, message["native_message_id"], message["canonical_message_id"])).fetchone()
    if source is None or source["ref_msg_idx"] is None:
        return False
    rows = cursor.execute("""SELECT i.ambiguous,i.native_message_id,i.canonical_message_id,
            q.canonical_message_id AS mapped_canonical,q.direction AS mapped_direction,
            m.canonical_message_id AS ledger_canonical,m.native_message_id AS ledger_native,
            m.source_endpoint_key,m.direction,m.sender_actor_key,c.scope_key
        FROM qq_official_message_indexes i
        LEFT JOIN qq_official_messages q ON q.account_key=i.account_key AND q.endpoint_key=i.endpoint_key
            AND q.native_message_id=i.native_message_id
        LEFT JOIN messages m ON m.canonical_message_id=i.canonical_message_id
        LEFT JOIN conversations c ON c.conversation_id=m.conversation_id
        WHERE i.account_key=? AND i.endpoint_key=? AND i.index_value=? AND i.index_kind IN ('msg_idx','ref_idx')""",
        (endpoint.account.key, endpoint.key, source["ref_msg_idx"])).fetchall()
    if not rows:
        return False
    for row in rows:
        if (row["ambiguous"] or row["native_message_id"] != target or row["ledger_native"] != target
                or row["canonical_message_id"] is None or row["canonical_message_id"] != row["mapped_canonical"]
                or row["canonical_message_id"] != row["ledger_canonical"]
                or row["direction"] != "outbound" or row["mapped_direction"] != "outbound"
                or row["source_endpoint_key"] != endpoint.key or row["scope_key"] != message["scope_key"]):
            return False
        try:
            author = ActorKey.parse(row["sender_actor_key"])
        except (TypeError, ValueError):
            return False
        if author.account != endpoint.account or author.identity_kind != "bot":
            return False
    return True


def _load(cursor, *, token, job, now, admission):
    """Account -> endpoint -> inbox -> job locks; no I/O beyond caller SQL."""
    _time(now)
    assert_epoch(cursor, token)
    if not isinstance(job, DurableJob) or not isinstance(job.payload, dict) or set(job.payload) != {"inbox_id"}:
        raise ExecutionError("invalid_turn_job")
    inbox_id = job.payload["inbox_id"]
    if not isinstance(inbox_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", inbox_id) is None:
        raise ExecutionError("invalid_turn_job")
    inbox = cursor.execute("SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND account_key=?",
        (inbox_id, token.account_key)).fetchone()
    if inbox is None:
        raise ExecutionError("turn_inbox_unavailable")
    if getattr(admission, 'lock_inbox', None) is not None:
        from .migration_types import MigrationDenied
        try:
            admission.lock_inbox(cursor, inbox_id, operation='execute')
        except MigrationDenied:
            raise ExecutionFenceLost('migration_source_unavailable') from None
    endpoint_row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE",
        (inbox["endpoint_key"], token.account_key)).fetchone()
    inbox = cursor.execute("SELECT * FROM qq_official_inbound_events WHERE inbox_id=? FOR UPDATE", (inbox_id,)).fetchone()
    current = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (job.job_id,)).fetchone()
    if (current is None or current["status"] != "running" or current["lease_owner"] != job.lease_owner
            or current["lease_generation"] != job.lease_generation or current["attempts"] != job.attempts
            or current["lease_until"] is None or current["lease_until"] <= now
            or not job.lease_owner or job.lease_generation < 1):
        raise ExecutionFenceLost("turn_job_lease_lost")
    endpoint = EndpointKey.parse(inbox["endpoint_key"])
    expected_kind = account_job_kind("turn", token.account_key)
    expected_lane = turn_dispatch_lane(token.account_key, endpoint.key)
    if (endpoint.account.key != token.account_key or current["kind"] != expected_kind or job.kind != expected_kind
            or current["scope_key"] != expected_lane or job.scope_key != expected_lane
            or current["idempotency_key"] != turn_dispatch_key(token.account_key, endpoint.key, inbox_id)
            or json.loads(current["payload_json"]) != {"inbox_id": inbox_id}
            or inbox["dispatch_job_id"] != job.job_id or inbox["status"] != "processed"
            or inbox["echo_state"] != "not_echo" or inbox["canonical_message_id"] is None):
        raise ExecutionError("turn_job_identity_conflict")
    message = cursor.execute("""SELECT m.*,c.scope_key FROM messages m JOIN conversations c USING(conversation_id)
        WHERE m.canonical_message_id=?""", (inbox["canonical_message_id"],)).fetchone()
    if message is None:
        raise ExecutionError("turn_message_unavailable")
    actor = ActorKey.parse(message["sender_actor_key"])
    if (not actor.matches_endpoint(endpoint) or actor.identity_kind == "bot"
            or message["source_endpoint_key"] != endpoint.key or message["direction"] != "inbound"
            or message["native_message_id"] != inbox["native_message_id"]
            or message["sender_native_user_id"] != actor.native_user_id):
        raise ExecutionError("turn_message_identity_conflict")
    if endpoint.kind != "private" and inbox["trigger_requested"] is not True and not _has_bot_reply(cursor, endpoint, message):
        raise ExecutionError("turn_trigger_unavailable")
    if (inbox["turn_text_version"] != 1 or not isinstance(inbox["turn_text"], str)
            or len(inbox["turn_text"].encode("utf-8")) > 262144):
        raise ExecutionError("turn_text_unavailable")
    if (endpoint_row is None or endpoint_row["generation"] != inbox["endpoint_generation"]
            or endpoint_row["route_epoch"] != inbox["route_epoch"]
            or endpoint_row["canonical_scope_key"] != message["scope_key"]
            or endpoint_row["kind"] != endpoint.kind or endpoint_row["native_conversation_id"] != endpoint.native_conversation_id
            or endpoint_row["binding_state"] == "revoked" or endpoint_row["membership_state"] != "present"
            or (endpoint.kind == "group" and endpoint_row["binding_state"] != "active")
            or not callable(admission) or admission(cursor, endpoint, actor) is not True):
        raise ExecutionFenceLost("turn_admission_changed")
    require_source = getattr(admission, 'require_inbox', None)
    if require_source is not None:
        from .migration_types import MigrationDenied
        try:
            require_source(cursor, inbox_id, endpoint=endpoint, actor=actor, operation='execute')
        except MigrationDenied:
            raise ExecutionFenceLost('migration_source_unavailable') from None
    input_digest = _digest(_json([token.account_key, endpoint.key, actor.key, inbox_id,
        job.job_id, inbox["native_message_id"], inbox["event_id"], inbox["received_at"],
        message["canonical_message_id"], message["scope_key"], message["body_json"], message["occurred_at"],
        inbox["turn_text_version"], inbox["turn_text"],
        message["reply_to_native_message_id"], inbox["endpoint_generation"], inbox["route_epoch"]]))
    return inbox, endpoint_row, message, actor, input_digest


def claim_execution(cursor, *, token, job, now, deadline_at, admission, result_purpose="final", execution_kind="chat") -> ExecutionDecision:
    """Only a returned prepared handle may enter the shared orchestrator.

    result_ready restores a frozen result only. Any already executing record
    denies another model invocation, including duplicate calls by the same owner.
    """
    _time(deadline_at)
    if not isinstance(result_purpose, str) or result_purpose not in {"final", "background_result"}:
        raise ExecutionError("invalid_execution_purpose")
    if not isinstance(execution_kind, str) or execution_kind not in {"chat", "command"}:
        raise ExecutionError("invalid_execution_kind")
    inbox, endpoint, message, actor, fingerprint = _load(cursor, token=token, job=job, now=now, admission=admission)
    row = cursor.execute("SELECT * FROM qq_official_turn_executions WHERE inbox_id=? FOR UPDATE", (inbox["inbox_id"],)).fetchone()
    if row is None:
        fields = dict(execution_id=secrets.token_hex(16), account_key=token.account_key, endpoint_key=endpoint["endpoint_key"],
            requester_actor_key=actor.key, inbox_id=inbox["inbox_id"], job_id=job.job_id,
            canonical_message_id=message["canonical_message_id"], source_native_message_id=inbox["native_message_id"], scope_key=message["scope_key"],
            endpoint_generation=endpoint["generation"], route_epoch=endpoint["route_epoch"], account_fencing_epoch=token.epoch,
            worker_owner=job.lease_owner, job_lease_generation=job.lease_generation, execution_revision=1,
            state="cancelled" if deadline_at <= now else "prepared", input_digest=fingerprint,
            reason_code="turn_deadline_expired" if deadline_at <= now else "",
            result_purpose=result_purpose, execution_kind=execution_kind,
            deadline_at=deadline_at, created_at=now, updated_at=now)
        row = cursor.execute("INSERT INTO qq_official_turn_executions (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ") RETURNING *", tuple(fields.values())).fetchone()
    else:
        if (row["job_id"] != job.job_id or row["input_digest"] != fingerprint
                or row["deadline_at"] != deadline_at or row["requester_actor_key"] != actor.key
                or row["result_purpose"] != result_purpose or row["execution_kind"] != execution_kind):
            raise ExecutionError("execution_identity_conflict")
        same_owner = (row["account_fencing_epoch"] == token.epoch and row["worker_owner"] == job.lease_owner
                      and row["job_lease_generation"] == job.lease_generation)
        if row["state"] == "executing":
            if same_owner:
                return ExecutionDecision("already_running", "execution_already_running", journal_turn_id=row["journal_turn_id"])
            cursor.execute("UPDATE qq_official_turn_executions SET state='needs_attention',reason_code='execution_owner_lost',updated_at=? WHERE execution_id=?", (now, row["execution_id"]))
            return ExecutionDecision("needs_attention", "execution_owner_lost", journal_turn_id=row["journal_turn_id"])
        if row["state"] in {"needs_attention", "cancelled", "handed_off"}:
            return ExecutionDecision(row["state"], row["reason_code"], journal_turn_id=row["journal_turn_id"], plan_id=row["plan_id"])
        if deadline_at <= now:
            cursor.execute("UPDATE qq_official_turn_executions SET state='cancelled',reason_code='turn_deadline_expired',updated_at=? WHERE execution_id=?", (now, row["execution_id"]))
            return ExecutionDecision("cancelled", "turn_deadline_expired", journal_turn_id=row["journal_turn_id"])
        if not same_owner:
            row = cursor.execute("""UPDATE qq_official_turn_executions SET account_fencing_epoch=?,worker_owner=?,
                job_lease_generation=?,execution_revision=execution_revision+1,updated_at=? WHERE execution_id=? RETURNING *""",
                (token.epoch, job.lease_owner, job.lease_generation, now, row["execution_id"])).fetchone()
    if row["state"] == "cancelled":
        return ExecutionDecision("cancelled", row["reason_code"])
    return ExecutionDecision(row["state"], handle=_issue(row), journal_turn_id=row["journal_turn_id"], plan_id=row["plan_id"])


def assert_execution_current(cursor, *, token, handle, job, now, admission):
    _require(handle)
    if token.account_key != handle.endpoint.account.key or job.job_id != handle.job_id:
        raise ExecutionFenceLost("execution_handle_namespace_conflict")
    inbox, endpoint, message, actor, fingerprint = _load(cursor, token=token, job=job, now=now, admission=admission)
    row = cursor.execute("SELECT * FROM qq_official_turn_executions WHERE execution_id=? FOR UPDATE", (handle.execution_id,)).fetchone()
    expected = (token.epoch, job.lease_owner, job.lease_generation, handle.execution_revision,
                handle.inbox_id, handle.endpoint.key, handle.actor.key, handle.canonical_message_id, handle.source_native_message_id,
                handle.scope_key, handle.endpoint_generation, handle.route_epoch, handle.deadline_at, handle.result_purpose, handle.execution_kind)
    names = ("account_fencing_epoch", "worker_owner", "job_lease_generation", "execution_revision", "inbox_id", "endpoint_key",
             "requester_actor_key", "canonical_message_id", "source_native_message_id", "scope_key", "endpoint_generation", "route_epoch", "deadline_at", "result_purpose", "execution_kind")
    if (row is None or tuple(row[name] for name in names) != expected or row["input_digest"] != fingerprint
            or handle.account_fencing_epoch != token.epoch or handle.worker_owner != job.lease_owner
            or handle.job_lease_generation != job.lease_generation):
        raise ExecutionFenceLost("execution_owner_changed")
    if row["state"] in {"cancelled", "needs_attention"}:
        raise ExecutionFenceLost("execution_not_current")
    return row


def begin_execution(cursor, *, token, handle, job, journal_turn_id, now, admission):
    row = assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    if row["execution_kind"] != "chat":
        raise ExecutionError("execution_kind_conflict")
    if row["state"] != "prepared" or row["journal_turn_id"] is not None:
        raise ExecutionError("execution_already_started")
    if now >= row["deadline_at"]:
        raise ExecutionFenceLost("turn_deadline_expired")
    if type(journal_turn_id) is not int or journal_turn_id < 1:
        raise ExecutionError("execution_journal_required")
    journal = cursor.execute("SELECT scope_key,trigger_canonical_message_id,status FROM agent_turns WHERE turn_id=? FOR UPDATE", (journal_turn_id,)).fetchone()
    if (journal is None or journal["scope_key"] != row["scope_key"] or journal["trigger_canonical_message_id"] != row["canonical_message_id"]
            or journal["status"] != "running"):
        raise ExecutionError("execution_journal_identity_conflict")
    cursor.execute("UPDATE qq_official_turn_executions SET state='executing',journal_turn_id=?,started_at=?,updated_at=? WHERE execution_id=?",
                   (journal_turn_id, now, now, handle.execution_id))


def begin_command_execution(cursor, *, token, handle, job, now, admission):
    """Commit before any router effect. No fabricated model journal is needed."""
    row = assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    if row["execution_kind"] != "command":
        raise ExecutionError("execution_kind_conflict")
    if row["state"] != "prepared" or row["journal_turn_id"] is not None:
        raise ExecutionError("execution_already_started")
    if now >= row["deadline_at"]:
        raise ExecutionFenceLost("turn_deadline_expired")
    cursor.execute("UPDATE qq_official_turn_executions SET state='executing',started_at=?,updated_at=? WHERE execution_id=?",
                   (now, now, handle.execution_id))


def _body(body):
    if not isinstance(body, MessageBody):
        raise ExecutionError("invalid_execution_result")
    try:
        encoded = body_to_json(body)
        encoded_bytes = encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ExecutionError("invalid_execution_result") from None
    if len(encoded_bytes) > 262144:
        raise ExecutionError("invalid_execution_result")
    stack = [json.loads(encoded)]
    while stack:
        value = stack.pop()
        if isinstance(value, str) and "\x00" in value:
            raise ExecutionError("invalid_execution_result")
        if isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return encoded, _digest(encoded)


def capture_result(cursor, *, token, handle, job, body, turn_id, status, now, admission):
    row = assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    if not isinstance(status, str) or status not in _RESULTS or (turn_id is not None and (type(turn_id) is not int or turn_id < 1)):
        raise ExecutionError("invalid_execution_result_status")
    encoded, fingerprint = _body(body)
    if row["journal_turn_id"] != turn_id:
        raise ExecutionError("execution_result_journal_conflict")
    if row["state"] in {"result_ready", "handed_off"}:
        if (row["result_body_json"], row["result_digest"], row["result_status"]) != (encoded, fingerprint, status):
            raise ExecutionError("execution_result_conflict")
        return False
    if row["state"] not in {"prepared", "executing"} or (row["state"] == "prepared" and turn_id is not None):
        raise ExecutionError("execution_cannot_finish")
    cursor.execute("""UPDATE qq_official_turn_executions SET state='result_ready',result_body_json=?,result_digest=?,
        result_status=?,result_at=?,updated_at=? WHERE execution_id=?""", (encoded, fingerprint, status, now, now, handle.execution_id))
    return True


def load_result(cursor, *, token, handle, job, now, admission):
    row = assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    if row["state"] not in {"result_ready", "handed_off"} or _digest(row["result_body_json"]) != row["result_digest"]:
        raise ExecutionError("execution_result_unavailable")
    try:
        body = body_from_json(row["result_body_json"])
        encoded, _ = _body(body)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ExecutionError("execution_result_integrity_conflict") from None
    if encoded != row["result_body_json"]:
        raise ExecutionError("execution_result_integrity_conflict")
    return body


def logical_result_key(handle):
    _require(handle)
    return "qq-turn-result:" + handle.execution_id


def input_text(cursor, *, token, handle, job, now, admission):
    """Return only source-bound Tx B normalization; old NULL rows stay closed."""
    assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    return cursor.execute("SELECT turn_text FROM qq_official_inbound_events WHERE inbox_id=?", (handle.inbox_id,)).fetchone()["turn_text"]


def attach_plan(cursor, *, token, handle, job, plan_id, now, admission):
    row = assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    if row["state"] == "handed_off":
        if row["plan_id"] != plan_id:
            raise ExecutionError("execution_plan_conflict")
        return False
    if row["state"] != "result_ready" or not isinstance(plan_id, str):
        raise ExecutionError("execution_result_required")
    plan = cursor.execute("SELECT * FROM delivery_plans WHERE plan_id=? FOR UPDATE", (plan_id,)).fetchone()
    try:
        manifest = json.loads(plan["manifest_json"]) if plan is not None else None
    except (TypeError, ValueError):
        raise ExecutionError("execution_plan_identity_conflict") from None
    identity = ["qq-plan-v1", token.account_key, handle.endpoint.key, logical_result_key(handle)]
    if (plan is None or not isinstance(manifest, dict) or manifest.get("source_native_message_id") != handle.source_native_message_id
            or manifest.get("control") is not None or plan["digest_scheme"] != "sha256"
            or _digest(plan["manifest_json"]) != plan["manifest_digest"]
            or plan["account_key"] != token.account_key or plan["endpoint_key"] != handle.endpoint.key
            or plan["requester_actor_key"] != handle.actor.key or plan["source_scope_key"] != handle.scope_key
            or plan["source_canonical_message_id"] != handle.canonical_message_id or plan["turn_id"] != row["journal_turn_id"]
            or plan["endpoint_generation"] != handle.endpoint_generation or plan["route_epoch"] != handle.route_epoch
            or plan["expires_at"] != handle.deadline_at or plan["purpose"] != handle.result_purpose or plan["payload_mode"] != "persistent"
            or plan["idempotency_key"] != "qqp1:" + _digest(_json(identity))):
        raise ExecutionError("execution_plan_identity_conflict")
    cursor.execute("UPDATE qq_official_turn_executions SET state='handed_off',plan_id=?,handed_off_at=?,updated_at=? WHERE execution_id=?",
                   (plan_id, now, now, handle.execution_id))
    return True


def complete_without_plan(cursor, *, token, handle, job, now, admission):
    row = assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    if row["state"] == "handed_off" and row["plan_id"] is None:
        return False
    if (row["state"] != "result_ready" or row["result_body_json"] != body_to_json(MessageBody())
            or row["result_status"] not in {"silence", "aborted", "cancelled"}):
        raise ExecutionError("execution_not_silent")
    cursor.execute("UPDATE qq_official_turn_executions SET state='handed_off',handed_off_at=?,updated_at=? WHERE execution_id=?",
                   (now, now, handle.execution_id))
    return True


def interrupt_execution(cursor, *, token, handle, job, reason, now, admission):
    _reason(reason)
    row = assert_execution_current(cursor, token=token, handle=handle, job=job, now=now, admission=admission)
    if row["state"] in {"result_ready", "handed_off"}:
        return row["state"]
    state = "cancelled" if row["state"] == "prepared" else "needs_attention"
    cursor.execute("UPDATE qq_official_turn_executions SET state=?,reason_code=?,updated_at=? WHERE execution_id=?",
                   (state, reason, now, handle.execution_id))
    return state
