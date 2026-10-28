"""Compatibility import path for the shared, SDK-free platform contracts."""

from ..platform_types import (
    AccountKey,
    ActorKey,
    CapabilityEvidence,
    CapabilityState,
    EffectiveCapabilities,
    EndpointKey,
    Environment,
    InboundMessage,
    PlatformFeatures,
    ReplyCredential,
    SendOutcome,
    SendRequest,
    SendStatus,
    TurnPlatform,
)

__all__ = [
    "AccountKey", "ActorKey", "CapabilityEvidence", "CapabilityState",
    "EffectiveCapabilities", "EndpointKey", "Environment", "InboundMessage",
    "PlatformFeatures", "ReplyCredential", "SendOutcome", "SendRequest",
    "SendStatus", "TurnPlatform",
]
