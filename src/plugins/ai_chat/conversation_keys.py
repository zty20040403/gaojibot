"""Canonical conversation/settings keys with explicit legacy OneBot decoding."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .conversation_scope import ConversationScope, _decode_key, _encode_key
from .platform_types import ActorKey, InboundMessage


SETTING_NAMES = frozenset({"default", "member-default", "enabled", "vision-auto-describe"})
_LEGACY_SETTING = re.compile(r"^group:(\d+):(default|member-default|enabled|vision-auto-describe)$")


@dataclass(frozen=True)
class TurnAddress:
    """A history lane plus a full actor; neither grants access to the other."""

    scope: ConversationScope
    actor: ActorKey

    @property
    def key(self) -> str:
        return _encode_key("turn", [self.scope.key, self.actor.key])

    @classmethod
    def parse(cls, key: str) -> TurnAddress:
        scope, actor = _decode_key(key, "turn", 2)
        return cls(ConversationScope.from_key(scope), ActorKey.parse(actor))


def conversation_key(inbound: InboundMessage) -> str:
    if inbound.endpoint.account.platform == "onebot-v11":
        # Preserve all existing model preferences and short-term history keys.
        if inbound.endpoint.kind == "group":
            return f"group:{inbound.endpoint.native_conversation_id}:user:{inbound.actor.native_user_id}"
        return f"private:{inbound.actor.native_user_id}"
    return TurnAddress(inbound.scope, inbound.actor).key


@dataclass(frozen=True)
class ScopeSettingAddress:
    scope: ConversationScope
    name: str

    def __post_init__(self) -> None:
        if self.scope.kind != "group" or self.name not in SETTING_NAMES:
            raise ValueError("invalid group setting address")

    @property
    def key(self) -> str:
        if self.scope.platform == "onebot-v11":
            # Account-less OneBot settings predate the structured platform keys.
            native_id = self.scope.native_conversation_id
            if not native_id.isdecimal() or int(native_id) <= 0:
                raise ValueError("invalid legacy OneBot group setting")
            return f"group:{int(native_id)}:{self.name}"
        return _encode_key("setting", [self.scope.key, self.name])

    @classmethod
    def parse(cls, key: str) -> ScopeSettingAddress:
        legacy = _LEGACY_SETTING.fullmatch(key)
        if legacy is not None:
            return cls(ConversationScope("onebot-v11", "group", legacy[1]), legacy[2])
        scope, name = _decode_key(key, "setting", 2)
        return cls(ConversationScope.from_key(scope), name)
