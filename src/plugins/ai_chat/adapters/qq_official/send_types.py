"""Values shared by delivery records and the sender."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

from src.plugins.ai_chat.platform_types import EndpointKey
from .lease import LeaseToken


class OutboxInvariantError(RuntimeError):
    """Fixed diagnostic only: never include payloads or opaque identities."""


@dataclass(frozen=True)
class SendOutcome:
    status: str
    reason: str = ""
    delivery_id: int | None = None
    retry_at: int | None = None


@dataclass(frozen=True)
class PlanRef:
    plan_id: str
    delivery_ids: tuple[int, ...]
    created: bool


@dataclass(frozen=True)
class OfficialAttemptContext:
    """Internal reservation data for DeliveryStore.begin_direct_attempt.

    This context is never a model tool schema; the store independently fences
    its token and checks the exact delivery namespace before creating a row.
    """
    token: LeaseToken = field(repr=False)
    expected_attempts: int
    fields: tuple[tuple[str, object], ...] = field(repr=False)


@dataclass(frozen=True)
class SendReservation:
    delivery_id: int
    attempt_id: int
    attempt_number: int
    fencing_epoch: int
    endpoint: EndpointKey = field(repr=False)
    wire_json: str = field(repr=False)
    payload_digest: str = field(repr=False)
    send_by_at: int
    payload_mode: str

    def payload(self) -> dict:
        return json.loads(self.wire_json)


def stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
