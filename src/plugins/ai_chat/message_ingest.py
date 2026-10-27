"""Message Ingest responsibilities extracted from the plugin entrypoint."""

from __future__ import annotations

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
from .proactive_learning import adaptive_for_group, is_rejection


class MessageIngest(HandlerService):
    async def handle_reaction_notice(self, bot: Bot, event: NoticeEvent) -> None:
        try:
            await self.context.db_call(
                record_reaction_feedback,
                event.model_dump(),
                bot_id=int(bot.self_id),
                ledger=self.context.message_ledger,
                journal=self.context.turn_journal,
                group_enabled=self.services.group_enabled,
                positive_ids=self.context.settings.feedback_positive_emoji_ids,
                negative_ids=self.context.settings.feedback_negative_emoji_ids,
            )
        except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
            self.context.logger.warning(f"Reaction feedback was not stored: {type(exc).__name__}")

    async def handle_canonical_ingest(self, event: MessageEvent) -> None:
        await self.services.ingest_adapter.ingest(event)
        await self._observe_proactive_message(event)

    async def _learning_enabled(self, group_id: int) -> bool:
        return (
            self.context.proactive_learning is not None
            and adaptive_for_group(self.context.settings, group_id)
            and await self.context.db_call(self.services.group_enabled, group_id)
        )

    async def handle_proactive_notice(self, bot: Bot, event: NoticeEvent) -> None:
        payload = event.model_dump()
        try:
            if not await self._learning_enabled(int(payload.get("group_id", 0))):
                return
            await self.context.db_call(
                self.context.proactive_learning.observe_notice,
                payload,
                bot_id=str(bot.self_id),
                positive_ids=self.context.settings.feedback_positive_emoji_ids,
                negative_ids=self.context.settings.feedback_negative_emoji_ids,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.context.logger.warning(f"Proactive feedback was not stored: {type(exc).__name__}")

    async def _observe_proactive_message(self, event: MessageEvent) -> None:
        if not isinstance(event, GroupMessageEvent) or event.user_id == event.self_id:
            return
        reply_id = str(event.reply.message_id) if event.reply is not None else None
        if reply_id is None:
            reply_id = next(
                (str(segment.data["id"]) for segment in event.original_message
                 if segment.type == "reply" and segment.data.get("id") is not None),
                None,
            )
        addressed = any(
            segment.type == "at" and str(segment.data.get("qq")) == str(event.self_id)
            for segment in event.original_message
        )
        text = event.original_message.extract_plain_text().strip()
        if (reply_id is None and not addressed) or not text or text.startswith("/"):
            return
        rejected = is_rejection(text)
        try:
            if not await self._learning_enabled(event.group_id):
                return
            await self.context.db_call(
                self.context.proactive_learning.feedback,
                scope_from_event(event),
                str(event.self_id),
                native_message_id=reply_id,
                actor_id=str(event.user_id),
                kind="rejected" if rejected else "follow_up",
                value=-1 if rejected else 1,
                source_id=str(event.message_id),
                occurred_at=event.time,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self.context.logger.warning(f"Proactive follow-up was not stored: {type(exc).__name__}")

    async def handle_group_activity(self, event: MessageEvent) -> None:
        await self.services.ingest_adapter.observe_group_activity(event)

    async def handle_image_auto_description(self, event: MessageEvent) -> None:
        if self.context.vision_worker is None:
            return
        if event.user_id == event.self_id:
            return
        if isinstance(event, GroupMessageEvent) and not (
            await self.context.db_call(self.services.group_enabled, event.group_id)
        ):
            return
        if isinstance(event, GroupMessageEvent) and not (
            await self.context.db_call(
                self.services.auto_describe_enabled, event.group_id
            )
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
            await self.context.db_call(
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
        if not (
            await self.context.db_call(self.services.group_enabled, event.group_id)
        ):
            return

        (
            await self.context.db_call(
                learn_stickers_from_message, event.original_message
            )
        )

        if self.context.message_ledger is not None:
            return

        text = self.services.chat._render_message_text(event.original_message)
        if not text or text.lstrip().startswith("/"):
            return

        (
            await self.context.db_call(
                self.context.group_context.append,
                event.group_id,
                self.services.chat._sender_label(event),
                text,
                timestamp=event.time,
                message_id=event.message_id,
            )
        )
