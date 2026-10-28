"""Narrow protocol-backed format fallback proposal; no network or state writes.

This adds evidence above the frozen M0 classifier, which calls these codes
unknown. It does not turn arbitrary 4xx, a category string, or a timeout into a
rejection. The exact codes describe argument validation failures in the official
all-API error table. Runtime must persist the source alongside that conclusion.

Every flag/counter here comes from the trusted persisted plan/attempt state;
none is a user/model capability. Selection creates a NEW immutable attempt and
must never alter a sent attempt. The sender still owns all live permission,
lease, window, sequence, deadline, ordering and rate-limit checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.qq_official.errors import QQAPIError

FORMAT_CONTRACT = "qq-common-format-validation-2026-10-28"
FORMAT_SOURCE_URL = "https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html"
FORMAT_SOURCE_HTML_SHA256 = "6a26f56f025240c1aa4356642a85014900a613c3da899ec4d44392f60faebc3c"
FORMAT_PINNED_DOC_COMMIT = "645787a45937e5d9c4f0f61afefdffde0f38696e"
FORMAT_PINNED_DOC_URL = (
    "https://github.com/tencent-connect/bot-docs/blob/"
    + FORMAT_PINNED_DOC_COMMIT
    + "/docs/develop/api-v2/dev-prepare/error-trace/openapi.md"
)

# This is a deliberately closed map, not a family/range heuristic. 50055 is
# underspecified invalid content; 50056 is a capability restriction. Neither
# authorizes format fallback. Source descriptors are paraphrases, not remote
# error text matching. Current docs explicitly cover ALL OpenAPI interfaces.
FORMAT_PARAMETER_CODES = {
    50037: "invalid_markdown_field_combination",
    50041: "empty_markdown_value",
    50042: "markdown_list_length_limit",
    50054: "invalid_markdown_template_parameters",
    50057: "mixed_native_and_template_markdown",
}


@dataclass(frozen=True)
class FormatRejection:
    code: int
    status: int
    stage: str
    reason: str
    accepted: Literal[False] = False
    contract: str = FORMAT_CONTRACT
    source_url: str = FORMAT_SOURCE_URL
    source_sha256: str = FORMAT_SOURCE_HTML_SHA256
    pinned_source_url: str = FORMAT_PINNED_DOC_URL


def classify_format_rejection(error: QQAPIError) -> FormatRejection | None:
    """Recognize one unambiguous validation failure at the message POST boundary.

    Minimal transport scope is HTTP 200 body failure or HTTP 400. 201/202 may
    indicate asynchronous acceptance; 5xx and conflicting status classes stay
    unresolved. Extending statuses later requires an explicit contract review.
    `request` here must mean message POST, never gateway/self GET.
    """
    if not isinstance(error, QQAPIError):
        raise TypeError("M0 QQAPIError required")
    if (
        error.stage not in {"request", "send"}
        or type(error.status) is not int or error.status not in {200, 400}
        or type(error.code) is not int or error.code not in FORMAT_PARAMETER_CODES
        or error.category not in {"unknown", "format_rejected"}
        or (error.accepted is not None and error.accepted is not False)
    ):
        return None
    return FormatRejection(error.code, error.status, error.stage, FORMAT_PARAMETER_CODES[error.code])


@dataclass(frozen=True)
class FormatFallbackDecision:
    action: Literal["select_frozen_plain_once", "no_fallback"]
    reason: str
    rejection: FormatRejection | None = None
    required_checks: tuple[str, ...] = ()


def decide_format_fallback(
    error: QQAPIError, *, attempted_variant: Literal["primary_markdown", "plain_fallback", "primary_text"],
    fallbacks_used: int, frozen_plain_variant_ready: bool,
    part_already_accepted: bool = False, earlier_attempt_unresolved: bool = False,
) -> FormatFallbackDecision:
    """Recommend a single durable format switch, not an in-function retry loop.

    fallbacks_used counts persisted variant switches, not token refreshes or
    ordinary retries of an already selected payload. Existing accepted receipts
    or unresolved earlier attempts prohibit another send even if this particular
    error has clear rejection evidence.
    """
    if type(fallbacks_used) is not int or fallbacks_used < 0:
        raise ValueError("invalid durable fallback counter")
    if attempted_variant not in {"primary_markdown", "plain_fallback", "primary_text"}:
        raise ValueError("unsupported attempted variant")
    if any(type(value) is not bool for value in (frozen_plain_variant_ready, part_already_accepted, earlier_attempt_unresolved)):
        raise ValueError("trusted plan state requires strict booleans")
    if part_already_accepted or earlier_attempt_unresolved:
        return FormatFallbackDecision("no_fallback", "part_has_acceptance_or_unresolved_attempt")
    rejection = classify_format_rejection(error)
    if rejection is None:
        return FormatFallbackDecision("no_fallback", "no_format_rejection_evidence")
    if attempted_variant != "primary_markdown" or fallbacks_used != 0:
        return FormatFallbackDecision("no_fallback", "format_switch_already_used_or_inapplicable", rejection)
    if not frozen_plain_variant_ready:
        return FormatFallbackDecision("no_fallback", "no_valid_frozen_plain_variant", rejection)
    return FormatFallbackDecision(
        "select_frozen_plain_once", "confirmed_parameter_rejection", rejection,
        ("same_account_endpoint_and_logical_part", "persist_variant_switch_before_send",
         "current_lease_route_authorization_and_deadline", "plain_capability_and_wire_limits",
         "fresh_attempt_sequence_and_rate_reservation", "unchanged_frozen_plain_content"),
    )
