"""Storage preparation for trusted structured inbound turns; no SDK event needed."""
from __future__ import annotations

from ..conversation_keys import conversation_key
from ..platform_types import InboundMessage
from ..memory_scopes import memory_scopes_for_inbound
from .shared_agent_turn import SharedTurnPorts


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


def platform_turn_ports(app, inbound: InboundMessage, reference_resolver) -> SharedTurnPorts:
    # A migrated history lane may share group facts, but never personal memory.
    group_scope, user_scope = memory_scopes_for_inbound(inbound)

    def resolve_plan(text, turn_id):
        ledger = app.message_ledger
        if ledger is None or inbound.endpoint.kind != "group":
            return None
        current_id = ledger.canonical_id_for_native(
            inbound.scope, inbound.native_message_id, endpoint=inbound.endpoint,
        )
        if current_id is None:
            return None
        return reference_resolver.resolve(
            ledger, inbound.scope, current_message_id=current_id, current_text=text,
            current_native_user_id=inbound.actor.native_user_id, now=inbound.occurred_at,
            current_actor_key=inbound.actor.key,
        )

    def render_memory(text, policy):
        return app.long_term_memory.render_relevant(
            group_scope, user_scope, text,
            include_group=policy.include_group_memory,
            include_user=policy.include_user_memory,
            fallback_group=policy.fallback_group_memory,
            fallback_user=policy.fallback_user_memory,
            max_entries_per_scope=policy.memory_max_entries_per_scope,
            max_chars=policy.memory_max_chars,
        )

    def current_user():
        ledger = app.message_ledger
        if ledger is not None:
            lookup = getattr(ledger, "principal_label_for_actor", None)
            if lookup is not None:
                label = lookup(inbound.actor)
                if label:
                    return label
        # Display labels are untrusted context, never identity or authorization.
        return inbound.sender_display or "当前用户"

    def fallback_context(policy, excluded):
        ledger = app.message_ledger
        if ledger is None:
            return ""
        if inbound.endpoint.kind == "private":
            if inbound.reply_to_native_message_id is None:
                return ""
            target_id = ledger.canonical_id_for_native(
                inbound.scope, inbound.reply_to_native_message_id, endpoint=inbound.endpoint,
            )
            target = ledger.get_in_scope(inbound.scope, target_id) if target_id is not None else None
            if target is None or not target.prompt_text:
                return ""
            # Private turns do not enable group timeline recall. An explicit
            # reply still carries bounded, visible evidence from its real source.
            max_chars = min(max(int(policy.token_budget.focus) * 4, 512), 4000)
            text = target.prompt_text[:max_chars]
            return (
                "[quoted context - explicit reply target, highest priority]\n"
                f"[msg#{target.canonical_message_id} | {target.sender_display[:100]}] {text}"
            )
        if not policy.include_recent_group:
            return ""
        current_id = app.message_ledger.canonical_id_for_native(
            inbound.scope, inbound.native_message_id, endpoint=inbound.endpoint,
        )
        excluded = (*excluded, current_id) if current_id is not None else excluded
        return app.message_ledger.render_recent(
            inbound.scope, max_messages=policy.max_messages, max_chars=policy.max_chars,
            exclude_canonical_message_ids=excluded,
        )

    return SharedTurnPorts(
        conversation_id=conversation_key(inbound), resolve_plan=resolve_plan,
        memory_scopes=(group_scope, user_scope), render_memory=render_memory,
        current_user=current_user, fallback_context=fallback_context,
        refresh_turn_context=lambda turn_id: current_turn_context(
            app, inbound, turn_id, create_edge=False, include_digest=False,
        ),
    )
