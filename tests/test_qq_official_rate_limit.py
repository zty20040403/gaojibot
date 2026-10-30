"""Clock-controlled account/group limits, including independent endpoints."""
import unittest
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.rate_limit import RateLimiter


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.waits = []
        self.account = AccountKey('qq-official','TEST_RATE','test')
        self.group = EndpointKey(self.account,'group','TEST_GROUP')
        self.private = EndpointKey(self.account,'private','TEST_PRIVATE')
        self.limiter = RateLimiter(clock=lambda:self.now, sleep=self.sleep)

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    async def test_group_twenty_then_wait_without_losing_a_message(self):
        for _ in range(20):
            await self.limiter.acquire(self.group)
        await self.limiter.acquire(self.private)
        self.assertEqual(self.now,0)
        await self.limiter.acquire(self.group)
        self.assertEqual(self.waits,[60])

    async def test_shared_bot_sixty_across_groups_and_private(self):
        for i in range(60):
            await self.limiter.acquire(EndpointKey(self.account,'group',f'TEST_{i}'))
        await self.limiter.acquire(self.private)
        self.assertEqual(self.waits,[60])
