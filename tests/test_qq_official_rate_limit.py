"""Clock-controlled token-bucket account/group limits, including independent endpoints."""
import unittest

from src.plugins.ai_chat.adapters.qq_official.rate_limit import RateLimiter
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.waits = []
        self.account = AccountKey("qq-official", "TEST_RATE", "test")
        self.group = EndpointKey(self.account, "group", "TEST_GROUP")
        self.private = EndpointKey(self.account, "private", "TEST_PRIVATE")
        self.limiter = RateLimiter(clock=lambda: self.now, sleep=self.sleep)

    async def sleep(self, seconds):
        self.waits.append(round(seconds, 6))
        self.now += seconds

    async def test_group_twenty_burst_then_token_refill_without_losing_messages(self):
        for _ in range(20):
            await self.limiter.acquire(self.group)
        await self.limiter.acquire(self.private)
        self.assertEqual(self.now, 0.0)
        await self.limiter.acquire(self.group)
        self.assertEqual(self.waits, [3.0])
        self.now += 15.0
        self.waits.clear()
        for _ in range(5):
            await self.limiter.acquire(self.group)
        self.assertEqual(self.waits, [])
        await self.limiter.acquire(self.group)
        self.assertEqual(self.waits, [3.0])

    async def test_shared_bot_sixty_burst_across_groups_and_private(self):
        for i in range(60):
            await self.limiter.acquire(EndpointKey(self.account, "group", f"TEST_{i}"))
        await self.limiter.acquire(self.private)
        self.assertEqual(self.waits, [1.0])
