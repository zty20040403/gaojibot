"""Fenced outcome transitions. Unknown outcomes are never automatic retries."""
from __future__ import annotations

import math
import re

from src.qq_official.errors import QQAPIError
from .control_receipts import control_id_for_delivery, sync_control_terminal
from .error_policy import decide_send_error
from .format_fallback import decide_format_fallback
from .protected_plans import verify_protected_attempt
from .lease import assert_epoch
from .outbox_types import OutboxInvariantError, SendOutcome, fixed_reason, require_issued
from .send_policy import apply_policy_rejection, extend_server_cooldown, settle_send_policy


def _current(cursor, token, reservation):
    require_issued(reservation)
    assert_epoch(cursor, token)
    if token.account_key != reservation.endpoint.account.key or token.epoch != reservation.fencing_epoch:
        return None
    row = cursor.execute("""SELECT d.*,a.state AS attempt_state,a.send_mode FROM deliveries d
        JOIN delivery_attempts a ON a.attempt_id=d.current_attempt_id
        WHERE d.delivery_id=? AND d.account_key=? AND d.current_attempt_id=? AND d.claim_epoch=?
          AND a.payload_digest=? AND d.status='sending' AND a.state IN ('reserved','dispatched')
        FOR UPDATE OF d,a""",
        (reservation.delivery_id, token.account_key, reservation.attempt_id, token.epoch, reservation.payload_digest)).fetchone()
    if row is not None and row["payload_mode"] == "protected":
        attempt = cursor.execute("SELECT * FROM delivery_attempts WHERE attempt_id=?", (reservation.attempt_id,)).fetchone()
        verify_protected_attempt(cursor, row, attempt, reservation=reservation)
    return row


def cancel_before_invocation(cursor, *, token, reservation, now, reason="request_not_started"):
    """Internal sender control-flow port; caller proves no HTTP invocation.

    It must never be used merely because dispatched_at is absent after restart.
    The process-local issued handle and its invocation guard are required by the
    OfficialOutbox facade before entering this function.
    """
    fixed_reason(reason)
    row = _current(cursor, token, reservation)
    if row is None:
        return SendOutcome("unavailable", "stale_attempt", reservation.delivery_id)
    settle_send_policy(cursor, token=token, attempt_id=reservation.attempt_id, result="proven_unsent", now=now)
    cursor.execute("""UPDATE delivery_attempts SET state='cancelled_unsent',acceptance='confirmed_no',finished_at=?
        WHERE attempt_id=? AND state IN ('reserved','dispatched')""", (now, reservation.attempt_id))
    state = "cancelled" if row["payload_mode"] == "ephemeral" or row["purpose"] == "progress" or control_id_for_delivery(cursor, row) is not None or row["expires_at"] <= now else "pending"
    cursor.execute("""UPDATE deliveries SET status=?,wait_reason=?,next_attempt_at=?,lease_until=NULL,
        updated_at=? WHERE delivery_id=? AND current_attempt_id=?""",
        (state, reason, now + 1, now, reservation.delivery_id, reservation.attempt_id))
    sync_control_terminal(cursor, token, reservation.delivery_id, now)
    return SendOutcome(state, reason, reservation.delivery_id, now + 1 if state == "pending" else None)


