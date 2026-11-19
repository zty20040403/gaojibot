import asyncio
import unittest
from unittest.mock import Mock

from src.plugins.ai_chat.admin_api.realtime import AdminEventBroker


class AdminEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_subscriber_keeps_every_resource_and_only_one_wakeup(self):
        revisions = {'budgets': 5}
        broker = AdminEventBroker(revisions.copy)
        fast, slow = broker.subscribe(), broker.subscribe()
        slow.set = Mock(wraps=slow.set)
        await asyncio.to_thread(broker.publish, 'budgets')
        self.assertEqual((await broker.receive(fast))['versions'], {'budgets': 5})
        await asyncio.gather(*(asyncio.to_thread(broker.publish_runtime, 'media') for _ in range(64)))
        runtime = await broker.receive(fast)
        self.assertEqual(runtime['resources'], ['media'])
        self.assertNotIn('versions', runtime)
        revisions['budgets'] = 6
        await asyncio.to_thread(broker.publish, 'budgets')
        latest = await broker.receive(slow)
        self.assertEqual((latest['sequence'], latest['resources'], latest['versions']), (66, ['budgets', 'media'], revisions))
        slow.set.assert_called_once()
        broker.unsubscribe(slow)
        broker.publish_runtime('tasks')
        self.assertEqual((await broker.receive(fast))['resources'], ['budgets', 'tasks'])
        self.assertFalse(slow.is_set())
