"""Compose the shared orchestrator for a trusted, admitted official inbound.

Admission/owner/binding and durable inbox dispatch belong to the adapter. This
composition never infers permission from a history alias or a platform role.
"""
from __future__ import annotations

from ..conversation_keys import conversation_key
from ..handler_constants import TURN_PROMPT_VERSION
from .chat_orchestrator import ChatOrchestrator, ChatPorts
from .platform_chat import PlatformChatService
from .platform_turn_ports import current_turn_context, reply_target_turn


def build_platform_chat_orchestrator(app, *, service: PlatformChatService | None = None) -> ChatOrchestrator:
    service = service or PlatformChatService(app)

    def group_default(inbound):
        if inbound.endpoint.kind != "group":
            return None
        scope = inbound.endpoint.scope
        return (app.model_preferences.get_scope_default(scope)
                or app.settings.model_profile_for_scope(scope))

    def record(inbound, scope):
        return app.message_ledger.record_message(
            scope, native_message_id=inbound.native_message_id,
            sender_native_user_id=inbound.actor.native_user_id,
            sender_display=inbound.sender_display, body=inbound.body,
            occurred_at=inbound.occurred_at,
            reply_to_native_message_id=inbound.reply_to_native_message_id,
            actor=inbound.actor, source_endpoint=inbound.endpoint,
        ).canonical_message_id

    async def feedback(task_id):
        return app.running_tasks.drain_feedback(task_id)

    return ChatOrchestrator(
        ports=ChatPorts(
            conversation_id=conversation_key,
            endpoint_enabled=lambda endpoint: service.scope_enabled(endpoint.scope),
            group_default_profile=group_default,
            reply_target_turn=lambda inbound: reply_target_turn(app, inbound),
            record_trigger=record,
            current_turn_context=lambda inbound, turn_id: current_turn_context(app, inbound, turn_id),
            drain_feedback=feedback, ask_agent=service.ask,
            is_silence_reply=lambda reply: str(reply).strip() == "[silence]", journal_reply_text=str,
        ),
        running_tasks=app.running_tasks, model_preferences=app.model_preferences,
        model_catalog=app.model_catalog, message_ledger=app.message_ledger,
        turn_journal=app.turn_journal, usage_store=getattr(app, "usage_store", None),
        logger=app.logger, prompt_version=TURN_PROMPT_VERSION,
        simple_chat_profile=app.settings.model_simple_chat_profile, db_call=app.db_call,
    )
