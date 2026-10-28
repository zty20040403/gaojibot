"""Native reply restoration for the explicitly selected OneBot execution path."""

from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment


class OneBotReplyCheckpointCodec:
    platform = "onebot-v11"

    def encode(self, segment: object) -> dict[str, Any]:
        if not isinstance(segment, MessageSegment):
            raise TypeError("OneBot reply checkpoints require MessageSegment")
        return {"type": segment.type, "data": segment.data}

    def decode(self, saved: dict[str, Any]) -> MessageSegment:
        return MessageSegment(**saved)
