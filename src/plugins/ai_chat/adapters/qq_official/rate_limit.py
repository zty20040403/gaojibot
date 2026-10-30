"""One in-memory rate limiter shared by all endpoints of this bot."""
import asyncio
from collections import deque
import time


class RateLimiter:
    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock, self.sleep = clock, sleep
        self._bot = deque()
        self._groups = {}
        self._lock = asyncio.Lock()

    async def acquire(self, endpoint):
        while True:
            async with self._lock:
                now = self.clock()
                buckets = [(self._bot, 60)]
                if endpoint.kind == 'group':
                    buckets.append((self._groups.setdefault(endpoint.key, deque()), 20))
                delay = 0
                for bucket, limit in buckets:
                    while bucket and bucket[0] <= now - 60:
                        bucket.popleft()
                    if len(bucket) >= limit:
                        delay = max(delay, bucket[0] + 60 - now)
                if delay <= 0:
                    for bucket, _ in buckets:
                        bucket.append(now)
                    return
            await self.sleep(delay)
