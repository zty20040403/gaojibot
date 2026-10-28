"""Structural verification of bot identity observations from trusted transports.

INTERNAL ADAPTER API: this module does not authenticate anything. Only the
runtime that authenticated the exact account/environment may construct a
TrustedTransportObservation. Never deserialize it from chat, tools, or model
output. Its boolean records a caller-established fact; it is not authorization.
No payload, token, human openid, AppID derivation, or fake READY proves identity.
Test observations may establish identity only for their separate test account.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal
import unicodedata

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey

OFFICIAL_SDK_COMMIT = "e25f3e84bad7217357d8200a9d12939b58285b84"
MAX_OBSERVATION_TIME = 2**63 - 1
_SOURCE_PATHS = {
    "ready": "botpy/types/gateway.py",
    "self_api": "botpy/api.py",
}


@dataclass(frozen=True)
class TrustedTransportObservation:
    """Caller-established provenance; NOT a user-supplied authentication proof.

    authenticated_account includes environment. For self_api, successful must
    mean M0 validated HTTP AND body success from GET /users/@me. For READY the
    runtime must have accepted this frame on the authenticated gateway session.
    """

    authenticated_account: AccountKey = field(repr=False)
    authenticated: bool
    observed_at: int
    successful: bool = False


@dataclass(frozen=True)
class IdentityEvidence:
    source: Literal["ready", "self_api"]
    source_commit: str
    source_url: str
    first_observed_at: int
    last_observed_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or self.source not in _SOURCE_PATHS:
            raise ValueError("unsupported identity evidence source")
        expected_url = f"https://github.com/tencent-connect/botpy/blob/{OFFICIAL_SDK_COMMIT}/{_SOURCE_PATHS[self.source]}"
        if self.source_commit != OFFICIAL_SDK_COMMIT or self.source_url != expected_url:
            raise ValueError("unsupported identity evidence contract")
        if not all(type(value) is int and 0 <= value <= MAX_OBSERVATION_TIME for value in (self.first_observed_at, self.last_observed_at)):
            raise ValueError("invalid evidence observation time")
        if self.first_observed_at > self.last_observed_at:
            raise ValueError("invalid evidence observation range")


@dataclass(frozen=True)
class VerifiedBotIdentity:
    native_id: str = field(repr=False)
    evidence: tuple[IdentityEvidence, ...]

    def __post_init__(self) -> None:
        if not _valid_id(self.native_id):
            raise ValueError("invalid bot identity native ID")
        if not isinstance(self.evidence, tuple) or not 1 <= len(self.evidence) <= 2:
            raise ValueError("verified identity requires bounded evidence")
        if not all(isinstance(item, IdentityEvidence) for item in self.evidence):
            raise ValueError("invalid identity evidence")
        for item in self.evidence:
            item.__post_init__()
        if len({item.source for item in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate identity evidence source")


@dataclass(frozen=True)
class IdentityState:
    account: AccountKey = field(repr=False)
    status: Literal["unknown", "verified", "mismatch"] = "unknown"
    identity: VerifiedBotIdentity | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.account, AccountKey) or self.account.platform != "qq-official":
            raise ValueError("official account required")
        if self.status not in {"unknown", "verified", "mismatch"}:
            raise ValueError("invalid identity state")
        if (self.status == "unknown") != (self.identity is None):
            raise ValueError("inconsistent identity state")
        if self.identity is not None:
            if not isinstance(self.identity, VerifiedBotIdentity):
                raise ValueError("invalid restored identity")
            self.identity.__post_init__()

    @property
    def bot_actor(self) -> ActorKey | None:
        # Conflict retains old evidence for review, never for active sending.
        if self.status != "verified" or self.identity is None:
            return None
        return ActorKey(self.account, "bot", "", self.identity.native_id)


@dataclass(frozen=True)
class ObservationResult:
    state: IdentityState
    accepted: bool
    reason: str
    conflicting_evidence: IdentityEvidence | None = None


def _valid_id(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        return False
    return len(value.encode("utf-8")) <= 256


def _observe(
    state: IdentityState, context: TrustedTransportObservation,
    user: object, source: Literal["ready", "self_api"],
) -> ObservationResult:
    def rejected(reason: str) -> ObservationResult:
        return ObservationResult(state, False, reason)

    if not isinstance(context, TrustedTransportObservation) or context.authenticated is not True:
        return rejected("unauthenticated_transport")
    if context.authenticated_account != state.account:
        return rejected("account_environment_mismatch")
    if type(context.observed_at) is not int or not 0 <= context.observed_at <= MAX_OBSERVATION_TIME:
        return rejected("invalid_observation_time")
    if source == "self_api" and context.successful is not True:
        return rejected("self_api_not_successful")
    if not isinstance(user, Mapping) or not _valid_id(user.get("id")):
        return rejected("invalid_bot_identity")
    # The authenticated GET /users/@me contract identifies the current robot;
    # its official example omits bot. This exception applies only after the
    # exact account and successful self-endpoint provenance checks above.
    # https://github.com/tencent-connect/bot-docs/blob/645787a45937e5d9c4f0f61afefdffde0f38696e/docs/develop/api-v2/openapi/user/me.md
    # READY and any explicitly supplied bot field still require strict True.
    if user.get("bot") is not True and (source != "self_api" or "bot" in user):
        return rejected("invalid_bot_identity")
    # No trimming, numeric coercion, Unicode normalization, or fallback fields.
    native_id = user["id"]
    evidence = IdentityEvidence(
        source, OFFICIAL_SDK_COMMIT,
        f"https://github.com/tencent-connect/botpy/blob/{OFFICIAL_SDK_COMMIT}/{_SOURCE_PATHS[source]}",
        context.observed_at, context.observed_at,
    )
    if state.status == "mismatch":
        return rejected("identity_conflict_requires_review")
    if state.identity is not None and state.identity.native_id != native_id:
        return ObservationResult(replace(state, status="mismatch"), False, "identity_conflict", evidence)
    if state.identity is None:
        identity = VerifiedBotIdentity(native_id, (evidence,))
    else:
        observations = {item.source: item for item in state.identity.evidence}
        old = observations.get(source)
        if old is not None:
            evidence = replace(
                old,
                first_observed_at=min(old.first_observed_at, context.observed_at),
                last_observed_at=max(old.last_observed_at, context.observed_at),
            )
        observations[source] = evidence
        identity = VerifiedBotIdentity(native_id, tuple(observations[key] for key in sorted(observations)))
    return ObservationResult(IdentityState(state.account, "verified", identity), True, "verified")


def observe_ready(
    state: IdentityState, context: TrustedTransportObservation, frame: object,
) -> ObservationResult:
    """Validate an already-authenticated READY; synthetic production input is forbidden."""
    if not isinstance(frame, Mapping) or type(frame.get("op")) is not int or frame.get("op") != 0 or frame.get("t") != "READY":
        return ObservationResult(state, False, "not_ready_frame")
    data = frame.get("d")
    if not isinstance(data, Mapping) or not _valid_id(data.get("session_id")):
        return ObservationResult(state, False, "invalid_ready_session")
    return _observe(state, context, data.get("user"), "ready")


def observe_self_api(
    state: IdentityState, context: TrustedTransportObservation, payload: object,
) -> ObservationResult:
    """Validate the success body of the account's GET /users/@me; never perform I/O."""
    # Defense in depth: a purported success with error fields isn't evidence.
    if isinstance(payload, Mapping) and any(
        name in payload and (type(payload[name]) is not int or payload[name] != 0)
        for name in ("code", "err_code")
    ):
        return ObservationResult(state, False, "self_api_error_payload")
    return _observe(state, context, payload, "self_api")
