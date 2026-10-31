"""Real queue/storage checks and the background loop's interruption boundary."""
import asyncio
import logging
from types import SimpleNamespace
import unittest

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.background_jobs import BackgroundJobs, BackgroundRunner
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.postgres_fixture import isolated_postgres_for_test


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database = isolated_postgres_for_test(self)
        self.queue = DurableJobStore(database)
        ledger = MessageLedger(database)
        self.addCleanup(self.queue.close)
        self.addCleanup(ledger.close)
        message = ledger.record_message(ConversationScope('onebot-v11', 'group', 'TEST_GROUP'),
            native_message_id='TEST_MESSAGE', sender_native_user_id='TEST_ACTOR',
            sender_display='Tester', body=MessageBody((TextNode(0, 'research'),)), occurred_at=100)
        self.source = dict(account_key='TEST_ACCOUNT', endpoint_key='TEST_ENDPOINT', actor_key='TEST_ACTOR',
            scope_key=message.scope_key, source_execution_id=None, canonical_message_id=message.canonical_message_id,
            native_message_id='TEST_MESSAGE', endpoint_generation=1, route_epoch=1)
        self.now = 100
        self.store = BackgroundJobs(self.queue, clock=lambda: self.now)
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)

    def submit(self, key='TEST_ONE'):
        with self.store.transaction() as cursor:
            return self.store.submit(cursor, kind='TEST_AGENT', key=key, source=self.source,
                                     task='research', title='prices', profile='TEST_MODEL')

    def test_three_jobs_limit_idempotency_and_actor_isolation(self):
        first = self.submit()
        self.assertEqual(self.submit()['job_id'], first['job_id'])
        self.submit('TEST_TWO')
        self.submit('TEST_THREE')
        with self.assertRaises(ValueError):
            self.submit('TEST_FOUR')
        with self.assertRaises(ValueError):
            self.store.steer(first['job_id'], 'TEST_ENDPOINT', 'TEST_OTHER_ACTOR', 'leak')
        self.assertFalse(self.store.cancel(first['job_id'], 'TEST_OTHER_ENDPOINT', 'TEST_ACTOR'))
        self.assertTrue(self.store.cancel(first['job_id'], 'TEST_ENDPOINT', 'TEST_ACTOR'))
        self.assertEqual(self.submit('TEST_FOUR')['state'], 'queued')

    async def test_steering_warning_report_and_restart_do_not_rerun_work(self):
        row = self.submit()
        saved = []

        async def nothing(*args):
            pass

        async def save(row, report):
            saved.append(report)

        async def session(*args):
            return SimpleNamespace(profile=None, prompt='TEST', tools=(), execute_tool=nothing, close=nothing)

        async def llm(*args, feedback_provider, **kwargs):
            self.store.steer(row['job_id'], 'TEST_ENDPOINT', 'TEST_ACTOR', 'also memory prices')
            self.now = row['deadline_at'] - 200
            feedback = await feedback_provider()
            self.assertEqual(feedback[0], 'also memory prices')
            self.assertEqual(len(feedback), 2)
            self.assertEqual(await feedback_provider(), [])
            return 'report with sources'

        runner = BackgroundRunner(self.store, calls=self.calls, session_factory=session, save_report=save,
            ask_main=nothing, record_usage=nothing, logger=logging.getLogger(__name__), llm=llm, clock=lambda: self.now)
        await runner.execute(self.queue.get(row['job_id']))
        self.assertEqual(saved, ['report with sources'])
        self.assertEqual(self.store.get(row['job_id'])['state'], 'completed')
        other = self.submit('TEST_RESTART')
        self.store.update(other['job_id'], state='running')
        self.store.recover('TEST_ACCOUNT')
        await runner.execute(self.queue.get(other['job_id']))
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.store.get(other['job_id'])['state'], 'failed')
        queued = self.submit('TEST_QUEUE_TERMINATED')
        self.queue.cancel(queued['job_id'])
        self.store.pending_results('TEST_ACCOUNT')
        self.assertEqual(self.store.get(queued['job_id'])['state'], 'failed')

    async def test_cancel_stops_model_without_claiming_success(self):
        row = self.submit()

        async def nothing(*args):
            pass

        async def session(*args):
            return SimpleNamespace(profile=None, prompt='TEST', tools=(), execute_tool=nothing, close=nothing)

        async def llm(*args, feedback_provider, **kwargs):
            self.store.cancel(row['job_id'], 'TEST_ENDPOINT', 'TEST_ACTOR')
            await feedback_provider()
            self.fail('A cancelled job continued')

        runner = BackgroundRunner(self.store, calls=self.calls, session_factory=session, save_report=nothing,
            ask_main=nothing, record_usage=nothing, logger=logging.getLogger(__name__), llm=llm, clock=lambda: self.now)
        with self.assertRaises(asyncio.CancelledError):
            await runner.execute(self.queue.get(row['job_id']))
        self.assertEqual(self.store.get(row['job_id'])['state'], 'cancelled')
