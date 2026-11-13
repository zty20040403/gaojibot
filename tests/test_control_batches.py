"""A delayed stop notification must neither merge nor discard new-generation input."""
import asyncio
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

    async def test_idle_timer_flushes_one_complete_input_without_database_timing(self):
        self.batcher.idle = .01
        self.assertTrue(self.add('那个',0,1))
        self.assertTrue(self.add('TEST second',0,2))
        self.assertTrue(self.add('TEST third',0,3))
        task = next(iter(self.batcher.tasks))
        await asyncio.wait_for(task,1)
        self.batcher.dispatch.assert_awaited_once()
        self.assertEqual(self.batcher.dispatch.await_args.args[0].payload['batch_message_ids'],[1,2,3])
        self.assertEqual(self.batcher.pending,{})

    async def test_maximum_flushes_even_when_idle_deadline_is_still_far_away(self):
        now = [0.0]
        self.batcher.clock = lambda:now[0]
        self.batcher.idle,self.batcher.maximum = 60,20
        self.add('那个',0,1)
        task = next(iter(self.batcher.tasks))
        await asyncio.sleep(0)
        now[0] = 19
        self.add('TEST continued',0,2)
        now[0] = 20
        next(iter(self.batcher.pending.values())).changed.set()
        await asyncio.wait_for(task,1)
        self.batcher.dispatch.assert_awaited_once()
        self.assertEqual(self.batcher.pending,{})

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
