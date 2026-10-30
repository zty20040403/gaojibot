"""Conservative, versioned error evidence; never retain remote error bodies."""
from __future__ import annotations

from typing import Literal

ERROR_CONTRACT = "qq-official-plan-2026-10-28"
Acceptance = Literal[False] | None


class QQAPIError(Exception):
    def __init__(
        self,
        category: str,
        *,
        accepted: Acceptance = None,
        status: int | None = None,
        code: int | None = None,
        stage: str = "request",
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.category = category
        self.accepted = accepted
        self.status = status
        self.code = code
        self.stage = stage
        self.request_id = request_id
        self.retry_after = retry_after
        super().__init__(f"QQ {stage}: {category} (HTTP {status}, code {code}, accepted={accepted})")

    @property
    def confirmed_not_accepted(self) -> bool:
        return self.accepted is False


def classify_error(status: int, code: int | None, *, stage: str = "request", **details) -> QQAPIError:
    # A parsed message error code is an explicit rejection. Transport/parse
    # failures remain unknown; uploads retain their separate S3 contract.
    known = {
        40034006: "content_rejected",
        40034128: "window_exhausted",
        40034105: "proactive_denied",
        50002: "rate_limited",
    }
    if code in known:
        category, accepted = known[code], False
    elif stage == "upload" and code == 50015014:
        category, accepted = "native_file_unavailable", False
    elif stage == "upload" and code in {40093001, 40093002}:
        category, accepted = "upload_contract_error", None
    elif status == 401:
        category, accepted = "authentication", False
    elif status == 429:
        category, accepted = "rate_limited", False
    elif code is not None and stage != "upload":
        category, accepted = "platform_rejected", False
    else:
        category, accepted = "unknown", None
    return QQAPIError(category, accepted=accepted, status=status, code=code, stage=stage, **details)
