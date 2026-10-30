"""Transaction-B ingestion: durable evidence to ledger and a unique turn intent.

The caller owns the cursor and its commit/rollback. This module never sends,
invokes a model, starts an in-memory task, or opens a second transaction.
The turn job is an intent only; M4 must supply journal/checkpoint idempotency
before connecting it to the shared application turn.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import time
from typing import Callable

from src.bot_storage import StoreCursor, StoreRow
from src.qq_official.events import EventParseError, parse_dispatch
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, MentionNode, TextNode, fallback_text
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore

from .codec import DecodedMessage, RejectedMessage, decode_message
from ...conversation_scope import ConversationScope
from .lease import LeaseToken
from .input_normalization import normalize_input


Admission = Callable[[StoreCursor, EndpointKey, ActorKey], bool]
EpochGuard = Callable[[StoreCursor, LeaseToken], None]
WINDOW_MARGIN_SECONDS = 5


@dataclass(frozen=True)
class IngestResult:
    status: str
    reason_code: str = ""
    canonical_message_id: int | None = None
    dispatch_job_id: int | None = None
    echo_state: str = "unknown"
    reply_requested: bool = False


class IngestInvariantError(RuntimeError):
    """Fixed internal failure; transaction owner must roll back."""


def _queue_key(domain: str, *parts: str) -> str:
    encoded = json.dumps([domain, *parts], ensure_ascii=True, separators=(",", ":")).encode()
    return domain + ":" + hashlib.sha256(encoded).hexdigest()


def turn_dispatch_key(account_key: str, endpoint_key: str, inbox_id: str) -> str:
    return _queue_key("qq.turn", account_key, endpoint_key, inbox_id)


def turn_dispatch_lane(account_key: str, endpoint_key: str) -> str:
    return _queue_key("qq.turn.lane", account_key, endpoint_key)


def _result(row: StoreRow) -> IngestResult:
    return IngestResult(str(row["status"]), str(row["reason_code"] or ""),
                        row["canonical_message_id"], row["dispatch_job_id"], str(row["echo_state"]), bool(row["trigger_requested"]))


def _finish(cursor, row, now, *, status="processed", reason="", canonical=None, job=None, echo="not_echo"):
    cursor.execute("""UPDATE qq_official_inbound_events SET status=?,reason_code=?,
        canonical_message_id=?,dispatch_job_id=?,echo_state=?,processed_at=?,updated_at=?
        WHERE inbox_id=? AND account_key=?""",
        (status, reason or None, canonical, job, echo, now, now, row["inbox_id"], row["account_key"]))
    return IngestResult(status, reason, canonical, job, echo)


def _reply_target(cursor: StoreCursor, decoded: DecodedMessage) -> str | None:
    reference = decoded.reference
    if reference.conflicting or reference.referenced_index is None:
        return None
    rows = cursor.execute("""SELECT i.native_message_id,i.canonical_message_id,i.ambiguous,
            m.canonical_message_id AS mapped_canonical,
            c.native_message_id AS ledger_native,c.source_endpoint_key,c.direction,
            v.scope_key
        FROM qq_official_message_indexes i JOIN qq_official_messages m
          ON m.endpoint_key=i.endpoint_key AND m.native_message_id=i.native_message_id
            AND m.account_key=i.account_key
        LEFT JOIN messages c ON c.canonical_message_id=i.canonical_message_id
        LEFT JOIN conversations v ON v.conversation_id=c.conversation_id
        WHERE i.account_key=? AND i.endpoint_key=? AND i.index_value=?
          AND i.index_kind IN ('msg_idx','ref_idx')""",
        (decoded.endpoint.account.key, decoded.endpoint.key, reference.referenced_index)).fetchall()
    targets = set()
    for row in rows:
        if row["ambiguous"] or row["canonical_message_id"] != row["mapped_canonical"]:
            return None
        if row["canonical_message_id"] is not None and (
            row["source_endpoint_key"] != decoded.endpoint.key
            or row["scope_key"] != cursor.execute("SELECT canonical_scope_key FROM qq_official_endpoints WHERE endpoint_key=?", (decoded.endpoint.key,)).fetchone()["canonical_scope_key"]
            or row["ledger_native"] != row["native_message_id"]
        ):
            return None
        targets.add((str(row["native_message_id"]), row["canonical_message_id"]))
    return next(iter(targets))[0] if len(targets) == 1 else None


def _record_index(cursor: StoreCursor, decoded: DecodedMessage, canonical: int, now: int):
    native_index = decoded.reference.message_index
    if decoded.reference.conflicting or native_index is None:
        return
    cursor.execute("""INSERT INTO qq_official_message_indexes
        (endpoint_key,account_key,index_kind,index_value,native_message_id,
         canonical_message_id,ambiguous,created_at,updated_at)
        VALUES (?,?,'msg_idx',?,?,?,FALSE,?,?)
        ON CONFLICT(endpoint_key,index_kind,index_value) DO UPDATE SET
            ambiguous=qq_official_message_indexes.ambiguous
                OR qq_official_message_indexes.native_message_id<>EXCLUDED.native_message_id
                OR (qq_official_message_indexes.canonical_message_id IS NOT NULL
                    AND qq_official_message_indexes.canonical_message_id<>EXCLUDED.canonical_message_id),
            updated_at=EXCLUDED.updated_at""",
        (decoded.endpoint.key, decoded.endpoint.account.key, native_index, decoded.native_message_id,
         canonical, now, now))


def observe_reply_window(cursor: StoreCursor, decoded: DecodedMessage, endpoint_row: StoreRow, now: int) -> None:
    """Observe only reply metadata in the caller's admitted transaction.

    Caller must fence and authorize the ordinary message or limited control
    command and provide its current actual endpoint row. Unbound ordinary chat
    must never call this helper. No body, binding code, or display name is stored;
    replay preserves the first observation and all allocation counters.
    """
    if decoded.timestamp_source != "platform" or decoded.platform_occurred_at is None:
        return
    seconds, limit = (300, 5) if decoded.endpoint.kind == "group" else (3600, 4)
    issued = decoded.platform_occurred_at
    expires = min(issued, decoded.received_at) + seconds - WINDOW_MARGIN_SECONDS
    cursor.execute("""INSERT INTO qq_official_reply_windows
        (window_id,account_key,endpoint_key,endpoint_generation,route_epoch,
         credential_kind,credential_value,issued_at,received_at,expires_at,
         max_replies,state,created_at,updated_at)
        VALUES (?,?,?,?,?,'msg_id',?,?,?,?,?,?,?,?)
        ON CONFLICT(endpoint_key,credential_kind,credential_value) DO NOTHING""",
        (secrets.token_hex(16), decoded.endpoint.account.key, decoded.endpoint.key,
         endpoint_row["generation"], endpoint_row["route_epoch"], decoded.native_message_id,
         issued, decoded.received_at, expires, limit, "active" if expires > now else "expired", now, now))


def _ledger_body(decoded: DecodedMessage) -> MessageBody:
    # The shared ledger resolves MentionNode into human principals. A provider
    # bot mention does not establish a human group identity or our own bot ID.
    return MessageBody(tuple(
        TextNode(node.segment_index, fallback_text(node))
        if isinstance(node, MentionNode) and (node.raw_data.get("is_you") is True or node.raw_data.get("bot") is True)
        else node for node in decoded.body.nodes
    ))


def process_inbox(
    cursor: StoreCursor, *, inbox_id: str, lease_token: LeaseToken,
    ledger: MessageLedger, jobs: DurableJobStore, epoch_guard: EpochGuard,
    admission: Admission | None = None, now: int | None = None,
) -> IngestResult:
    """Atomically ingest one accepted message using the caller's transaction.

    ``admission(cursor, endpoint, actor)`` rechecks current policy; no callback
    means deny. It must be synchronous and must not commit, open transactions,
    or perform network I/O. The epoch guard acquires the account row first.
    """
    if not isinstance(inbox_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", inbox_id):
        raise ValueError("invalid inbox identifier")
    timestamp = int(time.time()) if now is None else now
    if type(timestamp) is not int or not 0 <= timestamp <= (1 << 63) - 1:
        raise ValueError("invalid ingestion time")
    epoch_guard(cursor, lease_token)
    account = AccountKey.parse(lease_token.account_key)
    row = cursor.execute("SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND account_key=?",
                         (inbox_id, account.key)).fetchone()
    if row is None:
        return IngestResult("missing", "inbox_not_found")
    if row["status"] != "pending":
        return _result(row)
    if getattr(admission, 'lock_inbox', None) is not None:
        from .migration_types import MigrationDenied
        try:
            admission.lock_inbox(cursor, inbox_id, operation='ingest')
        except MigrationDenied:
            return _finish(cursor, row, timestamp, status='revoked', reason='migration_source_unavailable')
    # Lock the actual endpoint before the inbox, matching the control reducers.
    endpoint_row = cursor.execute("""SELECT * FROM qq_official_endpoints
        WHERE endpoint_key=? AND account_key=? FOR UPDATE""", (row["endpoint_key"], account.key)).fetchone()
    row = cursor.execute("""SELECT * FROM qq_official_inbound_events
        WHERE inbox_id=? AND account_key=? FOR UPDATE""", (inbox_id, account.key)).fetchone()
    if row["status"] != "pending":
        return _result(row)
    if endpoint_row is None:
        return _finish(cursor, row, timestamp, status="quarantined", reason="missing_endpoint")
    try:
        endpoint = EndpointKey.parse(str(row["endpoint_key"]))
    except (ValueError, TypeError):
        return _finish(cursor, row, timestamp, status="quarantined", reason="invalid_endpoint")
    if (endpoint.account != account or endpoint.kind != endpoint_row["kind"]
            or endpoint.native_conversation_id != endpoint_row["native_conversation_id"]
):
        return _finish(cursor, row, timestamp, status="quarantined", reason="endpoint_identity_mismatch")
    if (row["endpoint_generation"] != endpoint_row["generation"]
            or row["route_epoch"] != endpoint_row["route_epoch"]
            or endpoint_row["binding_state"] == "revoked" or endpoint_row["membership_state"] == "absent"
            or (endpoint.kind == "group" and (endpoint_row["binding_state"] != "active"
                                              or endpoint_row["membership_state"] != "present"))):
        return _finish(cursor, row, timestamp, status="revoked", reason="endpoint_no_longer_admitted")
    if row["payload_expires_at"] is not None and row["payload_expires_at"] <= timestamp:
        return _finish(cursor, row, timestamp, status="skipped", reason="payload_expired")
    try:
        dispatch = parse_dispatch(row["payload_json"])
        decoded = decode_message(dispatch, account, row["received_at"])
    except (EventParseError, ValueError, TypeError):
        return _finish(cursor, row, timestamp, status="quarantined", reason="invalid_payload")
    if isinstance(decoded, RejectedMessage) and decoded.disposition != "bot_origin":
        return _finish(cursor, row, timestamp, status="quarantined", reason=decoded.reason)
    if (decoded.endpoint != endpoint or decoded.native_message_id != row["native_message_id"]
            or decoded.event_id != row["event_id"] or dispatch.event_type != row["event_type"]):
        return _finish(cursor, row, timestamp, status="quarantined", reason="payload_identity_mismatch")
    existing = cursor.execute("""SELECT * FROM qq_official_messages
        WHERE account_key=? AND endpoint_key=? AND native_message_id=?""",
        (account.key, endpoint.key, decoded.native_message_id)).fetchone()
    if existing is not None and existing["direction"] == "outbound":
        return _finish(cursor, row, timestamp, reason="outbound_echo", echo="confirmed_self")
    if isinstance(decoded, RejectedMessage):
        return _finish(cursor, row, timestamp, reason="bot_origin", echo="possible_self")
    # A verified global bot ID is not a group-member or C2C-user identifier.
    # Do not compare naked values across those identity domains. Future self
    # actor mappings require a full ActorKey and independently verified proof.
    if admission is None or admission(cursor, endpoint, decoded.actor) is not True:
        return _finish(cursor, row, timestamp, status="revoked", reason="policy_denied")
    require_source = getattr(admission, 'require_inbox', None)
    if require_source is not None:
        from .migration_types import MigrationDenied
        try:
            require_source(cursor, inbox_id, endpoint=endpoint, actor=decoded.actor, operation='ingest')
        except MigrationDenied:
            return _finish(cursor, row, timestamp, status='revoked', reason='migration_source_unavailable')
    if existing is not None:
        return _finish(cursor, row, timestamp, reason="duplicate_message", canonical=existing["canonical_message_id"])
    prepared_input = normalize_input(dispatch)
    from .approval import prepare_approval_input
    if prepared_input.kind != "message" or prepare_approval_input(dispatch, endpoint).kind != "none":
        # Old/incorrect TxA admission must still never put a binding secret into
        # canonical history or leave its raw payload behind for later workers.
        cursor.execute("UPDATE qq_official_inbound_events SET payload_json=NULL WHERE inbox_id=?", (inbox_id,))
        return _finish(cursor, row, timestamp, status="quarantined", reason="input_sensitive_control")
    cursor.execute("""UPDATE qq_official_inbound_events SET turn_text=?,turn_text_version=?
        WHERE inbox_id=? AND account_key=?""",
        (prepared_input.input_text, prepared_input.version, inbox_id, account.key))
    reply_target = _reply_target(cursor, decoded)
    recorded = ledger.record_message(
        ConversationScope.from_key(endpoint_row["canonical_scope_key"]), native_message_id=decoded.native_message_id,
        sender_native_user_id=decoded.actor.native_user_id, sender_display=decoded.sender_display,
        body=_ledger_body(decoded), occurred_at=decoded.occurred_at,
        reply_to_native_message_id=reply_target, actor=decoded.actor, source_endpoint=endpoint,
        raw_event={"source": "qq-official", "message_type": decoded.evidence.message_type,
                   "reference_conflict": decoded.reference.conflicting,
                   "codec_warnings": list(decoded.evidence.warnings), "truncated": decoded.evidence.truncated},
        transaction_cursor=cursor,
    )
    canonical = recorded.canonical_message_id
    cursor.execute("""INSERT INTO qq_official_messages
        (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,
         direction,msg_idx,ref_msg_idx,inbox_id,created_at)
        VALUES (?,?,?,?,?,'inbound',?,?,?,?)""",
        (secrets.token_hex(16), account.key, endpoint.key, decoded.native_message_id, canonical,
         decoded.reference.message_index, decoded.reference.referenced_index, inbox_id, timestamp))
    _record_index(cursor, decoded, canonical, timestamp)
    observe_reply_window(cursor, decoded, endpoint_row, timestamp)
    reply = endpoint.kind == 'private' or row['trigger_requested'] or decoded.mentions_bot or reply_target is not None
    cursor.execute('UPDATE qq_official_inbound_events SET trigger_requested=? WHERE inbox_id=?', (bool(reply), inbox_id))
    result = _finish(cursor, row, timestamp, canonical=canonical)
    return IngestResult(result.status, result.reason_code, canonical, echo_state=result.echo_state, reply_requested=reply)
