"""Pure QQ quota specs and a single-process local rate waiter, not a send queue.

Limits implement supplied plan 3.5, not live account capability verification.
Trusted runtime configuration supplies certification/day-boundary evidence.
Exactly one gate instance must be shared by ALL send paths for a leased account.
No database transaction or send lease may be held across acquire/sleep.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
import re
import time
from typing import Literal
import unicodedata

from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey

SendMode = Literal["passive", "proactive"]
Certification = Literal["unknown", "unverified", "certified"]
POLICY_REVISION = 1
POLICY_CONTRACT = "qq-plan-3_5-rate-v1-draft"
MAX_TIME = 2**63 - 1
_EVIDENCE = re.compile(r"[a-z0-9_]{1,80}\Z")


class RatePolicyError(ValueError):
    """Fixed safe diagnostics only."""


class RateWaitExpired(TimeoutError):
    pass


def _timestamp(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_TIME:
        raise RatePolicyError("invalid_timestamp")


def _scope(endpoint: EndpointKey, mode: str) -> str:
    if not isinstance(endpoint, EndpointKey) or endpoint.account.platform != "qq-official":
        raise RatePolicyError("official_endpoint_required")
    if (
        any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for value in (endpoint.account.app_id, endpoint.native_conversation_id) for char in value)
        or len(endpoint.account.app_id.encode("utf-8")) > 256
        or len(endpoint.native_conversation_id.encode("utf-8")) > 256
        or len(endpoint.account.key.encode("utf-8")) > 512
        or len(endpoint.key.encode("utf-8")) > 1000
    ):
        raise RatePolicyError("invalid_endpoint_identity")
    if mode not in {"passive", "proactive"}:
        raise RatePolicyError("invalid_send_mode")
    return "group" if endpoint.kind == "group" else "c2c"


def effective_certification(value: Certification) -> Literal["unverified", "certified"]:
    if value not in {"unknown", "unverified", "certified"}:
        raise RatePolicyError("invalid_certification")
    return "certified" if value == "certified" else "unverified"


def _key(prefix: str, identity: tuple) -> str:
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RateSpec:
    account_key: str = field(repr=False)
    endpoint_key: str | None = field(repr=False)
    scene: str
    send_mode: SendMode
    metric: str
    limit_count: int
    interval_seconds: int
    # Conservative smooth dispatch: never bank a multi-message idle burst.
    capacity: int = 1

    @property
    def identity_tuple(self) -> tuple:
        # Profile/limit/revision are not identity: changing configuration must
        # not create an independent bucket with reusable old credits.
        return ("qq-rate-v1", self.account_key, self.endpoint_key,
                self.scene, self.send_mode, self.metric)

    @property
    def rate_key(self) -> str:
        return _key("qq.rate.", self.identity_tuple)


def build_rate_specs(
    endpoint: EndpointKey, send_mode: SendMode, *, certification: Certification = "unknown",
) -> tuple[RateSpec, ...]:
    scene = _scope(endpoint, send_mode)
    certified = effective_certification(certification) == "certified"
    if send_mode == "passive":
        # Window limits are M2's authority. Proactive caps are not all-message
        # limits; passive still goes through durable observed cooldown below.
        return ()
    account_key, endpoint_key = endpoint.account.key, endpoint.key

    def spec(target, metric, count, seconds):
        return RateSpec(account_key, target, scene, send_mode, metric, count, seconds)

    if scene == "group":
        result = [spec(None, "messages_per_minute", 60 if certified else 30, 60)]
    else:
        result = [spec(None, "messages_per_second", 10 if certified else 5, 1)]
        if not certified:
            result.append(spec(None, "messages_per_minute", 30, 60))
    result.append(spec(endpoint_key, "messages_per_minute", 20, 60))
    return tuple(result)


@dataclass(frozen=True)
class VerifiedDayBoundary:
    """Structure for an externally verified fixed provider-day origin.

    Construction itself proves nothing. Only trusted configuration/observations
    may supply this; never infer it from the machine's timezone/local midnight.
    DST/calendar boundaries would need explicit intervals from their verified
    contract; this draft supports only an explicitly verified fixed duration.
    """

    account: AccountKey = field(repr=False)
    origin_at: int
    evidence_code: str
    period_seconds: int = 86400

    def __post_init__(self) -> None:
        if not isinstance(self.account, AccountKey) or self.account.platform != "qq-official":
            raise RatePolicyError("official_account_required")
        _timestamp(self.origin_at)
        if type(self.period_seconds) is not int or not 1 <= self.period_seconds <= 604800:
            raise RatePolicyError("invalid_verified_period")
        if not isinstance(self.evidence_code, str) or not _EVIDENCE.fullmatch(self.evidence_code):
            raise RatePolicyError("invalid_boundary_evidence")


@dataclass(frozen=True)
class QuotaSpec:
    account_key: str = field(repr=False)
    endpoint_key: str = field(repr=False)
    scene: str
    send_mode: SendMode
    metric: str
    period_start_at: int
    period_end_at: int | None
    reset_verified: bool
    limit_count: int = 1000
    policy_revision: int = POLICY_REVISION
    evidence_code: str = "plan_3_5_unverified_reset"
    release_policy: str = "not_dispatched_only"

    @property
    def identity_tuple(self) -> tuple:
        period = ("verified", self.period_start_at, self.period_end_at) if self.reset_verified else ("unverified",)
        return ("qq-quota-v1", self.account_key, self.endpoint_key,
                self.scene, self.send_mode, self.metric, period)

    @property
    def quota_key(self) -> str:
        return _key("", self.identity_tuple)

    @property
    def lookup_guard_tuple(self) -> tuple:
        # Unknown period identity is stable even if a caller accidentally passes
        # a new start. Storage must reject metadata drift instead of resetting it.
        return (*self.identity_tuple, self.period_start_at, self.period_end_at, self.reset_verified)


def daily_quota_spec(
    endpoint: EndpointKey, send_mode: SendMode, *, now: int,
    unverified_period_start: int | None = None,
    verified_boundary: VerifiedDayBoundary | None = None,
) -> QuotaSpec | None:
    """Return an attempt-associated counter spec; never reserve or clear counts.

    Unknown start MUST come from the existing persistent period row, or its
    first creation under the account lock. Never pass each request's `now` as
    a replacement start. Storage compares lookup_guard_tuple on every lookup.
    Crossing to a verified period does not settle or erase old uncertain charges;
    original-period receipt settlement/cross-period conservation belongs to DB.
    """
    scene = _scope(endpoint, send_mode)
    _timestamp(now)
    if send_mode == "passive":
        return None
    if verified_boundary is None:
        _timestamp(unverified_period_start)
        if now < unverified_period_start:
            raise RatePolicyError("unverified_period_in_future")
        start, end, verified, evidence = unverified_period_start, None, False, "plan_3_5_unverified_reset"
    else:
        if not isinstance(verified_boundary, VerifiedDayBoundary) or verified_boundary.account != endpoint.account:
            raise RatePolicyError("boundary_account_mismatch")
        verified_boundary.__post_init__()
        if now < verified_boundary.origin_at:
            raise RatePolicyError("verified_period_not_started")
        elapsed = (now - verified_boundary.origin_at) // verified_boundary.period_seconds
        start = verified_boundary.origin_at + elapsed * verified_boundary.period_seconds
        end = start + verified_boundary.period_seconds
        _timestamp(end)
        verified, evidence = True, verified_boundary.evidence_code
    return QuotaSpec(endpoint.account.key, endpoint.key, scene, send_mode, "endpoint_messages_per_day",
                     start, end, verified, evidence_code=evidence)


@dataclass(frozen=True)
class LocalRatePermit:
    """Single-use local preflight result, NOT durable permission to send.

    prepare_send must require exact account/endpoint/mode/current profile. If a
    passive window expired and proactive is now needed, return a WaitDecision,
    release DB/worker claims, and gate again. Never silently switch modes.
    """

    account_key: str = field(repr=False)
    endpoint_key: str = field(repr=False)
    scene: str
    send_mode: SendMode
    certification: str
    acquired_at_monotonic: float
    policy_revision: int = POLICY_REVISION

    def matches(self, endpoint: EndpointKey, send_mode: SendMode, *, certification: Certification = "unknown") -> bool:
        _scope(endpoint, send_mode)
        return (self.account_key == endpoint.account.key and self.endpoint_key == endpoint.key
                and self.send_mode == send_mode and self.certification == effective_certification(certification)
                and self.policy_revision == POLICY_REVISION)


@dataclass
class _Bucket:
    identity: tuple = field(repr=False)
    limit_count: int
    interval_seconds: int
    capacity: int
    tokens: float
    updated_at: float


class LocalAccountRateGate:
    """One account's shared local waiter; never a second durable queue.

    Async read_blocked_until supplies max applicable durable cooldown in Unix
    seconds and is re-read on every wake. It must finish its read transaction
    before returning. No DB cursor, lease or transaction is accepted here.
    The caller repeats durable cooldown checks in prepare_send to cover changes
    between local gating and reservation. This does not support independent
    sending processes or multiple uncoordinated gate instances for one account.
    """

    def __init__(
        self, account: AccountKey, *, clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, max_buckets: int = 4096,
    ) -> None:
        if not isinstance(account, AccountKey) or account.platform != "qq-official":
            raise RatePolicyError("official_account_required")
        if type(max_buckets) is not int or max_buckets < 1:
            raise RatePolicyError("invalid_bucket_capacity")
        self._account = account
        self._clock, self._wall_clock, self._sleep = clock, wall_clock, sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._last_clock: float | None = None
        self._max_buckets = max_buckets

    def __repr__(self) -> str:
        return "LocalAccountRateGate()"

    def _sample(self) -> tuple[float, float]:
        mono, wall = self._clock(), self._wall_clock()
        if any(type(value) not in {int, float} or not -MAX_TIME <= value <= MAX_TIME or not math.isfinite(value) for value in (mono, wall)):
            raise RatePolicyError("invalid_clock")
        if wall < 0 or wall > MAX_TIME or (self._last_clock is not None and mono < self._last_clock):
            raise RatePolicyError("invalid_clock")
        self._last_clock = mono
        return float(mono), float(wall)

    @staticmethod
    def _deadline_check(mono, wall, deadline, monotonic_deadline):
        if deadline is not None and (wall >= deadline or mono >= monotonic_deadline):
            raise RateWaitExpired("rate_wait_deadline_expired")

    async def acquire(
        self, endpoint: EndpointKey, send_mode: SendMode, *,
        certification: Certification = "unknown",
        read_blocked_until: Callable[[], Awaitable[int | float | None] | int | float | None],
        deadline: int | float | None = None,
    ) -> LocalRatePermit:
        scene = _scope(endpoint, send_mode)
        if endpoint.account != self._account:
            raise RatePolicyError("rate_gate_account_mismatch")
        effective = effective_certification(certification)
        specs = build_rate_specs(endpoint, send_mode, certification=certification)
        if deadline is not None and (type(deadline) not in {int, float} or not 0 <= deadline <= MAX_TIME or not math.isfinite(deadline)):
            raise RatePolicyError("invalid_deadline")
        mono, wall = self._sample()
        monotonic_deadline = None if deadline is None else mono + max(0.0, deadline - wall)
        cooldown_monotonic = mono
        while True:
            mono, wall = self._sample()
            self._deadline_check(mono, wall, deadline, monotonic_deadline)
            blocked_until = read_blocked_until()
            if inspect.isawaitable(blocked_until):
                blocked_until = await blocked_until
            if blocked_until is not None and (
                type(blocked_until) not in {int, float} or not 0 <= blocked_until <= MAX_TIME or not math.isfinite(blocked_until)
            ):
                raise RatePolicyError("invalid_persistent_cooldown")
            mono, wall = self._sample()
            self._deadline_check(mono, wall, deadline, monotonic_deadline)
            if blocked_until is not None:
                cooldown_monotonic = max(cooldown_monotonic, mono + max(0.0, blocked_until - wall))
            async with self._lock:
                mono, wall = self._sample()
                self._deadline_check(mono, wall, deadline, monotonic_deadline)
                missing = sum(spec.rate_key not in self._buckets for spec in specs)
                if len(self._buckets) + missing > self._max_buckets:
                    raise RatePolicyError("local_bucket_capacity_exceeded")
                buckets, wait = [], max(0.0, cooldown_monotonic - mono)
                for spec in specs:
                    key = spec.rate_key
                    bucket = self._buckets.get(key)
                    if bucket is not None and bucket.identity != spec.identity_tuple:
                        raise RatePolicyError("rate_key_collision")
                    if bucket is None or (bucket.limit_count, bucket.interval_seconds, bucket.capacity) != (spec.limit_count, spec.interval_seconds, spec.capacity):
                        # Newly observed target/process or changed trusted policy:
                        # start EMPTY. No profile change can recover old credits.
                        bucket = _Bucket(spec.identity_tuple, spec.limit_count, spec.interval_seconds,
                                         spec.capacity, 0.0, mono)
                        self._buckets[key] = bucket
                    elapsed = mono - bucket.updated_at
                    bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.limit_count / bucket.interval_seconds)
                    bucket.updated_at = mono
                    wait = max(wait, (1.0 - bucket.tokens) * bucket.interval_seconds / bucket.limit_count)
                    buckets.append(bucket)
                if wait <= 0:
                    # All dimensions debit atomically. No awaits between the
                    # first debit and permit return; cancellation cannot half-debit.
                    for bucket in buckets:
                        bucket.tokens -= 1.0
                    return LocalRatePermit(self._account.key, endpoint.key, scene, send_mode, effective, mono)
            # Always outside the local lock, and caller must hold no DB lock.
            delay = max(0.000001, min(wait, 60.0))
            if deadline is not None:
                delay = min(delay, max(0.0, monotonic_deadline - mono), max(0.0, deadline - wall))
            await self._sleep(delay)
