"""Record successful sends, quote indexes and turn/file associations."""
from __future__ import annotations
import json
import secrets
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_from_json, body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.conversation_scope import ConversationScope
from .protected_plans import load_protected_ref, verify_protected_attempt
from .lease import assert_epoch
from .send_types import OutboxInvariantError, SendOutcome, digest, stable_json


def _native(value):
    if not isinstance(value, str) or not value:
        raise OutboxInvariantError('invalid_receipt_identifier')
    return value


def record_receipt(cursor, reservation, body, now):
    native = _native(body['id'])
    ref = (body.get('ext_info') or {}).get('ref_idx')
    receipt_id = secrets.token_hex(16)
    endpoint = reservation.endpoint
    cursor.execute("""INSERT INTO delivery_receipts
        (receipt_id,attempt_id,account_key,endpoint_key,kind,native_message_id,ref_idx,receipt_digest,observed_at)
        VALUES (?,?,?,?,'response',?,?,?,?) ON CONFLICT(attempt_id,kind,native_message_id) DO NOTHING""",
        (receipt_id, reservation.attempt_id, endpoint.account.key, endpoint.key, native, ref,
         digest(stable_json([reservation.attempt_id, native, ref])), now))
    return cursor.execute("SELECT receipt_id FROM delivery_receipts WHERE attempt_id=? AND kind='response' AND native_message_id=?",
        (reservation.attempt_id, native)).fetchone()['receipt_id']


def _canonical_body(delivery):
    if delivery["payload_mode"] == "ephemeral":
        if delivery["purpose"] not in {"binding_otp", "approval_otp"} or delivery["body_json"] != '{"v":1,"nodes":[]}':
            raise OutboxInvariantError("invalid_ephemeral_body")
        return MessageBody()
    try:
        encoded = json.loads(delivery["body_json"])
        body = body_from_json(delivery["body_json"])
        if json.loads(body_to_json(body)) != encoded:
            raise ValueError
        return body
    except (TypeError, ValueError):
        raise OutboxInvariantError("invalid_delivery_body") from None


def _context_projection(cursor, delivery, attempt):
    """Project confirmed whoami content without changing protected send evidence.

    A current binding/window is deliberately not required for historical receipt
    settlement. The immutable manifest and its exact persisted decision identify
    this special response; message text alone never selects a redaction policy.
    """
    body = _canonical_body(delivery)
    if delivery["payload_mode"] == "protected":
        load_protected_ref(cursor, delivery)
        return body, {"content_redacted": True, "redaction_reason": "qq_official_protected_artifact",
                      "artifact_delivery": True, "protected_payload_version": 1}
    if delivery["payload_mode"] != "persistent":
        return body, {}
    plan = cursor.execute("SELECT manifest_json,manifest_digest FROM delivery_plans WHERE plan_id=?",
                          (delivery["plan_id"],)).fetchone()
    if plan is None:
        raise OutboxInvariantError("receipt_plan_missing")
    try:
        manifest = json.loads(plan["manifest_json"])
        control = manifest.get("control")
    except (TypeError, ValueError, AttributeError):
        raise OutboxInvariantError("receipt_plan_invalid") from None
    if not isinstance(control, dict) or control.get("kind") != "authenticated_control":
        return body, {}
    if (digest(plan["manifest_json"]) != plan["manifest_digest"]
            or set(control) != {"kind", "control_id"} or not isinstance(control["control_id"], str)):
        raise OutboxInvariantError("control_projection_integrity_conflict")
    decision = cursor.execute("""SELECT control_action FROM qq_official_inbound_events
        WHERE inbox_id=? AND account_key=? AND endpoint_key=? AND control_actor_key=?""",
        (control["control_id"], delivery["account_key"], delivery["endpoint_key"], delivery["requester_actor_key"])).fetchone()
    if decision is None or decision['control_action'] != 'whoami':
        return body, {}
    return MessageBody((TextNode(0, "[身份查询已回复：具体平台身份标识已从会话上下文隐去，仅保留于受保护发送审计。]"),)), {
        "content_redacted": True, "redaction_reason": "qq_official_whoami_identity", "control_kind": "whoami",
    }


def _quote(cursor, attempt, delivery, endpoint):
    if attempt["payload_mode"] == "ephemeral":
        return None, None
    try:
        if attempt["payload_mode"] == "protected":
            _, wire = verify_protected_attempt(cursor, delivery, attempt)
        else:
            wire = json.loads(attempt["wire_payload_json"])
        reference = wire.get("message_reference")
        if reference is None:
            return None, None
        if not isinstance(reference, dict):
            raise ValueError
        index = _native(reference.get("message_id"))
    except (TypeError, ValueError, AttributeError, OutboxInvariantError):
        raise OutboxInvariantError("invalid_attempt_reference") from None
    rows = cursor.execute("""SELECT native_message_id,ambiguous FROM qq_official_message_indexes
        WHERE account_key=? AND endpoint_key=? AND index_value=? AND index_kind IN ('msg_idx','ref_idx')""",
        (endpoint.account.key, endpoint.key, index)).fetchall()
    if (not rows or any(row["ambiguous"] for row in rows)
            or {row["native_message_id"] for row in rows} != {delivery["reply_to_native_message_id"]}):
        return None, index
    return delivery["reply_to_native_message_id"], index


