"""A delayed stop notification must neither merge nor discard new-generation input."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from src.plugins.ai_chat.adapters.qq_official.message_batch import MessageBatcher


class BatchGenerationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.batcher = MessageBatcher(AsyncMock())
        self.addAsyncCleanup(self.batcher.close)

    def add(self, text, generation, canonical):
        key = ('TEST_ENDPOINT','TEST_ACTOR',1,generation)
        source = SimpleNamespace(conversation_generation=generation,payload={})
        return self.batcher.add(key,source,text,requested=True,canonical_id=canonical)

    async def test_reconciliation_of_unchanged_generation_keeps_burst_detection(self):
        self.assertFalse(self.add('TEST first',0,1))
        self.batcher.forget('TEST_ENDPOINT',before_generation=0)
        self.assertFalse(self.add('TEST second',0,2))
        self.batcher.forget('TEST_ENDPOINT',before_generation=0)
        self.assertTrue(self.add('TEST third',0,3))

    async def test_delayed_stop_keeps_new_generation_recent_and_pending_input(self):
        self.assertTrue(self.add('那个',0,1))
        self.assertFalse(self.add('TEST new',1,2))
        self.batcher.forget('TEST_ENDPOINT',before_generation=1)
        self.assertFalse(self.add('TEST new second',1,3))
        self.assertTrue(self.add('TEST new third',1,4))
        self.batcher.forget('TEST_ENDPOINT',before_generation=1)
        batch = next(iter(self.batcher.pending.values()))
        self.assertEqual(batch.source.conversation_generation,1)
        self.assertEqual(batch.messages,[(4,'TEST new third')])
