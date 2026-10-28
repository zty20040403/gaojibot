"""Internal immutable send handles; none of these objects authorize chat input.

Only locally issued reservation objects may capture transport evidence. A
serialized copy cannot authorize either another request or a late receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import threading
import weakref

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
class SendCandidate:
    delivery_id: int
    endpoint: EndpointKey = field(repr=False)
    send_mode: str
    expires_at: int
    certification: str


@dataclass(frozen=True)
class OfficialAttemptContext:
    """Internal reservation data for DeliveryStore.begin_direct_attempt.

    This context is never a model tool schema; the store independently fences
    its token and checks the exact delivery namespace before creating a row.
    """
    token: LeaseToken = field(repr=False)
    expected_attempts: int
    fields: tuple[tuple[str, object], ...] = field(repr=False)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
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
        require_issued(self)
        return json.loads(self.wire_json)


_issued: weakref.WeakValueDictionary[int, SendReservation] = weakref.WeakValueDictionary()
_lock = threading.RLock()


def issue_reservation(**fields) -> SendReservation:
    """Internal storage factory, called only after a committed reservation."""
    result = SendReservation(**fields)
    with _lock:
        _issued[id(result)] = result
    return result


def require_issued(reservation: SendReservation) -> None:
    with _lock:
        if not isinstance(reservation, SendReservation) or _issued.get(id(reservation)) is not reservation:
            raise OutboxInvariantError("unissued_attempt_handle")


def stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fixed_reason(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", value):
        raise OutboxInvariantError("invalid_reason_code")
    return value
