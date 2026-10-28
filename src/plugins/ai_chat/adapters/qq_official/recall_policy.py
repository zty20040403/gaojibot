"""Fail-closed eligibility for explicit recall; never send-failure compensation.

Internal adapter port: messages come from the exact-endpoint ledger, identity
from identity_store, and limits/capabilities from verified platform evidence.
No caller may populate these from chat/model arguments. Eligibility is not an
execution permit: the eventual M7 transport must recheck current lease, route,
authorization and this deadline immediately before one audited DELETE.

There is deliberately no guessed two-minute default or live DELETE wiring.
Self recall and member moderation require separate evidence and local grants.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.plugins.ai_chat.ledger import CanonicalMessage
from src.plugins.ai_chat.platform_types import ActorKey, EffectiveCapabilities, EndpointKey


@dataclass(frozen=True)
class RecallLimits:
    endpoint: EndpointKey = field(repr=False)
    operation: str
    max_age_seconds: int
    observed_at: int
    expires_at: int
    source: str

    def __post_init__(self):
        if (not isinstance(self.endpoint, EndpointKey)
                or self.operation not in {"recall_own", "recall_member"}
                or type(self.max_age_seconds) is not int or not 1 <= self.max_age_seconds <= 86400
                or type(self.observed_at) is not int or type(self.expires_at) is not int
                or not 0 <= self.observed_at < self.expires_at
                or not isinstance(self.source, str) or not self.source.strip()):
            raise ValueError("invalid_recall_limits")


@dataclass(frozen=True)
class RecallEligibility:
    allowed: bool
    reason: str
    deadline: int | None = None


@dataclass(frozen=True)
class RecallMessageTime:
    """Trusted original time; receipt observation is never a send timestamp.

    For our own messages, a recorded dispatch start is a conservative lower
    bound. For inbound messages only the authenticated original platform
    timestamp is eligible, never the adapter's missing/future-time fallback.
    """
    endpoint: EndpointKey = field(repr=False)
    native_message_id: str = field(repr=False)
    occurred_at: int
    source: str


def recall_eligibility(*, endpoint, requester, message, operation, capabilities,
                       capability_revision, verified_bot=None, limits=None,
                       message_time=None, authorize=None, now, safety_margin=5):
    """Read-only policy over current trusted evidence; unknown denies by default.

    ``authorize`` checks the human actor's exact operation and target message.
    A local owner grant alone cannot establish platform moderation capability.
    A canonical history scope never substitutes for the actual source endpoint.
    """
    if (not isinstance(endpoint, EndpointKey) or endpoint.account.platform != "qq-official"
            or not isinstance(requester, ActorKey) or requester.identity_kind == "bot"
            or not requester.matches_endpoint(endpoint)
            or operation not in {"recall_own", "recall_member"}
            or type(now) is not int or now < 0
            or type(safety_margin) is not int or not 1 <= safety_margin <= 30):
        return RecallEligibility(False, "invalid_recall_context")
    if (not isinstance(message, CanonicalMessage) or not message.native_message_id
            or message.source_endpoint_key != endpoint.key
            or type(message.occurred_at) is not int or not 0 <= message.occurred_at <= now):
        return RecallEligibility(False, "message_provenance_unavailable")
    try:
        author = ActorKey.parse(message.sender_actor_key)
    except (TypeError, ValueError):
        return RecallEligibility(False, "message_author_unavailable")
    if author.account != endpoint.account or message.sender_native_user_id != author.native_user_id:
        return RecallEligibility(False, "message_author_conflict")
    if operation == "recall_own":
        if (not isinstance(verified_bot, ActorKey) or verified_bot.account != endpoint.account
                or verified_bot.identity_kind != "bot" or verified_bot.identity_namespace
                or author != verified_bot or message.direction != "outbound"):
            return RecallEligibility(False, "not_verified_own_message")
    elif (endpoint.kind != "group" or author.identity_kind != "group_member"
          or not author.matches_endpoint(endpoint) or message.direction != "inbound"):
        return RecallEligibility(False, "not_group_member_message")
    if (not isinstance(message_time, RecallMessageTime) or message_time.endpoint != endpoint
            or message_time.native_message_id != message.native_message_id
            or type(message_time.occurred_at) is not int
            or not 0 <= message_time.occurred_at <= min(now, message.occurred_at)
            or message_time.source not in {"platform", "dispatch_started"}
            or (operation == "recall_member" and message_time.source != "platform")):
        return RecallEligibility(False, "original_message_time_unverified")
    if (not isinstance(capabilities, EffectiveCapabilities) or capabilities.endpoint != endpoint
            or type(capability_revision) is not int or capabilities.revision != capability_revision
            or not capabilities.allows(operation, now)):
        return RecallEligibility(False, "recall_capability_unverified")
    if (not isinstance(limits, RecallLimits) or limits.endpoint != endpoint
            or limits.operation != operation or not limits.observed_at <= now < limits.expires_at):
        return RecallEligibility(False, "recall_limits_unverified")
    deadline = min(message_time.occurred_at + limits.max_age_seconds - safety_margin,
                   limits.expires_at, capabilities.grants[operation].expires_at)
    if now >= deadline:
        return RecallEligibility(False, "recall_deadline_expired")
    if not callable(authorize) or authorize(requester, endpoint, operation, message) is not True:
        return RecallEligibility(False, "recall_not_authorized")
    return RecallEligibility(True, "eligible_for_explicit_recall", deadline)
