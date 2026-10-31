"""Reply-targeted continuation context for official turns."""
from __future__ import annotations

from ..platform_types import InboundMessage


def reply_target_turn(app, inbound: InboundMessage):
    if app.message_ledger is None or app.turn_journal is None or inbound.reply_to_native_message_id is None:
        return None
    target = app.message_ledger.canonical_id_for_native(
        inbound.scope, inbound.reply_to_native_message_id, endpoint=inbound.endpoint,
    )
    return app.turn_journal.find_turn_for_reply(inbound.scope, target) if target is not None else None


def current_turn_context(
    app, inbound: InboundMessage, current_turn_id: int, *,
    create_edge: bool = True, include_digest: bool = True,
) -> str:
    previous = reply_target_turn(app, inbound)
    if previous is None or previous.tool_call_count <= 0:
        return ""
    if create_edge:
        app.turn_journal.add_fork_edge(
            current_turn_id, previous.turn_id,
            created_by_principal_id=app.message_ledger.principal_id_for_actor(inbound.actor),
        )
    reference_time = previous.finished_at or previous.started_at
    count, activity = app.message_ledger.activity_since(
        inbound.scope, reference_time, limit=3,
        exclude_native_message_id=inbound.native_message_id,
        exclude_endpoint=inbound.endpoint,
    )
    parts = [
        "[reply-targeted continuation]",
        f"当前用户明确引用 {previous.handle} 的回复。",
        "这是新的回合；旧记录仅是证据，不改变当前权限，不得重复已提交的发送或写入。",
    ]
    if include_digest:
        digest = app.turn_journal.render_turn(
            inbound.scope, previous.turn_ordinal, max_chars=app.settings.turn_expand_max_chars,
        )
        if digest:
            parts.append(digest)
    parts.append(f"[ambient delta]\n此前回合之后有 {count} 条其他消息。")
    parts.extend(f"- msg#{message.canonical_message_id} {message.sender_display}: {message.rendered_text[:300]}" for message in activity)
    return "\n".join(parts)
