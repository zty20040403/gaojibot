"""Source-derived control replies. No reducer, network, commit or queue.

The real authenticated Tx A first writes bounded control metadata and its
gateway receipt. These functions never authorize a caller-created BindingEffect.
The existing outbox revalidates these source rows at enqueue, prepare and mark.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import secrets
import unicodedata

from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


_ACKS = {
    "already_confirmed": "该请求已按原选择确认；本次没有重复执行。",
    "cancelled": "本次绑定请求已取消。",
    "confirmed": "本次绑定请求已确认。",
    "conflict": "绑定状态或候选版本已变化，请重新查询状态。",
    "denied": "当前身份没有执行此绑定操作的权限。",
    "expired": "绑定请求已过期，请重新申请。",
    "invalid": "绑定请求无效，请核对命令。",
    "issued": "绑定挑战已发送，请在有效期内完成后续操作。",
    "locked": "绑定请求已锁定，请重新申请。",
    "pending": "绑定请求正在等待原 owner 处理。",
    "rejected": "本次绑定请求已拒绝，原验证码已失效。",
    "stale": "绑定状态已经变化，请重新查询状态。",
    "syntax": "绑定命令参数无效，请核对命令格式。",
}
_ACTIONS = {"whoami", "create", "submit", "status", "confirm", "reject", "cancel", "invalid"}
_CODES = {*_ACKS, "candidate", "identity", "limited", "sending"}


class ControlEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class ControlView:
    control_id: str = field(repr=False)
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    body: MessageBody = field(repr=False)
    body_digest: str = field(repr=False)
    window_id: str = field(repr=False)
    expires_at: int


def _identity(endpoint_key, actor_key):
    try:
        endpoint, actor = EndpointKey.parse(endpoint_key), ActorKey.parse(actor_key)
        values = (endpoint.account.app_id, endpoint.native_conversation_id, actor.native_user_id, actor.identity_namespace)
        valid = (endpoint.account.platform == "qq-official" and actor.identity_kind != "bot"
            and actor.matches_endpoint(endpoint) and len(endpoint.account.key.encode()) <= 512
            and len(endpoint.key.encode()) <= 1000 and len(actor.key.encode()) <= 1280
            and all(len(value.encode()) <= 256 and not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value) for value in values))
        if not valid:
            raise ValueError
        return endpoint, actor
    except (ValueError, TypeError, UnicodeError):
        raise ControlEvidenceError("invalid_control_identity") from None


def safe_body(endpoint, actor, action, code):
    if action not in _ACTIONS or code not in _CODES:
        raise ControlEvidenceError("unknown_control_decision")
    _identity(endpoint.key, actor.key)
    if code in {"limited", "sending", "candidate"}:
        # OTP stays RAM-only; pending-candidate status uses the existing
        # live-owner preview authority, never this limited acknowledgment port.
        return "suppressed", MessageBody()
    if action == "whoami" and code == "identity":
        text = (f"当前入口：{endpoint.kind} / {endpoint.native_conversation_id}\n"
                f"当前身份类型：{actor.identity_kind}\n当前身份：{actor.native_user_id}\n"
                "此信息仅用于识别你在当前入口的身份，不授予 owner 或管理权限。")
        return "whoami", MessageBody((TextNode(0, text),))
    if code == "identity":
        raise ControlEvidenceError("control_action_mismatch")
    return "ack", MessageBody((TextNode(0, _ACKS[code]),))


def _digest(body):
    return hashlib.sha256(body_to_json(body).encode()).hexdigest()


def _source(cursor, token, inbox_id, session_id, seq):
    row = cursor.execute("""SELECT i.* FROM qq_official_inbound_events i
        JOIN qq_official_gateway_receipts r ON r.account_key=i.account_key AND r.inbox_id=i.inbox_id
        WHERE i.inbox_id=? AND i.account_key=? AND r.session_id=? AND r.seq=?
          AND r.disposition='control' AND i.status='processed' AND i.reason_code='binding_control'
          AND r.event_type=i.event_type
          AND i.event_type IN ('GROUP_AT_MESSAGE_CREATE','GROUP_MESSAGE_CREATE','C2C_MESSAGE_CREATE')
          AND i.echo_state='not_echo' AND i.payload_json IS NULL
          AND i.control_actor_key IS NOT NULL AND i.control_action IS NOT NULL
          AND i.control_decision_code IS NOT NULL""", (inbox_id, token.account_key, session_id, seq)).fetchone()
    if row is None:
        raise ControlEvidenceError("authenticated_control_source_required")
    return row


def record_decision(cursor, token, *, inbox_id, source_session_id, source_seq, now):
    """Internal Tx A hook AFTER its gateway receipt; no caller-supplied decision."""
    if type(now) is not int or not 0 <= now < 2**63:
        raise ControlEvidenceError("invalid_control_time")
    assert_epoch(cursor, token)
    source = _source(cursor, token, inbox_id, source_session_id, source_seq)
    endpoint, actor = _identity(source["endpoint_key"], source["control_actor_key"])
    kind, body = safe_body(endpoint, actor, source["control_action"], source["control_decision_code"])
    route = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=?",
                           (endpoint.key, token.account_key)).fetchone()
    if (route is None or route["generation"] != source["endpoint_generation"]
            or route["route_epoch"] != source["route_epoch"] or route["membership_state"] != "present"
            or route["binding_state"] == "revoked"):
        raise ControlEvidenceError("control_source_route_changed")
    window = cursor.execute("""SELECT * FROM qq_official_reply_windows
        WHERE account_key=? AND endpoint_key=? AND credential_kind='msg_id' AND credential_value=?
          AND endpoint_generation=? AND route_epoch=? AND state='active'""",
        (token.account_key, endpoint.key, source["native_message_id"], source["endpoint_generation"], source["route_epoch"])).fetchone()
    deadline = min(source["received_at"] + 30, window["expires_at"] if window else source["received_at"])
    if kind == "suppressed" or window is None or deadline <= now:
        kind, body = "suppressed", MessageBody()
    values = dict(control_id=secrets.token_hex(16), inbox_id=inbox_id, account_key=token.account_key,
        endpoint_key=endpoint.key, requester_actor_key=actor.key, source_session_id=source_session_id,
        source_seq=source_seq, source_native_message_id=source["native_message_id"],
        endpoint_generation=source["endpoint_generation"], route_epoch=source["route_epoch"],
        action=source["control_action"], decision_code=source["control_decision_code"], reply_kind=kind,
        body_digest=_digest(body), reply_window_id=window["window_id"] if window else None,
        received_at=source["received_at"], expires_at=deadline,
        state="terminal" if kind == "suppressed" else "recorded", created_at=now, updated_at=now)
    cursor.execute("INSERT INTO qq_official_control_decisions (" + ",".join(values) + ") VALUES (" +
        ",".join("?" for _ in values) + ") ON CONFLICT(inbox_id) DO NOTHING", tuple(values.values()))
    return cursor.execute("SELECT control_id FROM qq_official_control_decisions WHERE inbox_id=? AND account_key=?",
                          (inbox_id, token.account_key)).fetchone()["control_id"]


def load_authority(cursor, token, *, control_id, now):
    """Read exact not-yet-attempted body authority; no general admission grant.

    This view is not an outbox capability. The outbox accepts only the
    stored control_id and re-reads these same checks at enqueue/prepare/mark;
    a caller-created ControlView or BindingEffect grants no reply permission.
    """
    return _load_authority(cursor, token, control_id=control_id, now=now)


def _load_authority(cursor, token, *, control_id, now, expected_attempt=None, reserved_window=False):
    if type(now) is not int or not 0 <= now < 2**63:
        raise ControlEvidenceError("invalid_control_time")
    assert_epoch(cursor, token)
    row = cursor.execute("SELECT * FROM qq_official_control_decisions WHERE control_id=? AND account_key=?",
                         (control_id, token.account_key)).fetchone()
    states = {"attempted"} if expected_attempt is not None else {"recorded", "handed_off"}
    if (row is None or row["state"] not in states or row["attempt_id"] != expected_attempt
            or row["reply_kind"] == "suppressed" or row["expires_at"] <= now):
        return None
    source = _source(cursor, token, row["inbox_id"], row["source_session_id"], row["source_seq"])
    endpoint, actor = _identity(row["endpoint_key"], row["requester_actor_key"])
    if (source["endpoint_key"] != endpoint.key or source["control_actor_key"] != actor.key
            or source["native_message_id"] != row["source_native_message_id"]
            or source["control_action"] != row["action"] or source["control_decision_code"] != row["decision_code"]):
        raise ControlEvidenceError("control_evidence_conflict")
    kind, body = safe_body(endpoint, actor, row["action"], row["decision_code"])
    if kind != row["reply_kind"] or _digest(body) != row["body_digest"]:
        raise ControlEvidenceError("control_body_conflict")
    window = cursor.execute("""SELECT w.* FROM qq_official_reply_windows w
        JOIN qq_official_endpoints e ON e.endpoint_key=w.endpoint_key AND e.account_key=w.account_key
        WHERE w.window_id=? AND w.account_key=? AND w.endpoint_key=? AND w.credential_kind='msg_id'
          AND w.credential_value=? AND w.state='active' AND w.expires_at>?
          AND e.membership_state='present'
          AND e.binding_state<>'revoked' AND e.generation=? AND e.route_epoch=?
          AND w.endpoint_generation=e.generation AND w.route_epoch=e.route_epoch""",
        (row["reply_window_id"], token.account_key, endpoint.key, row["source_native_message_id"], now,
         row["endpoint_generation"], row["route_epoch"])).fetchone()
    if (window is None or row["expires_at"] > min(source["received_at"] + 30, window["expires_at"])
            or (not reserved_window and window["used_count"] + window["reserved_count"] >= window["max_replies"])):
        return None
    return ControlView(control_id, endpoint, actor, body, row["body_digest"], window["window_id"], row["expires_at"])


def _delivery(cursor, view, plan_id, delivery_id):
    row = cursor.execute("""SELECT d.*,p.part_count,p.requester_actor_key AS plan_requester FROM deliveries d
        JOIN delivery_plans p ON p.plan_id=d.plan_id
        WHERE d.delivery_id=? AND p.plan_id=? AND p.account_key=? AND p.endpoint_key=?""",
        (delivery_id,plan_id,view.endpoint.account.key,view.endpoint.key)).fetchone()
    source = cursor.execute("SELECT credential_value FROM qq_official_reply_windows WHERE window_id=?", (view.window_id,)).fetchone()
    if (row is None or source is None or row["requester_actor_key"] != view.actor.key or row["plan_requester"] != view.actor.key
            or row["part_count"] != 1 or row["part_index"] != 0 or row["purpose"] != "final"
            or row["reply_to_native_message_id"] != source["credential_value"]
            or row["source_scope_key"] != view.endpoint.scope.key
            or row["payload_mode"] != "persistent" or row["expires_at"] > view.expires_at
            or row["body_json"] != body_to_json(view.body)):
        raise ControlEvidenceError("control_plan_not_exact")
    return row


def bind_control_plan(cursor, token, *, control_id, plan_id, delivery_id, now):
    """Inside the SAME transaction as the single M3 plan creation; no own commit."""
    view = load_authority(cursor, token, control_id=control_id, now=now)
    if view is None:
        raise ControlEvidenceError("control_reply_unavailable")
    _delivery(cursor, view, plan_id, delivery_id)
    row = cursor.execute("SELECT * FROM qq_official_control_decisions WHERE control_id=? FOR UPDATE", (control_id,)).fetchone()
    if row["plan_id"] is not None:
        if (row["plan_id"],row["delivery_id"]) != (plan_id,delivery_id):
            raise ControlEvidenceError("control_plan_already_bound")
        return
    cursor.execute("""UPDATE qq_official_control_decisions SET plan_id=?,delivery_id=?,state='handed_off',updated_at=?
        WHERE control_id=? AND account_key=? AND state='recorded' AND plan_id IS NULL""",
        (plan_id,delivery_id,now,control_id,token.account_key))
    if cursor.rowcount != 1:
        raise ControlEvidenceError("control_plan_conflict")


def claim_control_attempt(cursor, token, *, control_id, attempt_id, now):
    """M3 prepare hook AFTER allocating attempt/window, BEFORE committing it.

    A failure rolls back the allocation. Existing historical attempts make a
    second attempt ineligible even when one was proven unsent. No retry budget
    or fallback may clear this binding; mark must recheck the exact first ID.
    """
    assert_epoch(cursor, token)
    row = cursor.execute("SELECT * FROM qq_official_control_decisions WHERE control_id=? AND account_key=? FOR UPDATE",
                         (control_id,token.account_key)).fetchone()
    if row is None:
        raise ControlEvidenceError("control_reply_unavailable")
    if row["attempt_id"] == attempt_id:
        return assert_control_attempt(cursor, token, control_id=control_id, attempt_id=attempt_id, now=now)
    view = _load_authority(cursor, token, control_id=control_id, now=now, reserved_window=True)
    if view is None or row["state"] != "handed_off":
        raise ControlEvidenceError("control_attempt_already_used")
    _delivery(cursor, view, row["plan_id"], row["delivery_id"])
    attempt = cursor.execute("SELECT * FROM delivery_attempts WHERE attempt_id=? AND delivery_id=? AND account_key=?",
        (attempt_id,row["delivery_id"],token.account_key)).fetchone()
    count = cursor.execute("SELECT COUNT(*) AS count FROM delivery_attempts WHERE delivery_id=?", (row["delivery_id"],)).fetchone()["count"]
    if (count != 1 or attempt is None or attempt["endpoint_key"] != view.endpoint.key
            or attempt["fencing_epoch"] != token.epoch or attempt["state"] != "reserved"
            or attempt["send_mode"] != "passive" or attempt["window_id"] != view.window_id
            or attempt["send_by_at"] > view.expires_at or attempt["send_by_at"] <= now):
        raise ControlEvidenceError("control_attempt_not_exact")
    cursor.execute("""UPDATE qq_official_control_decisions SET attempt_id=?,state='attempted',updated_at=?
        WHERE control_id=? AND account_key=? AND state='handed_off' AND attempt_id IS NULL""",
        (attempt_id,now,control_id,token.account_key))
    if cursor.rowcount != 1:
        raise ControlEvidenceError("control_attempt_already_used")


def assert_control_attempt(cursor, token, *, control_id, attempt_id, now):
    """M3 mark-dispatching hook, before the sole physical request."""
    view = _load_authority(cursor, token, control_id=control_id, now=now,
                           expected_attempt=attempt_id, reserved_window=True)
    if view is None:
        raise ControlEvidenceError("control_attempt_unavailable")
    row = cursor.execute("SELECT * FROM qq_official_control_decisions WHERE control_id=?", (control_id,)).fetchone()
    _delivery(cursor, view, row["plan_id"], row["delivery_id"])
    attempt = cursor.execute("SELECT * FROM delivery_attempts WHERE attempt_id=? AND delivery_id=? AND account_key=?",
        (attempt_id,row["delivery_id"],token.account_key)).fetchone()
    if (attempt is None or attempt["state"] not in {"reserved", "dispatched"}
            or attempt["fencing_epoch"] != token.epoch or attempt["send_mode"] != "passive"
            or attempt["window_id"] != view.window_id or not now < attempt["send_by_at"] <= view.expires_at):
        raise ControlEvidenceError("control_attempt_not_exact")


def control_job_kind(account_key):
    # Reuse the established full-account digest and bounded kind convention.
    # On adoption, account_job_kind may add the fixed 'control' stage directly.
    from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
    return account_job_kind("turn", account_key).replace("qq_official.turn.", "qq_official.control.", 1)


def enqueue_control_job(cursor, token, control_id, jobs, now):
    """Same TxA existing durable queue; suppressed decisions never get a job."""
    assert_epoch(cursor, token)
    row = cursor.execute("SELECT * FROM qq_official_control_decisions WHERE control_id=? AND account_key=?",
                         (control_id, token.account_key)).fetchone()
    if row is None:
        raise ControlEvidenceError("control_decision_missing")
    if row["state"] == "terminal":
        return None
    kind = control_job_kind(token.account_key)
    payload = {"control_id": control_id}
    lane = "qq.control.lane:" + hashlib.sha256(row["endpoint_key"].encode()).hexdigest()
    key = "qq.control:" + control_id
    job, _ = jobs.enqueue(kind=kind, payload=payload, scope_key=lane, idempotency_key=key,
        now=now, transaction_cursor=cursor)
    if (job.kind != kind or job.payload != payload or job.scope_key != lane
            or row["job_id"] not in {None, job.job_id}):
        raise ControlEvidenceError("control_job_identity_conflict")
    cursor.execute("UPDATE qq_official_control_decisions SET job_id=?,updated_at=? WHERE control_id=?",
                   (job.job_id, now, control_id))
    return job.job_id


def validate_plan_authority(cursor, token, *, control_id, rendered, requester,
                            logical_key, source_scope_key, source_native_message_id,
                            expires_at, purpose, now):
    view = load_authority(cursor, token, control_id=control_id, now=now)
    source = cursor.execute("SELECT source_native_message_id FROM qq_official_control_decisions WHERE control_id=?",
                           (control_id,)).fetchone()
    if (view is None or source is None or requester != view.actor or rendered.endpoint != view.endpoint
            or logical_key != "control:" + control_id or source_scope_key != view.endpoint.scope.key
            or source_native_message_id != source["source_native_message_id"] or expires_at != view.expires_at
            or purpose != "final" or len(rendered.parts) != 1
            or rendered.primary_format != "text" or rendered.parts[0].mention_actors
            or body_to_json(rendered.parts[0].canonical_body) != body_to_json(view.body)):
        raise ControlEvidenceError("control_plan_not_exact")
    return view


def control_id_for_delivery(cursor, row):
    """Read only the sealed persisted plan; callers cannot supply a free flag."""
    import json
    stored = cursor.execute("SELECT manifest_json,manifest_digest FROM delivery_plans WHERE plan_id=?",
                            (row["plan_id"],)).fetchone()
    if stored is None or hashlib.sha256(stored["manifest_json"].encode()).hexdigest() != stored["manifest_digest"]:
        # Ephemeral plans have a different digest scheme and are not controls.
        if row["payload_mode"] == "ephemeral":
            return None
        raise ControlEvidenceError("control_manifest_corrupt")
    control = json.loads(stored["manifest_json"]).get("control")
    if not isinstance(control, dict) or control.get("kind") != "authenticated_control":
        return None
    if set(control) != {"kind", "control_id"} or not isinstance(control["control_id"], str):
        raise ControlEvidenceError("control_manifest_invalid")
    return control["control_id"]


def delivery_authorized(cursor, token, row, now):
    control_id = control_id_for_delivery(cursor, row)
    if control_id is None:
        return False
    if row["status"] == "sending":
        assert_control_attempt(cursor, token, control_id=control_id, attempt_id=row["current_attempt_id"], now=now)
        return True
    view = load_authority(cursor, token, control_id=control_id, now=now)
    if view is None:
        return False
    _delivery(cursor, view, row["plan_id"], row["delivery_id"])
    bound = cursor.execute("SELECT plan_id,delivery_id FROM qq_official_control_decisions WHERE control_id=?", (control_id,)).fetchone()
    return (bound["plan_id"], bound["delivery_id"]) == (row["plan_id"], row["delivery_id"])


def control_policy_authority(cursor, token, *, control_id, endpoint, source_native_id, deadline, now):
    """Same-transaction policy check before the first attempt CAS/charge commit."""
    view = _load_authority(cursor, token, control_id=control_id, now=now, reserved_window=True)
    row = cursor.execute("SELECT * FROM qq_official_control_decisions WHERE control_id=? AND account_key=?",
                         (control_id, token.account_key)).fetchone()
    if (view is None or row is None or view.endpoint != endpoint
            or row["source_native_message_id"] != source_native_id or deadline > view.expires_at
            or row["plan_id"] is None or row["delivery_id"] is None):
        raise ControlEvidenceError("control_policy_unavailable")
    _delivery(cursor, view, row["plan_id"], row["delivery_id"])
    return view


def sync_control_terminal(cursor, token, delivery_id, now):
    """Mirror existing delivery terminal state without clearing its spent attempt."""
    assert_epoch(cursor, token)
    cursor.execute("""UPDATE qq_official_control_decisions c SET state='terminal',updated_at=?
        FROM deliveries d WHERE c.delivery_id=d.delivery_id AND c.account_key=d.account_key
          AND c.account_key=? AND d.delivery_id=? AND c.state<>'terminal'
          AND d.status IN ('committed','failed','cancelled','ambiguous')""", (now, token.account_key, delivery_id))
