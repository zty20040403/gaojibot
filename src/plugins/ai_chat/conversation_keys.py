"""Canonical conversation/settings keys with explicit legacy OneBot decoding."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .conversation_scope import ConversationScope, _decode_key, _encode_key
from .platform_types import ActorKey, InboundMessage


SETTING_NAMES = frozenset({"default", "member-default", "enabled", "vision-auto-describe"})
_LEGACY_GROUP = re.compile(r"^group:(\d+)(?::user:(\d+))?$")
_LEGACY_PRIVATE = re.compile(r"^private:(\d+)$")
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


def scope_from_context_key(key: str) -> ConversationScope | None:
    """Find an evidence scope, never a send endpoint or authorization identity.

    Legacy memory keys are decoded only in their known formats. Structured
    official keys always retain their account/environment and actor namespace.
    """
    try:
        if key.startswith("turn:v1:"):
            return TurnAddress.parse(key).scope
        if key.startswith("setting:v1:"):
            return ScopeSettingAddress.parse(key).scope
        if key.startswith("actor:v1:"):
            actor = ActorKey.parse(key)
            if actor.identity_kind == "group_member":
                return ConversationScope(
                    actor.account.platform, "group", actor.identity_namespace,
                    account=actor.account,
                )
            if actor.identity_kind == "c2c_user":
                return ConversationScope(
                    actor.account.platform, "private", actor.native_user_id,
                    account=actor.account,
                )
            return None
        legacy = _LEGACY_GROUP.fullmatch(key)
        if legacy is not None:
            return ConversationScope("onebot-v11", "group", legacy[1])
        legacy_private = _LEGACY_PRIVATE.fullmatch(key)
        if legacy_private is not None:
            return ConversationScope("onebot-v11", "private", legacy_private[1])
        if key.startswith("group:"):
            platform, native_id = key[len("group:"):].split(":", 1)
            return ConversationScope(platform, "group", native_id)
        return ConversationScope.from_key(key)
    except (ValueError, TypeError):
        return None
