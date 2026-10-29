"""Minimal transport receipts and atomic, account-fenced historical settlement.

The caller owns every transaction. Capturing late transport evidence does not
grant authority or change a send state. Only the current account lease may
apply it; finalization never calls a transport or chooses a new destination.
"""
from __future__ import annotations

import json
import re
import secrets
import unicodedata

from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_from_json, body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey

from .lease import assert_epoch
from .outbox_types import OutboxInvariantError, SendOutcome, digest, require_issued, stable_json
from .send_policy import settle_send_policy


def _time(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise OutboxInvariantError("invalid_receipt_time")


def _native(value):
    if not isinstance(value, str) or not value.strip():
        raise OutboxInvariantError("invalid_receipt_identifier")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise OutboxInvariantError("invalid_receipt_identifier")
    try:
        valid = 1 <= len(value.encode("utf-8")) <= 256
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise OutboxInvariantError("invalid_receipt_identifier")
    return value


def _receipt_digest(attempt_id, account_key, endpoint_key, kind, native, ref_idx):
    return digest(stable_json({"v": 1, "attempt_id": attempt_id, "account_key": account_key,
                              "endpoint_key": endpoint_key, "kind": kind, "id": native, "ref_idx": ref_idx}))


def record_receipt_evidence(cursor, *, reservation, body, now, kind="response") -> str:
    """Capture only a trusted transport result for a locally issued attempt.

    ``verified_echo`` is an internal port: its caller must already establish a
    unique attempt association from verified platform evidence, never matching
    message text/time. This function itself performs no heuristic echo lookup.
    It is valid after lease loss, and has no account/endpoint authority writes.
    """
    require_issued(reservation)
    _time(now)
    if not isinstance(kind, str) or kind not in {"response", "verified_echo"} or not isinstance(body, dict):
        raise OutboxInvariantError("invalid_receipt_body")
    if not isinstance(reservation.endpoint, EndpointKey) or reservation.endpoint.account.platform != "qq-official":
        raise OutboxInvariantError("receipt_attempt_identity_conflict")
    for name in ("code", "err_code"):
        if name in body and not ((type(body[name]) is int and body[name] == 0) or body[name] == "0"):
            raise OutboxInvariantError("receipt_contains_platform_error")
    native = _native(body.get("id"))
    extra = body.get("ext_info")
    if extra is not None and not isinstance(extra, dict):
        raise OutboxInvariantError("invalid_receipt_reference")
    ref_idx = None if extra is None or extra.get("ref_idx") is None else _native(extra["ref_idx"])
    attempt = cursor.execute("SELECT * FROM delivery_attempts WHERE attempt_id=?", (reservation.attempt_id,)).fetchone()
    if attempt is None or (
        attempt["delivery_id"] != reservation.delivery_id
        or attempt["account_key"] != reservation.endpoint.account.key
        or attempt["endpoint_key"] != reservation.endpoint.key
        or attempt["attempt"] != reservation.attempt_number
        or attempt["fencing_epoch"] != reservation.fencing_epoch
        or attempt["payload_digest"] != reservation.payload_digest
        or attempt["payload_mode"] != reservation.payload_mode
        or attempt["send_by_at"] != reservation.send_by_at
        or (attempt["payload_mode"] == "persistent" and (
            attempt["wire_payload_json"] != reservation.wire_json
            or digest(reservation.wire_json) != reservation.payload_digest))
        or (attempt["payload_mode"] == "ephemeral" and attempt["wire_payload_json"] is not None)
    ):
        raise OutboxInvariantError("receipt_attempt_identity_conflict")
    # Only these minimal fields are hashed/stored. Remote text, URLs, headers,
    # echoed OTPs and arbitrary response fields never enter the evidence row.
    receipt_digest = _receipt_digest(reservation.attempt_id, attempt["account_key"], attempt["endpoint_key"], kind, native, ref_idx)
    candidate = secrets.token_hex(16)
    cursor.execute("""INSERT INTO delivery_receipts
        (receipt_id,attempt_id,account_key,endpoint_key,kind,native_message_id,ref_idx,receipt_digest,observed_at)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(attempt_id,kind,native_message_id) DO NOTHING""",
        (candidate, reservation.attempt_id, attempt["account_key"], attempt["endpoint_key"],
         kind, native, ref_idx, receipt_digest, now))
    stored = cursor.execute("""SELECT * FROM delivery_receipts
        WHERE attempt_id=? AND kind=? AND native_message_id=?""", (reservation.attempt_id, kind, native)).fetchone()
    if stored is None or stored["account_key"] != attempt["account_key"] or stored["endpoint_key"] != attempt["endpoint_key"]:
        raise OutboxInvariantError("receipt_storage_identity_conflict")
    if stored["ref_idx"] != ref_idx or stored["receipt_digest"] != receipt_digest:
        raise OutboxInvariantError("receipt_reference_conflict")
    return str(stored["receipt_id"])


def _author(attempt, endpoint):
    if attempt["bot_actor_key"] is None:
        return None
    try:
        actor = ActorKey.parse(attempt["bot_actor_key"])
        _native(actor.native_user_id)
    except (TypeError, ValueError, AttributeError, OutboxInvariantError):
        return None
    if (actor.account != endpoint.account or actor.identity_kind != "bot" or actor.identity_namespace
            or attempt["bot_identity_source"] not in {"ready", "self_api", "ready_and_self_api"}
            or type(attempt["bot_identity_revision"]) is not int or attempt["bot_identity_revision"] < 1):
        return None
    return actor


def _canonical_body(delivery):
    if delivery["payload_mode"] == "ephemeral":
        if delivery["purpose"] != "binding_otp" or delivery["body_json"] != '{"v":1,"nodes":[]}':
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
    decision = cursor.execute("""SELECT reply_kind,action,decision_code,body_digest
        FROM qq_official_control_decisions WHERE control_id=? AND account_key=? AND endpoint_key=?
          AND requester_actor_key=? AND plan_id=? AND delivery_id=? AND attempt_id=?""",
        (control["control_id"], delivery["account_key"], delivery["endpoint_key"], delivery["requester_actor_key"],
         delivery["plan_id"], delivery["delivery_id"], attempt["attempt_id"])).fetchone()
    if decision is None or decision["body_digest"] != digest(body_to_json(body)):
        raise OutboxInvariantError("control_projection_integrity_conflict")
    if decision["reply_kind"] != "whoami":
        return body, {}
    if decision["action"] != "whoami" or decision["decision_code"] != "identity":
        raise OutboxInvariantError("control_projection_integrity_conflict")
    return MessageBody((TextNode(0, "[身份查询已回复：具体平台身份标识已从会话上下文隐去，仅保留于受保护发送审计。]"),)), {
        "content_redacted": True, "redaction_reason": "qq_official_whoami_identity", "control_kind": "whoami",
    }


def _quote(cursor, attempt, delivery, endpoint):
    if attempt["payload_mode"] != "persistent":
        return None, None
    try:
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


def _wait(cursor, delivery, reason, now):
    cursor.execute("UPDATE deliveries SET wait_reason=?,updated_at=? WHERE delivery_id=? AND status='accepted'",
                   (reason, now, delivery["delivery_id"]))
    return SendOutcome("accepted", reason, delivery["delivery_id"])


def _existing_message(cursor, endpoint, native, delivery, attempt, actor, body, reply_target):
    """Ledger idempotency may reuse only a record with our exact send provenance."""
    rows = cursor.execute("""SELECT m.* FROM messages m JOIN conversations c USING(conversation_id)
        WHERE c.scope_key=? AND m.source_endpoint_key=? AND m.native_message_id=?""",
        (endpoint.scope.key, endpoint.key, native)).fetchall()
    if len(rows) > 1:
        return None, True
    existing = rows[0] if rows else None
    if existing is not None:
        try:
            provenance = json.loads(existing["raw_event_json"])
            equal_body = json.loads(existing["body_json"]) == json.loads(body_to_json(body))
        except (TypeError, ValueError):
            return existing, True
        if (existing["direction"] != "outbound" or existing["sender_actor_key"] != actor.key
                or existing["sender_native_user_id"] != actor.native_user_id or not equal_body
                or (existing["reply_to_native_message_id"] or None) not in {None, delivery["reply_to_native_message_id"] or None}
                or (delivery["status"] != "committed" and (existing["reply_to_native_message_id"] or None) != reply_target)
                or not isinstance(provenance, dict) or provenance.get("source") != "qq-official"
                or provenance.get("delivery_id") != delivery["delivery_id"]
                or provenance.get("attempt_id") != attempt["attempt_id"]):
            return existing, True
    mapping = cursor.execute("""SELECT * FROM qq_official_messages
        WHERE account_key=? AND endpoint_key=? AND native_message_id=?""",
        (endpoint.account.key, endpoint.key, native)).fetchone()
    if mapping is not None and (mapping["direction"] != "outbound" or existing is None
                               or mapping["canonical_message_id"] != existing["canonical_message_id"]):
        return existing, True
    return existing, False


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


def finalize_receipt(cursor, *, token, receipt_id, ledger, journal=None, now) -> SendOutcome:
    """Apply an accepted historical fact; current binding is not send authority.

    Receipt, quota, ledger, official mapping/index and strict journal association
    settle in this one caller-owned transaction. Exceptions require rollback.
    """
    _time(now)
    if not isinstance(receipt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", receipt_id):
        raise OutboxInvariantError("invalid_receipt_handle")
    assert_epoch(cursor, token)
    receipt = cursor.execute("SELECT * FROM delivery_receipts WHERE receipt_id=? AND account_key=?",
                             (receipt_id, token.account_key)).fetchone()
    if receipt is None:
        return SendOutcome("unavailable", "receipt_not_found")
    endpoint = EndpointKey.parse(receipt["endpoint_key"])
    if endpoint.account.key != token.account_key:
        raise OutboxInvariantError("receipt_namespace_conflict")
    endpoint_row = cursor.execute("""SELECT endpoint_key FROM qq_official_endpoints
        WHERE endpoint_key=? AND account_key=? FOR UPDATE""", (endpoint.key, token.account_key)).fetchone()
    if endpoint_row is None:
        raise OutboxInvariantError("receipt_endpoint_missing")
    attempt = cursor.execute("SELECT * FROM delivery_attempts WHERE attempt_id=? AND account_key=?",
                             (receipt["attempt_id"], token.account_key)).fetchone()
    if attempt is None or attempt["endpoint_key"] != endpoint.key:
        raise OutboxInvariantError("receipt_attempt_namespace_conflict")
    delivery = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=? AND account_key=? FOR UPDATE",
                              (attempt["delivery_id"], token.account_key)).fetchone()
    if delivery is None or (
        delivery["endpoint_key"] != endpoint.key or delivery["target_platform"] != endpoint.account.platform
        or delivery["target_kind"] != endpoint.kind or delivery["target_native_conversation_id"] != endpoint.native_conversation_id
        or delivery["endpoint_generation"] != attempt["endpoint_generation"]
        or delivery["route_epoch"] != attempt["route_epoch"] or delivery["payload_mode"] != attempt["payload_mode"]
    ):
        raise OutboxInvariantError("receipt_delivery_namespace_conflict")
    if (delivery["current_attempt_id"] != attempt["attempt_id"] or delivery["claim_epoch"] != attempt["fencing_epoch"]
            or delivery["attempts"] != attempt["attempt"]):
        return SendOutcome("ambiguous", "attempt_pointer_conflict", delivery["delivery_id"])
    if attempt["state"] in {"confirmed_unaccepted", "cancelled_unsent"}:
        return SendOutcome("ambiguous", "attempt_acceptance_conflict", delivery["delivery_id"])
    receipts = cursor.execute("SELECT * FROM delivery_receipts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchall()
    for evidence in receipts:
        _native(evidence["native_message_id"])
        if evidence["ref_idx"] is not None:
            _native(evidence["ref_idx"])
        if (evidence["account_key"] != token.account_key or evidence["endpoint_key"] != endpoint.key
                or evidence["receipt_digest"] != _receipt_digest(attempt["attempt_id"], token.account_key,
                    endpoint.key, evidence["kind"], evidence["native_message_id"], evidence["ref_idx"])):
            raise OutboxInvariantError("receipt_integrity_conflict")
    if attempt["payload_mode"] == "persistent" and digest(attempt["wire_payload_json"]) != attempt["payload_digest"]:
        raise OutboxInvariantError("attempt_payload_integrity_conflict")
    native_ids = {row["native_message_id"] for row in receipts}
    refs = {row["ref_idx"] for row in receipts if row["ref_idx"] is not None}
    conflict_reason = "receipt_identity_conflict" if len(native_ids) != 1 else "receipt_reference_conflict" if len(refs) > 1 else ""
    if delivery["status"] == "committed" and conflict_reason:
        return SendOutcome("ambiguous", conflict_reason, delivery["delivery_id"])
    native = receipt["native_message_id"]
    if delivery["native_message_id"] and delivery["native_message_id"] not in native_ids:
        return SendOutcome("ambiguous", "delivery_receipt_identity_conflict", delivery["delivery_id"])
    if conflict_reason and delivery["native_message_id"]:
        native = delivery["native_message_id"]
    settle_send_policy(cursor, token=token, attempt_id=attempt["attempt_id"], result="accepted", now=now)
    if delivery["status"] != "committed":
        cursor.execute("""UPDATE delivery_attempts SET state='accepted',acceptance='confirmed_yes',
            finished_at=COALESCE(finished_at,?) WHERE attempt_id=? AND account_key=?""",
            (now, attempt["attempt_id"], token.account_key))
        cursor.execute("""UPDATE deliveries SET status='accepted',native_message_id=?,lease_until=NULL,
            wait_reason=NULL,last_error='',updated_at=? WHERE delivery_id=? AND current_attempt_id=? AND claim_epoch=?""",
            (native, now, delivery["delivery_id"], attempt["attempt_id"], attempt["fencing_epoch"]))
        if cursor.rowcount != 1:
            raise OutboxInvariantError("receipt_attempt_changed")
    if conflict_reason:
        return _wait(cursor, delivery, conflict_reason, now)
    actor = _author(attempt, endpoint)
    if actor is None:
        if delivery["status"] == "committed":
            raise OutboxInvariantError("committed_author_missing")
        return _wait(cursor, delivery, "identity_unverified", now)
    if delivery["turn_id"] is not None and journal is None and delivery["status"] != "committed":
        return _wait(cursor, delivery, "turn_journal_unavailable", now)
    body, projection_metadata = _context_projection(cursor, delivery, attempt)
    reply_target, quoted_index = _quote(cursor, attempt, delivery, endpoint)
    existing, collision = _existing_message(cursor, endpoint, native, delivery, attempt, actor, body, reply_target)
    if collision:
        if delivery["status"] == "committed":
            raise OutboxInvariantError("committed_message_identity_conflict")
        return _wait(cursor, delivery, "native_message_identity_conflict", now)
    if existing is not None and delivery["status"] == "committed":
        # Later index collisions do not rewrite an already committed historical
        # quote, and a newly learned index cannot invent a quote on replay.
        reply_target = existing["reply_to_native_message_id"] or None
    other = cursor.execute("""SELECT 1 FROM deliveries WHERE account_key=? AND endpoint_key=?
        AND delivery_id<>? AND native_message_id=? AND status IN ('accepted','committed') LIMIT 1""",
        (token.account_key, endpoint.key, delivery["delivery_id"], native)).fetchone()
    if other is not None:
        if delivery["status"] == "committed":
            return SendOutcome("ambiguous", "native_message_identity_conflict", delivery["delivery_id"])
        return _wait(cursor, delivery, "native_message_identity_conflict", now)
    stored = ledger.record_message(
        endpoint.scope, native_message_id=native, sender_native_user_id=actor.native_user_id,
        sender_display="Bot", body=body, occurred_at=receipt["accepted_at"] or receipt["observed_at"],
        direction="outbound", message_kind="command" if delivery["payload_mode"] == "ephemeral" or projection_metadata else "chat",
        reply_to_native_message_id=reply_target, actor=actor, source_endpoint=endpoint,
        raw_event={"source": "qq-official", "delivery_id": delivery["delivery_id"],
                   "attempt_id": attempt["attempt_id"], "receipt_id": receipt_id, "time_source": "receipt_observed",
                   **projection_metadata},
        transaction_cursor=cursor,
    )
    canonical = stored.canonical_message_id
    cursor.execute("""INSERT INTO qq_official_messages
        (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,direction,ref_msg_idx,created_at)
        VALUES (?,?,?,?,?,'outbound',?,?) ON CONFLICT(endpoint_key,native_message_id) DO NOTHING""",
        (secrets.token_hex(16), token.account_key, endpoint.key, native, canonical, quoted_index, now))
    index_conflict = _record_reference(cursor, endpoint, native, canonical, next(iter(refs)) if refs else None, now)
    if delivery["turn_id"] is not None:
        if journal is not None:
            journal.link_send(delivery["turn_id"], canonical, node_id=f"outbox:{delivery['delivery_id']}",
                              chunk_index=delivery["part_index"], transaction_cursor=cursor, strict_association=True)
        else:
            # A committed replay need not recreate a journal service, but it
            # must still prove the exact association already committed.
            link = cursor.execute("""SELECT turn_id,node_id,chunk_index FROM turn_send_links
                WHERE canonical_message_id=? FOR UPDATE""", (canonical,)).fetchone()
            if link is None or (link["turn_id"], link["node_id"], link["chunk_index"]) != (
                    delivery["turn_id"], f"outbox:{delivery['delivery_id']}", delivery["part_index"]):
                raise OutboxInvariantError("committed_journal_identity_conflict")
    cursor.execute("""UPDATE delivery_attempts SET state='committed',acceptance='confirmed_yes',
        finished_at=COALESCE(finished_at,?) WHERE attempt_id=? AND account_key=?""",
        (now, attempt["attempt_id"], token.account_key))
    cursor.execute("""UPDATE deliveries SET status='committed',native_message_id=?,lease_until=NULL,
        wait_reason=NULL,last_error='',updated_at=? WHERE delivery_id=? AND current_attempt_id=? AND claim_epoch=?""",
        (native, now, delivery["delivery_id"], attempt["attempt_id"], attempt["fencing_epoch"]))
    if cursor.rowcount != 1:
        raise OutboxInvariantError("receipt_attempt_changed")
    cursor.execute("""UPDATE delivery_receipts SET applied_at=COALESCE(applied_at,?)
        WHERE attempt_id=? AND account_key=? AND native_message_id=?""",
        (max(now, max(row["observed_at"] for row in receipts)), attempt["attempt_id"], token.account_key, native))
    return SendOutcome("committed", "reference_index_ambiguous" if index_conflict else "", delivery["delivery_id"])
