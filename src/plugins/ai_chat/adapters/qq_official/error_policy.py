"""Pure M3 decisions over M0 evidence; no retry, fallback, networking, or mutation.

Only call this at the failed operation boundary: `request` means the message
POST, not a gateway/self-info GET. `token` and `upload` are prerequisite stages.
Their failure never proves a message was sent, but the upload itself may remain
ambiguous. Any returned retry action still requires persisted budgets, epoch,
authorization, deadline/window, unchanged payload, and sequence checks.
Unknown codes and category labels do not create acceptance evidence.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from typing import Literal
import unicodedata

from src.qq_official.errors import ERROR_CONTRACT, QQAPIError, classify_error

MessageAcceptance = Literal["not_sent", "rejected", "unknown", "accepted"]
M0_SOURCE = "src/qq_official/errors.py@7ea5ac09ec5a8102c0ff899b92e13700ca5133e6"


@dataclass(frozen=True)
class ErrorDecision:
    action: str
    message_acceptance: MessageAcceptance
    operation_acceptance: bool | None
    reason: str
    status: int | None
    code: int | None
    stage: str
    request_id: str | None = field(repr=False)
    delay_seconds: float | None = None
    effects: tuple[str, ...] = ()
    contract: str = ERROR_CONTRACT
    source: str = M0_SOURCE

    @property
    def automatic_format_fallback(self) -> bool:
        return False


def _counter(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("retry counters must be nonnegative integers")


def _delay(error: QQAPIError, retry_count: int) -> float:
    value = error.retry_after
    if type(value) in {int, float} and 0 <= value <= 86400 and math.isfinite(value):
        return float(value)
    return float(min(60, 2 ** min(retry_count, 6)))


def decide_send_error(
    error: QQAPIError, *, auth_retries_used: int = 0,
    retry_count: int = 0, max_transient_retries: int = 3,
) -> ErrorDecision:
    """Return a bounded scheduling decision, never perform the action.

    Counters belong to one durable logical operation and survive restarts.
    Refresh uses TokenManager single-flight; it must not unconditionally
    invalidate a newer token (M0 already invalidates only the rejected token).
    """
    if not isinstance(error, QQAPIError):
        raise TypeError("M0 QQAPIError required")
    for value in (auth_retries_used, retry_count, max_transient_retries):
        _counter(value)
    stage = error.stage
    prerequisite = stage in {"token", "upload"}
    rejected = error.accepted is False
    acceptance: MessageAcceptance = "not_sent" if prerequisite else ("rejected" if rejected else "unknown")

    def decide(action: str, reason: str, *, delay=None, effects=(), message_acceptance=None) -> ErrorDecision:
        return ErrorDecision(
            action, message_acceptance or acceptance,
            False if rejected else None, reason,
            error.status, error.code, stage, error.request_id, delay, effects,
        )

    if stage not in {"token", "upload", "request", "send"}:
        return decide("quarantine_ambiguous", "unsupported_stage", message_acceptance="unknown")
    # Verify the M0 numeric/status contract again. A fabricated category string
    # or malformed numeric field must not unlock window/auth/capability effects.
    valid_numbers = (
        (error.status is None or (type(error.status) is int and 100 <= error.status <= 599))
        and (error.code is None or type(error.code) is int)
    )
    category = "unknown"
    if valid_numbers:
        category = classify_error(error.status or 0, error.code, stage=stage).category
    if category != error.category:
        category = "unknown"
    connection_failed = (
        error.category == "connection_failed" and rejected
        and error.status is None and error.code is None
    )

    if stage == "token":
        # Internal prepared-token validation runs synchronously before any
        # message HTTP write. This local proof has no provider status/code;
        # expiry/rotation must re-enter preflight + rate admission on a new
        # attempt, without consuming the remote-auth retry budget or pausing it.
        if (error.category == "prepared_token_unavailable" and rejected
                and error.status is None and error.code is None):
            if retry_count >= max_transient_retries:
                return decide("pause_retry_budget", "prepared_auth_retry_budget_exhausted")
            return decide("backoff_unsent", "prepared_auth_stale", delay=1)
        if rejected and category == "authentication":
            return decide("pause_auth", "token_authentication_failed", effects=("diagnose_account_auth",))
        if rejected and (connection_failed or category == "rate_limited"):
            if retry_count >= max_transient_retries:
                return decide("pause_retry_budget", "token_retry_budget_exhausted")
            return decide("wait_token", "token_preflight_failed", delay=_delay(error, retry_count))
        return decide("pause_token_preflight", "token_outcome_requires_review")

    # Even a documented numeric reason cannot create False acceptance when
    # the transport retained None. No content/format/channel bypass is allowed.
    if not rejected:
        if stage == "upload":
            action = "review_upload_contract" if category == "upload_contract_error" else "review_upload_ambiguous"
            return decide(action, "upload_outcome_unresolved", effects=("no_message_attempt",))
        return decide("quarantine_ambiguous", "message_outcome_unresolved", effects=("no_automatic_resend",))
    if category == "content_rejected":
        return decide("stop_content", "content_rejected", effects=("no_format_account_or_channel_retry",))
    if connection_failed:
        if retry_count >= max_transient_retries:
            return decide("pause_retry_budget", "connection_retry_budget_exhausted", message_acceptance="not_sent")
        return decide("backoff_unsent", "request_not_dispatched", delay=_delay(error, retry_count), message_acceptance="not_sent")
    if category == "authentication":
        if auth_retries_used >= 1:
            return decide("pause_auth", "authentication_retry_exhausted", effects=("diagnose_account_auth",))
        return decide("refresh_token_once", "confirmed_authentication_rejection", effects=("single_flight_refresh", "revalidate_before_retry"))
    if category == "rate_limited":
        if retry_count >= max_transient_retries:
            return decide("pause_retry_budget", "rate_retry_budget_exhausted")
        # 429/50002 do not identify a daily quota or which dimension was hit.
        return decide("wait_rate_limit", "confirmed_rate_rejection", delay=_delay(error, retry_count), effects=("cooldown_affected_route", "no_inferred_daily_quota"))
    if stage == "upload":
        if category == "native_file_unavailable":
            return decide("review_file_route", "native_file_unavailable", effects=("mark_exact_file_capability_temporarily_unavailable", "require_m6_route_authorization"))
        return decide("review_upload_contract", "upload_rejection_requires_review")
    if category == "window_exhausted":
        return decide("close_window_and_reselect", "confirmed_window_exhaustion", effects=("close_exact_window", "select_legal_route_without_reusing_seq"))
    if category == "proactive_denied":
        return decide("wait_window", "confirmed_proactive_denial", effects=("deny_proactive_for_exact_endpoint", "retain_membership_and_other_endpoints"))
    return decide("pause_unknown", "unsupported_error_contract", effects=("no_automatic_resend",))


@dataclass(frozen=True)
class SuccessDecision:
    action: str
    message_acceptance: MessageAcceptance
    native_message_id: str | None = field(default=None, repr=False)
    # A remote success never establishes DB/journal/ledger commit.
    delivery_committed: bool = False


def classify_successful_stage(stage: str, payload: object) -> SuccessDecision:
    """Classify a response AFTER M0 has checked HTTP/body success.

    token/upload payload-specific validation remains with their protocol
    operation; these outcomes grant no delivery commit and no bot identity.
    """
    if stage == "token":
        return SuccessDecision("continue_token_validation", "not_sent")
    if stage == "upload":
        return SuccessDecision("continue_upload_validation", "not_sent")
    if stage not in {"request", "send"} or not isinstance(payload, Mapping):
        return SuccessDecision("quarantine_ambiguous", "unknown")
    if any(name in payload and (type(payload[name]) is not int or payload[name] != 0) for name in ("code", "err_code")):
        return SuccessDecision("quarantine_ambiguous", "unknown")
    native_id = payload.get("id")
    if (
        not isinstance(native_id, str) or not native_id.strip()
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in native_id)
        or len(native_id.encode("utf-8")) > 256
    ):
        return SuccessDecision("quarantine_ambiguous", "unknown")
    return SuccessDecision("persist_accepted_receipt", "accepted", native_id)
