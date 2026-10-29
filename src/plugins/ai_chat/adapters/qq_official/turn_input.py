"""Reconstruct admitted input from durable source evidence, not a fresh event.

Caller owns the transaction and both leases. No raw payload or model argument
can choose the identity, history route, body or reply credential here.
"""
from __future__ import annotations

import json

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import body_from_json, body_to_json
from src.plugins.ai_chat.platform_types import InboundMessage, ReplyCredential


class TurnInputError(RuntimeError):
    """Fixed diagnostic only."""


def _current_reply(cursor, handle, message):
    expected = message["reply_to_native_message_id"]
    if not expected:
        return None
    source = cursor.execute("""SELECT ref_msg_idx FROM qq_official_messages
        WHERE account_key=? AND endpoint_key=? AND native_message_id=?
          AND canonical_message_id=? AND direction='inbound'""",
        (handle.endpoint.account.key, handle.endpoint.key, message["native_message_id"],
         handle.canonical_message_id)).fetchone()
    if source is None or not source["ref_msg_idx"]:
        return None
    targets = cursor.execute("""SELECT i.native_message_id,i.canonical_message_id,i.ambiguous,
        q.canonical_message_id AS mapped_id,m.source_endpoint_key,m.native_message_id AS ledger_native
        FROM qq_official_message_indexes i LEFT JOIN qq_official_messages q
          ON q.account_key=i.account_key AND q.endpoint_key=i.endpoint_key AND q.native_message_id=i.native_message_id
        LEFT JOIN messages m ON m.canonical_message_id=i.canonical_message_id
        WHERE i.account_key=? AND i.endpoint_key=? AND i.index_value=? AND i.index_kind IN ('msg_idx','ref_idx')""",
        (handle.endpoint.account.key, handle.endpoint.key, source["ref_msg_idx"])).fetchall()
    if not targets or any(row["ambiguous"] or row["native_message_id"] != expected
        or row["canonical_message_id"] is None or row["mapped_id"] != row["canonical_message_id"]
        or row["source_endpoint_key"] != handle.endpoint.key or row["ledger_native"] != expected for row in targets):
        return None
    return expected


def load_turn_input(cursor, *, execution_api, token, handle, job, now, admission):
    execution_api.assert_execution_current(cursor, token=token, handle=handle,
        job=job, now=now, admission=admission)
    row = cursor.execute("""SELECT m.*,i.event_id,i.received_at,i.trigger_requested
        FROM messages m JOIN qq_official_inbound_events i ON i.canonical_message_id=m.canonical_message_id
        WHERE i.inbox_id=? AND i.account_key=? AND i.endpoint_key=? AND i.dispatch_job_id=?
          AND m.canonical_message_id=?""", (handle.inbox_id, token.account_key,
        handle.endpoint.key, job.job_id, handle.canonical_message_id)).fetchone()
    if row is None:
        raise TurnInputError("turn_source_unavailable")
    try:
        body = body_from_json(row["body_json"])
        if json.loads(body_to_json(body)) != json.loads(row["body_json"]):
            raise ValueError
        scope = ConversationScope.from_key(handle.scope_key)
    except (TypeError, ValueError):
        raise TurnInputError("turn_source_body_invalid") from None
    # Revalidate references even on an explicit @ turn: a later conflicting
    # index must not introduce a wrong quoted context or journal continuation.
    # Credential values always come from the original persisted window, even
    # when expired; M3 independently checks and reserves final send authority.
    window = cursor.execute("""SELECT * FROM qq_official_reply_windows
        WHERE account_key=? AND endpoint_key=? AND endpoint_generation=? AND route_epoch=?
          AND credential_kind='msg_id' AND credential_value=? AND state IN ('active','expired')""",
        (token.account_key, handle.endpoint.key, handle.endpoint_generation,
         handle.route_epoch, row["native_message_id"])).fetchone()
    credential = None
    if window is not None:
        credential = ReplyCredential(handle.endpoint, window["credential_kind"], window["credential_value"],
            window["issued_at"], window["received_at"], window["expires_at"], window["max_replies"])
    return InboundMessage(endpoint=handle.endpoint, actor=handle.actor, event_id=row["event_id"],
        native_message_id=row["native_message_id"], sender_display=row["sender_display"],
        platform_role="", body=body, mentions_bot=bool(row["trigger_requested"]),
        reply_to_native_message_id=_current_reply(cursor, handle, row),
        occurred_at=row["occurred_at"], reply_credential=credential,
        canonical_scope=scope if scope != handle.endpoint.scope else None,
        route_epoch=handle.route_epoch)