def _record_reference(cursor, endpoint, native, canonical, ref_idx, now):
    if ref_idx is None:
        return False
    old = cursor.execute("""SELECT native_message_id,canonical_message_id,ambiguous FROM qq_official_message_indexes
        WHERE account_key=? AND endpoint_key=? AND index_value=?""",
        (endpoint.account.key, endpoint.key, ref_idx)).fetchall()
    conflict = any(row["ambiguous"] or row["native_message_id"] != native
                   or row["canonical_message_id"] != canonical for row in old)
    if conflict:
        cursor.execute("""UPDATE qq_official_message_indexes SET ambiguous=TRUE,updated_at=?
            WHERE account_key=? AND endpoint_key=? AND index_value=?""",
            (now, endpoint.account.key, endpoint.key, ref_idx))
    cursor.execute("""INSERT INTO qq_official_message_indexes
        (endpoint_key,account_key,index_kind,index_value,native_message_id,canonical_message_id,ambiguous,created_at,updated_at)
        VALUES (?,?,'ref_idx',?,?,?,?,?,?) ON CONFLICT(endpoint_key,index_kind,index_value) DO UPDATE SET
            ambiguous=qq_official_message_indexes.ambiguous OR EXCLUDED.ambiguous
                OR qq_official_message_indexes.native_message_id<>EXCLUDED.native_message_id
                OR qq_official_message_indexes.canonical_message_id IS DISTINCT FROM EXCLUDED.canonical_message_id,
            updated_at=EXCLUDED.updated_at""",
        (endpoint.key, endpoint.account.key, ref_idx, native, canonical, conflict, now, now))
    return conflict


def finalize_receipt(cursor, *, token, receipt_id, ledger, journal=None, now):
    assert_epoch(cursor, token)
    receipt = cursor.execute('SELECT * FROM delivery_receipts WHERE receipt_id=? AND account_key=? FOR UPDATE',
        (receipt_id, token.account_key)).fetchone()
    attempt = cursor.execute('SELECT * FROM delivery_attempts WHERE attempt_id=?', (receipt['attempt_id'],)).fetchone()
    delivery = cursor.execute('SELECT * FROM deliveries WHERE delivery_id=? FOR UPDATE', (attempt['delivery_id'],)).fetchone()
    if receipt['applied_at'] is not None:
        return SendOutcome('committed', '', delivery['delivery_id'])
    endpoint = EndpointKey.parse(delivery['endpoint_key'])
    actor = ActorKey.parse(attempt['bot_actor_key'])
    body, metadata = _context_projection(cursor, delivery, attempt)
    reply_target, quoted_index = _quote(cursor, attempt, delivery, endpoint)
    native = receipt['native_message_id']
    scope_key = cursor.execute('SELECT source_scope_key FROM delivery_plans WHERE plan_id=?', (delivery['plan_id'],)).fetchone()['source_scope_key']
    stored = ledger.record_message(ConversationScope.from_key(scope_key), native_message_id=native, sender_native_user_id=actor.native_user_id,
        sender_display='Bot', body=body, occurred_at=receipt['observed_at'], direction='outbound',
        message_kind='command' if delivery['payload_mode']=='ephemeral' or metadata else 'chat',
        reply_to_native_message_id=reply_target, actor=actor, source_endpoint=endpoint,
        raw_event={'source':'qq-official', 'delivery_id':delivery['delivery_id'], 'attempt_id':attempt['attempt_id'],
                   'receipt_id':receipt_id, 'time_source':'receipt_observed', **metadata}, transaction_cursor=cursor)
    canonical = stored.canonical_message_id
    cursor.execute("""INSERT INTO qq_official_messages
        (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,direction,ref_msg_idx,created_at)
        VALUES (?,?,?,?,?,'outbound',?,?) ON CONFLICT(endpoint_key,native_message_id) DO NOTHING""",
        (secrets.token_hex(16), token.account_key, endpoint.key, native, canonical, quoted_index, now))
    _record_reference(cursor, endpoint, native, canonical, receipt['ref_idx'], now)
    if delivery['turn_id'] is not None:
        journal.link_send(delivery['turn_id'], canonical, node_id=f"outbox:{delivery['delivery_id']}",
            chunk_index=delivery['part_index'], transaction_cursor=cursor, strict_association=True)
    cursor.execute("UPDATE delivery_attempts SET state='committed',acceptance='confirmed_yes',finished_at=? WHERE attempt_id=?",
        (now, attempt['attempt_id']))
    cursor.execute("""UPDATE deliveries SET status='committed',native_message_id=?,lease_until=NULL,
        wait_reason=NULL,last_error='',updated_at=? WHERE delivery_id=?""", (native, now, delivery['delivery_id']))
    cursor.execute('UPDATE delivery_receipts SET applied_at=? WHERE receipt_id=?', (now, receipt_id))
    from .file_projection import apply_file_receipt
    apply_file_receipt(cursor, delivery=delivery, attempt=attempt, now=now)
    return SendOutcome('committed', '', delivery['delivery_id'])
