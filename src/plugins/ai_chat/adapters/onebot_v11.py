from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol
from weakref import WeakValueDictionary

from src.bot_storage import DatabaseCalls
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)

from ..bridges import BridgeEvent
from ..ocr import image_sources
from ..onebot_codec import (
    decode_onebot_message,
    record_onebot_event,
    scope_from_event,
)
from ..voice import contains_voice
from ..video import contains_video


class AdapterLogger(Protocol):
    def warning(self, message: object, *args: object, **kwargs: object) -> object: ...


class OneBotIngestAdapter:
    """Translate OneBot events into canonical storage and application caches."""

    def __init__(
        self,
        *,
        group_enabled: Callable[[int], bool],
        canonical_scope: Callable[[MessageEvent], Any],
        image_cache_key: Callable[[MessageEvent], str],
        voice_cache_key: Callable[[MessageEvent], str],
        video_cache_key: Callable[[MessageEvent], str] | None = None,
        ocr_max_images: int,
        logger: AdapterLogger,
        message_ledger: Any = None,
        delivery_store: Any = None,
        bridge_router: Any = None,
        mirror_state: Any = None,
        bridge_manager: Any = None,
        media_library: Any = None,
        source_store: Any = None,
        user_profiles: Any = None,
        recent_images: Any = None,
        recent_voices: Any = None,
        recent_videos: Any = None,
        db_call: DatabaseCalls | None = None,
    ) -> None:
        self.group_enabled = group_enabled
        self.canonical_scope = canonical_scope
        self.image_cache_key = image_cache_key
        self.voice_cache_key = voice_cache_key
        self.video_cache_key = video_cache_key or voice_cache_key
        self.ocr_max_images = max(int(ocr_max_images), 1)
        self.logger = logger
        self.message_ledger = message_ledger
        self.delivery_store = delivery_store
        self.bridge_router = bridge_router
        self.mirror_state = mirror_state
        self.bridge_manager = bridge_manager
        self.media_library = media_library
        self.source_store = source_store
        self.user_profiles = user_profiles
        self.recent_images = recent_images
        self.recent_voices = recent_voices
        self.recent_videos = recent_videos
        self.db_call = db_call if db_call is not None else DatabaseCalls()
        self._ingest_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )

    async def ingest(self, event: MessageEvent) -> None:
        scope_key = scope_from_event(event).key
        lock = self._ingest_locks.setdefault(scope_key, asyncio.Lock())
        # Preserve arrival order within a conversation without blocking other groups.
        async with lock:
            await self.db_call(self._ingest, event)

    def _ingest(self, event: MessageEvent) -> None:
        if (
            isinstance(event, GroupMessageEvent)
            and not self.group_enabled(event.group_id)
        ):
            return
        physical_scope = scope_from_event(event)
        decoded = decode_onebot_message(event.original_message)
        reconciled_delivery = None
        if self.delivery_store is not None and event.user_id == event.self_id:
            try:
                reconciled_delivery = self.delivery_store.reconcile_echo(
                    physical_scope,
                    decoded.body,
                    native_message_id=event.message_id,
                    reply_to_native_message_id=decoded.reply_to_native_message_id,
                    observed_at=int(event.time),
                )
                if reconciled_delivery is not None and self.mirror_state is not None:
                    self.mirror_state.confirm_delivery(
                        reconciled_delivery.delivery_id,
                        str(event.message_id),
                        confirmed_at=int(event.time),
                    )
            except Exception as exc:
                self.logger.warning(f"Outbound echo reconciliation failed: {exc}")
        if self.message_ledger is None:
            return
        try:
            if (
                self.bridge_manager is not None
                and self.bridge_router is not None
                and self.bridge_router.bundle_for(physical_scope) is not None
                and event.user_id != event.self_id
            ):
                sender = getattr(event, "sender", None)
                sender_name = str(
                    getattr(sender, "card", "")
                    or getattr(sender, "nickname", "")
                    or "群成员"
                )
                plain = event.original_message.extract_plain_text().strip()
                self.bridge_manager.ingest(
                    BridgeEvent(
                        scope=physical_scope,
                        native_event_id=str(event.message_id),
                        sender_native_user_id=str(event.user_id),
                        sender_display=sender_name,
                        body=decoded.body,
                        occurred_at=int(event.time),
                        reply_to_native_message_id=(
                            decoded.reply_to_native_message_id
                        ),
                        message_kind=("command" if plain.startswith("/") else "chat"),
                        raw_event={"source": "onebot-event"},
                    )
                )
                return
            is_mirror_echo = bool(
                reconciled_delivery is not None
                and self.mirror_state is not None
                and self.mirror_state.is_mirror_delivery(
                    reconciled_delivery.delivery_id
                )
            )
            if is_mirror_echo:
                return
            stored = record_onebot_event(
                self.message_ledger,
                event,
                scope=self.canonical_scope(event),
            )
            if self.media_library is not None and event.user_id != event.self_id:
                self.media_library.ingest_message(
                    physical_scope,
                    native_message_id=event.message_id,
                    sender_native_user_id=event.user_id,
                    segments=[
                        {"type": segment.type, "data": dict(segment.data)}
                        for segment in event.original_message
                    ],
                    canonical_message_id=stored.canonical_message_id,
                    occurred_at=int(event.time),
                )
            if self.source_store is not None and event.user_id != event.self_id:
                self.source_store.ingest_message(
                    physical_scope,
                    body=decoded.body,
                    native_message_id=event.message_id,
                    sender_native_user_id=event.user_id,
                    canonical_message_id=stored.canonical_message_id,
                    occurred_at=int(event.time),
                )
            if event.user_id == event.self_id and self.bridge_manager is not None:
                self.bridge_manager.mirror_local_outgoing(
                    source_scope=physical_scope,
                    source_native_event_id=str(event.message_id),
                    canonical_message_id=stored.canonical_message_id,
                    body=decoded.body,
                    occurred_at=int(event.time),
                    reply_to_native_message_id=decoded.reply_to_native_message_id,
                )
        except Exception as exc:
            self.logger.warning(f"Canonical message ingest failed: {exc}")

    async def observe_group_activity(self, event: MessageEvent) -> None:
        if event.user_id == event.self_id:
            return
        if isinstance(event, GroupMessageEvent):
            if not await self.db_call(self.group_enabled, event.group_id):
                return
            if self.user_profiles is not None:
                await self.db_call(
                    self.user_profiles.observe,
                    event.group_id,
                    event.user_id,
                    nickname=event.sender.nickname or "",
                    card=event.sender.card or "",
                )
        elif not isinstance(event, PrivateMessageEvent):
            return

        # Recent media belongs to the physical conversation and sender. Private
        # messages need the same short-lived cache as groups so a phone user can
        # send an image first and ask about it in the next message.
        sources = image_sources(
            event.original_message,
            max_images=self.ocr_max_images,
        )
        if sources and self.recent_images is not None:
            self.recent_images.record(self.image_cache_key(event), sources)
        if contains_voice(event.original_message) and self.recent_voices is not None:
            self.recent_voices.record(
                self.voice_cache_key(event),
                event.message_id,
            )
        if contains_video(event.original_message) and self.recent_videos is not None:
            self.recent_videos.record(
                self.video_cache_key(event),
                event.message_id,
            )
