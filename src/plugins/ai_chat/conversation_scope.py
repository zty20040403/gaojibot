from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .platform_types import AccountKey


ConversationKind = Literal["group", "private"]


def _encode_key(kind: str, values: list[str]) -> str:
    return f"{kind}:v1:" + json.dumps(values, ensure_ascii=True, separators=(",", ":"))


def _decode_key(value: str, kind: str, size: int) -> list[str]:
    prefix = f"{kind}:v1:"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError(f"invalid {kind} key")
    try:
        parts = json.loads(value[len(prefix):])
    except (ValueError, RecursionError):
        raise ValueError(f"invalid {kind} key") from None
    if (
        not isinstance(parts, list)
        or len(parts) != size
        or any(not isinstance(part, str) for part in parts)
        or _encode_key(kind, parts) != value
    ):
        raise ValueError(f"invalid canonical {kind} key")
    return parts


@dataclass(frozen=True)
class ConversationScope:
    """History address, never an authorization identity.

    Account-less legacy keys retain their exact representation. New official
    scopes require an account. Actor/bot metadata does not alter the history key
    and must not be used to construct or merge an ActorKey.
    """

    platform: str
    kind: ConversationKind
    native_conversation_id: str
    actor_native_user_id: str = ""
    bot_native_user_id: str = ""
    account: AccountKey | None = None

    def __post_init__(self) -> None:
        if not self.platform.strip():
            raise ValueError("platform must not be empty")
        if self.kind not in {"group", "private"}:
            raise ValueError("unsupported conversation kind")
        if not self.native_conversation_id.strip():
            raise ValueError("native_conversation_id must not be empty")
        if self.account is not None:
            from .platform_types import AccountKey

            if not isinstance(self.account, AccountKey):
                raise ValueError("account must be an AccountKey")
            if self.account.platform != self.platform:
                raise ValueError("scope and account platforms must match")
        elif self.platform == "qq-official":
            raise ValueError("official scopes require an account")

    @property
    def key(self) -> str:
        if self.account is not None:
            return _encode_key("scope", [
                self.account.key, self.kind, self.native_conversation_id,
            ])
        return (
            f"{self.platform}:{self.kind}:"
            f"{self.native_conversation_id}"
        )

    @classmethod
    def from_key(cls, key: str) -> "ConversationScope":
        """Decode a history address without inventing an actor or bot identity."""
        if not isinstance(key, str):
            raise ValueError("invalid scope key")
        if key.startswith("scope:v1:"):
            from .platform_types import AccountKey

            account_key, kind, native_id = _decode_key(key, "scope", 3)
            account = AccountKey.parse(account_key)
            return cls(account.platform, kind, native_id, account=account)
        parts = key.split(":", 2)
        if len(parts) != 3:
            raise ValueError("invalid legacy scope key")
        return cls(*parts)

    @classmethod
    def parse(cls, key: str) -> "ConversationScope":
        return cls.from_key(key)

    def with_actor(self, native_user_id: str | int) -> "ConversationScope":
        return replace(self, actor_native_user_id=str(native_user_id))
