"""Token-bucket rate limiter shared by all endpoints of one QQ Official bot."""
from __future__ import annotations

import asyncio
import time

BOT_PER_MINUTE = 60
GROUP_PER_MINUTE = 20
WINDOW_SECONDS = 60.0
_EPSILON = 1e-9


class _TokenBucket:
    __slots__ = ("capacity", "rate", "tokens", "updated_at")

    def __init__(self, capacity: int, window_seconds: float = WINDOW_SECONDS) -> None:
        self.capacity = float(capacity)
        self.rate = self.capacity / window_seconds
        self.tokens = float(capacity)
        self.updated_at: float | None = None

    def refill(self, now: float) -> None:
        if self.updated_at is not None and now > self.updated_at:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
        self.updated_at = now

    def deficit_seconds(self) -> float:
        if self.tokens + _EPSILON >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate

    def consume(self) -> None:
        self.tokens = max(0.0, self.tokens - 1.0)


class RateLimiter:
    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.clock, self.sleep = clock, sleep
        self._bot = _TokenBucket(BOT_PER_MINUTE)
        self._groups: dict[str, _TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, endpoint) -> None:
        while True:
            async with self._lock:
                now = float(self.clock())
                buckets = [self._bot]
                if endpoint.kind == "group":
                    buckets.append(self._groups.setdefault(endpoint.key, _TokenBucket(GROUP_PER_MINUTE)))
                delay = 0.0
                for bucket in buckets:
                    bucket.refill(now)
                    delay = max(delay, bucket.deficit_seconds())
                if delay <= 0.0:
                    for bucket in buckets:
                        bucket.consume()
                    return
            await self.sleep(delay)
