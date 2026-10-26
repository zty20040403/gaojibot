"""Message Ingest responsibilities extracted from the plugin entrypoint."""

from __future__ import annotations

import asyncio
from src.bot_storage import (
    DatabaseError,
)
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageEvent,
    NoticeEvent,
    Bot,
)
from .onebot_codec import (
    scope_from_event,
)
from .stickers import (
    learn_stickers_from_message,
)
from .handler_services import HandlerService
from .reaction_feedback import record_reaction_feedback


class MessageIngest(HandlerService):
    async def handle_reaction_notice(self, bot: Bot, event: NoticeEvent) -> None:
        try:
            await asyncio.to_thread(record_reaction_feedback, event.model_dump(),
                bot_id=int(bot.self_id), ledger=self.context.message_ledger,
                journal=self.context.turn_journal, group_enabled=self.services.group_enabled,
                positive_ids=self.context.settings.feedback_positive_emoji_ids,
                negative_ids=self.context.settings.feedback_negative_emoji_ids)
        except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
            self.context.logger.warning(f"Reaction feedback was not stored: {type(exc).__name__}")

    async def handle_canonical_ingest(self, event: MessageEvent) -> None:
        await self.services.ingest_adapter.ingest(event)

    async def handle_group_activity(self, event: MessageEvent) -> None:
        await self.services.ingest_adapter.observe_group_activity(event)

    async def handle_image_auto_description(self, event: MessageEvent) -> None:
        if self.context.vision_worker is None:
            return
        if event.user_id == event.self_id:
            return
        if isinstance(event, GroupMessageEvent) and not self.services.group_enabled(event.group_id):
            return
        if isinstance(event, GroupMessageEvent) and not self.services.auto_describe_enabled(
            event.group_id
        ):
            return
        if not isinstance(event, GroupMessageEvent) and not self.context.settings.vision_auto_describe:
            return
        if event.original_message.extract_plain_text().strip():
            return
        source_items = self.services.chat._indexed_image_sources(
            event.original_message,
            ordinary_only=True,
        )
        if not source_items:
            return
        segment_index, source_url = source_items[0]
        try:
            await asyncio.to_thread(
                self.context.vision_worker.submit,
                scope_key=scope_from_event(event).key,
                native_message_id=event.message_id,
                segment_index=segment_index,
                requester_native_user_id=event.user_id,
                source_url=source_url,
                mode="summary",
                delivery_target=scope_from_event(event),
                reply_to_native_message_id=event.message_id,
            )
        except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
            self.context.logger.warning(f"Automatic image introduction failed: {exc}")
            return

    async def handle_group_context_recorder(self, event: MessageEvent) -> None:
        if not isinstance(event, GroupMessageEvent):
            return
        if event.user_id == event.self_id:
            return
        if not self.services.group_enabled(event.group_id):
            return

        learn_stickers_from_message(event.original_message)

        if self.context.message_ledger is not None:
            return

        text = self.services.chat._render_message_text(event.original_message)
        if not text or text.lstrip().startswith("/"):
            return

        self.context.group_context.append(
            event.group_id,
            self.services.chat._sender_label(event),
            text,
            timestamp=event.time,
            message_id=event.message_id,
        )
