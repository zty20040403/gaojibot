"""Trusted adapter contracts shared by the application and platform adapters.

These objects are not model-input schemas. Adapters establish account, endpoint,
actor and reply evidence; a canonical history route never grants authority or
changes the send target. No transport SDK is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol

from .conversation_scope import ConversationKind, ConversationScope, _decode_key, _encode_key
from .message_ir import MessageBody
from .message_lowering import OutboundCapabilities


Environment = Literal["production", "sandbox", "test"]
CapabilityState = Literal["unknown", "available", "denied", "temporarily_unavailable"]
SendStatus = Literal["committed", "pending", "ambiguous", "failed"]


def _text(value: str, name: str, *, empty: bool = False) -> None:
    # Preserve the exact opaque identifier: no trimming, number conversion or
    # Unicode normalization can safely establish platform identity equivalence.
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a nonempty string")


def _counter(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


@dataclass(frozen=True)
class AccountKey:
    platform: str
    app_id: str
    environment: Environment

    def __post_init__(self) -> None:
        _text(self.platform, "platform")
        _text(self.app_id, "app_id")
        if self.environment not in {"production", "sandbox", "test"}:
            raise ValueError("unsupported account environment")

    @property
    def key(self) -> str:
        return _encode_key("account", [self.platform, self.app_id, self.environment])

    @classmethod
    def parse(cls, key: str) -> AccountKey:
        return cls(*_decode_key(key, "account", 3))


@dataclass(frozen=True)
class EndpointKey:
    account: AccountKey
    kind: ConversationKind
    native_conversation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.account, AccountKey):
            raise ValueError("endpoint requires an AccountKey")
        if self.kind not in {"group", "private"}:
            raise ValueError("unsupported endpoint kind")
        _text(self.native_conversation_id, "native_conversation_id")

    @property
    def key(self) -> str:
        return _encode_key("endpoint", [
            self.account.key, self.kind, self.native_conversation_id,
        ])

    @classmethod
    def parse(cls, key: str) -> EndpointKey:
        account, kind, native_id = _decode_key(key, "endpoint", 3)
        return cls(AccountKey.parse(account), kind, native_id)

    @property
    def scope(self) -> ConversationScope:
        return ConversationScope(
            self.account.platform, self.kind, self.native_conversation_id,
            account=self.account,
        )


@dataclass(frozen=True)
class ActorKey:
    """A namespaced identity, not necessarily a human authorization subject.

    Official ``bot`` identities use the real bot ID supplied by the platform.
    Adapters must not substitute the requester or derive a bot ID from AppID.
    Bot identities are ledger authors only, never inbound/send request actors.
    """

    account: AccountKey
    identity_kind: str
    identity_namespace: str
    native_user_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.account, AccountKey):
            raise ValueError("actor requires an AccountKey")
        _text(self.identity_kind, "identity_kind")
        _text(self.identity_namespace, "identity_namespace", empty=True)
        _text(self.native_user_id, "native_user_id")
        if self.account.platform == "qq-official":
            if self.identity_kind not in {"group_member", "c2c_user", "bot"}:
                raise ValueError("unsupported official actor identity kind")
            if self.identity_kind == "group_member" and not self.identity_namespace.strip():
                raise ValueError("official group actors require a group namespace")
            if self.identity_kind == "c2c_user" and self.identity_namespace:
                raise ValueError("official C2C actors have no group namespace")
            if self.identity_kind == "bot" and self.identity_namespace:
                raise ValueError("official bot authors have no group namespace")
        elif self.account.platform == "onebot-v11":
            if self.identity_kind != "user" or self.identity_namespace:
                raise ValueError("OneBot actors require the global user namespace")

    @property
    def key(self) -> str:
        return _encode_key("actor", [
            self.account.key, self.identity_kind, self.identity_namespace, self.native_user_id,
        ])

    @classmethod
    def parse(cls, key: str) -> ActorKey:
        account, kind, namespace, native_id = _decode_key(key, "actor", 4)
        return cls(AccountKey.parse(account), kind, namespace, native_id)

    def matches_endpoint(self, endpoint: EndpointKey) -> bool:
        """Identity-domain consistency only; this does not prove membership or access."""
        if not isinstance(endpoint, EndpointKey) or self.account != endpoint.account:
            return False
        if self.account.platform == "qq-official":
            if self.identity_kind == "bot":
                return True
            if endpoint.kind == "group":
                return (
                    self.identity_kind == "group_member"
                    and self.identity_namespace == endpoint.native_conversation_id
                )
            return (
                self.identity_kind == "c2c_user"
                and not self.identity_namespace
                and self.native_user_id == endpoint.native_conversation_id
            )
        if self.account.platform == "onebot-v11":
            return endpoint.kind == "group" or self.native_user_id == endpoint.native_conversation_id
        # New adapters must explicitly establish one of these identity domains.
        if self.identity_kind == "group_member":
            return endpoint.kind == "group" and self.identity_namespace == endpoint.native_conversation_id
        return (
            self.identity_kind == "user" and not self.identity_namespace
            and (endpoint.kind == "group" or self.native_user_id == endpoint.native_conversation_id)
        )


@dataclass(frozen=True)
class ReplyCredential:
    endpoint: EndpointKey
    kind: Literal["msg_id", "event_id"]
    value: str = field(repr=False)
    issued_at: int | None
    received_at: int
    expires_at: int
    max_replies: int

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointKey):
            raise ValueError("reply credential requires an endpoint")
        if self.kind not in {"msg_id", "event_id"}:
            raise ValueError("unsupported reply credential kind")
        _text(self.value, "reply credential value")
        if self.issued_at is not None:
            _counter(self.issued_at, "issued_at")
        for name in ("received_at", "expires_at", "max_replies"):
            _counter(getattr(self, name), name)
        if self.max_replies == 0:
            raise ValueError("reply credential must permit at least one reply")

    def valid_at(self, now: int) -> bool:
        """Expiry only; the sender separately allocates the persisted reply quota.

        A replay may arrive after expiry, so construction never resets or extends
        an old window based on received_at.
        """
        return self.received_at <= now < self.expires_at


def _validate_target(
    endpoint: EndpointKey, actor: ActorKey, credential: ReplyCredential | None,
) -> None:
    # The actor on these requests is the human requesting work. A separately
    # established bot author may be used only when recording an outbound result.
    if isinstance(actor, ActorKey) and actor.account.platform == "qq-official" and actor.identity_kind == "bot":
        raise ValueError("bot authors cannot be inbound or send request actors")
    if not isinstance(actor, ActorKey) or not actor.matches_endpoint(endpoint):
        raise ValueError("actor identity domain does not match endpoint")
    if credential is not None and (
        not isinstance(credential, ReplyCredential) or credential.endpoint != endpoint
    ):
        raise ValueError("reply credential does not match endpoint")


@dataclass(frozen=True)
class InboundMessage:
    endpoint: EndpointKey
    actor: ActorKey
    event_id: str | None
    native_message_id: str
    sender_display: str
    platform_role: Literal["owner", "admin", "member", ""]
    body: MessageBody
    mentions_bot: bool
    reply_to_native_message_id: str | None
    occurred_at: int
    reply_credential: ReplyCredential | None
    # Only trusted adapters/routing storage may set these fields. They select
    # history, never the actor, actual endpoint, reply window or authorization.
    canonical_scope: ConversationScope | None = None
    route_epoch: int = 0

    def __post_init__(self) -> None:
        _validate_target(self.endpoint, self.actor, self.reply_credential)
        _text(self.native_message_id, "native_message_id")
        for name in ("event_id", "reply_to_native_message_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if self.platform_role not in {"owner", "admin", "member", ""}:
            raise ValueError("unsupported platform role")
        if not isinstance(self.body, MessageBody):
            raise ValueError("body must be a MessageBody")
        if type(self.mentions_bot) is not bool:
            raise ValueError("mentions_bot must be a bool")
        _counter(self.occurred_at, "occurred_at")
        _counter(self.route_epoch, "route_epoch")
        if self.canonical_scope is not None and not isinstance(self.canonical_scope, ConversationScope):
            raise ValueError("canonical_scope must be a ConversationScope")

    @property
    def scope(self) -> ConversationScope:
        return self.canonical_scope if self.canonical_scope is not None else self.endpoint.scope


@dataclass(frozen=True)
class PlatformFeatures:
    """Implementation support only; not platform or local authorization."""

    group_files: bool = False
    reactions: bool = False
    forward_messages: bool = False
    member_list: bool = False
    native_ocr: bool = False
    inline_voice_text: bool = False
    markdown: bool = False
    native_files: bool = False
    proactive_group: bool = False


@dataclass(frozen=True)
class CapabilityEvidence:
    endpoint: EndpointKey
    state: CapabilityState
    source: str
    observed_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointKey):
            raise ValueError("capability evidence requires an endpoint")
        if self.state not in {"unknown", "available", "denied", "temporarily_unavailable"}:
            raise ValueError("unsupported capability state")
        _text(self.source, "capability evidence source")
        _counter(self.observed_at, "observed_at")
        _counter(self.expires_at, "expires_at")

    def available_at(self, now: int) -> bool:
        return self.state == "available" and self.observed_at <= now < self.expires_at


@dataclass(frozen=True)
class EffectiveCapabilities:
    endpoint: EndpointKey
    revision: int
    grants: Mapping[str, CapabilityEvidence]

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointKey):
            raise ValueError("effective capabilities require an endpoint")
        _counter(self.revision, "revision")
        copied = dict(self.grants)
        for name, evidence in copied.items():
            _text(name, "capability name")
            if not isinstance(evidence, CapabilityEvidence) or evidence.endpoint != self.endpoint:
                raise ValueError("capability evidence does not match endpoint")
        object.__setattr__(self, "grants", MappingProxyType(copied))

    def allows(self, name: str, now: int) -> bool:
        """Platform evidence only; callers must also check local actor authorization."""
        evidence = self.grants.get(name)
        return evidence is not None and evidence.available_at(now)


@dataclass(frozen=True)
class SendRequest:
    """Trusted delivery intent; actor is the human requester, not the sent author.

    Recheck route, actor authorization and quota at send. Only an actual committed
    receipt may be recorded using a separately established platform bot identity.
    """

    endpoint: EndpointKey
    actor: ActorKey
    body: MessageBody
    delivery_id: str
    route_epoch: int
    reply_credential: ReplyCredential | None = None
    reply_to_native_message_id: str | None = None
    fragment_id: str = ""

    def __post_init__(self) -> None:
        _validate_target(self.endpoint, self.actor, self.reply_credential)
        if not isinstance(self.body, MessageBody):
            raise ValueError("body must be a MessageBody")
        _text(self.delivery_id, "delivery_id")
        _text(self.fragment_id, "fragment_id", empty=True)
        _counter(self.route_epoch, "route_epoch")
        if self.reply_to_native_message_id is not None:
            _text(self.reply_to_native_message_id, "reply_to_native_message_id")


@dataclass(frozen=True)
class SendOutcome:
    """The sender may report committed only from an actual platform receipt."""

    status: SendStatus
    native_message_id: str | None = None
    receipt: Mapping[str, Any] = field(default_factory=dict, repr=False)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"committed", "pending", "ambiguous", "failed"}:
            raise ValueError("unsupported send status")
        if self.native_message_id is not None:
            _text(self.native_message_id, "native_message_id")
        receipt = dict(self.receipt)
        if self.status == "committed" and self.native_message_id is None and not receipt:
            raise ValueError("committed send requires platform receipt evidence")
        object.__setattr__(self, "receipt", MappingProxyType(receipt))


class TurnPlatform(Protocol):
    account: AccountKey
    capabilities: OutboundCapabilities
    features: PlatformFeatures

    async def ready(self, endpoint: EndpointKey) -> str | None: ...

    async def effective_capabilities(self, endpoint: EndpointKey) -> EffectiveCapabilities: ...

    async def send(self, request: SendRequest) -> SendOutcome: ...

    async def recall(self, endpoint: EndpointKey, native_message_id: str) -> bool: ...