def settle_failure(cursor, *, token, reservation, error, now, frozen_plain_digest=None):
    if not isinstance(error, QQAPIError):
        error = QQAPIError("transport_ambiguous", stage="send")
    row = _current(cursor, token, reservation)
    if row is None:
        return SendOutcome("unavailable", "stale_attempt", reservation.delivery_id)
    if cursor.execute("SELECT 1 FROM delivery_receipts WHERE attempt_id=? LIMIT 1", (reservation.attempt_id,)).fetchone():
        return SendOutcome("accepted", "receipt_needs_finalization", reservation.delivery_id)
    ephemeral = row["payload_mode"] == "ephemeral"
    attempted = "plain_fallback" if row["format_fallback_used"] else ("primary_markdown" if '"msg_type":2' in reservation.wire_json else "primary_text")
    if row["payload_mode"] == "protected":
        frozen_plain_digest = None
    fallback = decide_format_fallback(error, attempted_variant=attempted,
        fallbacks_used=int(row["format_fallback_used"]), frozen_plain_variant_ready=frozen_plain_digest is not None)
    policy = decide_send_error(error, auth_retries_used=row["auth_retries_used"], retry_count=row["transient_retries_used"])
    rejected = fallback.rejection is not None or policy.message_acceptance in {"rejected", "not_sent"}
    unknown = not rejected
    reason = fallback.reason if fallback.rejection is not None else policy.reason
    fixed_reason(reason)
    retry = False
    retry_at = now
    auth_used, transient_used = row["auth_retries_used"], row["transient_retries_used"]
    switch_plain = False
    if not ephemeral and row["purpose"] != "progress" and control_id_for_delivery(cursor, row) is None and not unknown:
        if row["payload_mode"] == "persistent" and fallback.action == "select_frozen_plain_once":
            retry, switch_plain = True, True
        elif policy.action == "refresh_token_once":
            retry, auth_used = True, auth_used + 1
        elif policy.action in {"backoff_unsent", "wait_rate_limit"}:
            retry, transient_used = True, transient_used + 1
            retry_at = now + max(1, math.ceil(policy.delay_seconds or 1))
        elif policy.action in {"close_window_and_reselect", "wait_window"}:
            retry, retry_at = True, now + 1
    state = "ambiguous" if unknown else ("pending" if retry and retry_at < row["expires_at"] else "failed")
    attempt_state = "ambiguous" if unknown else ("cancelled_unsent" if policy.message_acceptance == "not_sent" else "confirmed_unaccepted")
    result = "ambiguous" if unknown else ("proven_unsent" if policy.message_acceptance == "not_sent" else "confirmed_unaccepted")
    settle_send_policy(cursor, token=token, attempt_id=reservation.attempt_id, result=result, now=now)
    if not unknown and policy.action == "close_window_and_reselect":
        apply_policy_rejection(cursor, token=token, attempt_id=reservation.attempt_id, category="window_exhausted", now=now)
    if not unknown and policy.action == "wait_window":
        apply_policy_rejection(cursor, token=token, attempt_id=reservation.attempt_id, category="proactive_denied", now=now)
    if not unknown and policy.action == "wait_rate_limit":
        extend_server_cooldown(cursor, token=token, endpoint=reservation.endpoint, send_mode=row["send_mode"], blocked_until=retry_at, now=now)
    # Store only bounded structured evidence. Remote strings/bodies never enter
    # last_error or attempt detail; OTP schema also enforces empty free text.
    http_status = error.status if type(error.status) is int and 100 <= error.status <= 599 else None
    code = error.code if type(error.code) is int and -(2**63) <= error.code < 2**63 else None
    request_id = error.request_id if isinstance(error.request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", error.request_id) else None
    stage = error.stage if error.stage in {"request", "send", "token", "upload"} else "unknown"
    cursor.execute("""UPDATE delivery_attempts SET state=?,acceptance=?,finished_at=?,failure_category=?,
        failure_stage=?,http_status=?,platform_code=?,request_id=? WHERE attempt_id=?""",
        (attempt_state, "unknown" if unknown else "confirmed_no", now, reason, stage,
         http_status, code, request_id, reservation.attempt_id))
    if switch_plain:
        if not re.fullmatch(r"[a-f0-9]{64}", frozen_plain_digest or ""):
            raise OutboxInvariantError("invalid_plain_variant_digest")
        cursor.execute("UPDATE deliveries SET variant='plain_fallback',format_fallback_used=TRUE,payload_digest=? WHERE delivery_id=?",
                       (frozen_plain_digest, reservation.delivery_id))
    cursor.execute("""UPDATE deliveries SET status=?,wait_reason=?,next_attempt_at=?,lease_until=NULL,
        last_error=?,auth_retries_used=?,transient_retries_used=?,updated_at=?
        WHERE delivery_id=? AND current_attempt_id=?""",
        (state, reason, retry_at, "" if ephemeral else reason, auth_used, transient_used, now,
         reservation.delivery_id, reservation.attempt_id))
    if state == "failed":
        cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason='previous_part_failed',updated_at=?
            WHERE plan_id=? AND part_index>? AND status='pending'""", (now, row["plan_id"], row["part_index"]))
    # Complete account-fenced metadata before this same transaction deliberately
    # pauses authentication. A new assert_epoch after the pause must reject it.
    sync_control_terminal(cursor, token, reservation.delivery_id, now)
    if policy.action in {"pause_auth", "pause_token_preflight"}:
        cursor.execute("""UPDATE qq_official_accounts SET gateway_state='paused',pause_reason=?,updated_at=?
            WHERE account_key=?""", (reason, now, token.account_key))
    return SendOutcome(state, reason, reservation.delivery_id, retry_at if state == "pending" else None)


def recover_account_sends(cursor, *, token, now, live_progress=()):
    """Restart recovery never equates a reserved intent with proof of no send."""
    assert_epoch(cursor, token)
    receipt_ids = tuple(row["receipt_id"] for row in cursor.execute("""SELECT r.receipt_id FROM delivery_receipts r
        WHERE r.account_key=? AND r.applied_at IS NULL ORDER BY r.observed_at,r.receipt_id LIMIT 100""", (token.account_key,)).fetchall())
    rows = cursor.execute("""SELECT a.attempt_id,a.delivery_id FROM delivery_attempts a
        JOIN deliveries d ON d.current_attempt_id=a.attempt_id WHERE a.account_key=? AND a.fencing_epoch<?
          AND a.state IN ('reserved','dispatched') AND d.status='sending'
          AND NOT EXISTS (SELECT 1 FROM delivery_receipts r WHERE r.attempt_id=a.attempt_id)
        FOR UPDATE OF a,d""", (token.account_key, token.epoch)).fetchall()
    for row in rows:
        cursor.execute("UPDATE delivery_attempts SET state='ambiguous',acceptance='unknown',finished_at=? WHERE attempt_id=?", (now, row["attempt_id"]))
        cursor.execute("""UPDATE deliveries SET status='ambiguous',wait_reason='restart_outcome_unknown',lease_until=NULL,
            updated_at=? WHERE delivery_id=? AND current_attempt_id=?""", (now, row["delivery_id"], row["attempt_id"]))
        sync_control_terminal(cursor, token, row["delivery_id"], now)
    cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason='ephemeral_payload_lost',updated_at=?
        WHERE account_key=? AND payload_mode='ephemeral' AND status='pending'""", (now, token.account_key))
    # Current-process emissions may have been queued just before sender.start.
    # A fresh process has no such host capabilities and cancels every unsent
    # progress plan. Unknown/sending rows remain governed by attempt evidence.
    keep = tuple(live_progress)
    extra = " AND plan_id NOT IN (" + ",".join("?" for _ in keep) + ")" if keep else ""
    cursor.execute("UPDATE deliveries SET status='cancelled',wait_reason='ephemeral_payload_lost',updated_at=? "
        "WHERE account_key=? AND purpose='progress' AND status='pending'" + extra,
        (now, token.account_key, *keep))
    cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason='delivery_expired',updated_at=?
        WHERE account_key=? AND status='pending' AND expires_at<=?""", (now, token.account_key, now))
    return receipt_ids
