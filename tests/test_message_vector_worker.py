import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import httpx

from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.bot_storage.async_calls import DatabaseCalls
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_vector_worker import MessageVectorWorker,build_worker
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.semantic_recall import EmbeddingClient,PgVectorBackend,SemanticRecallService
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.qq_official_outbox_fixture import OutboxFixture
from tests import test_message_vectors as vector_fixture


class MessageVectorWorkerTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    message = vector_fixture.MessageVectorTests.message

    def setUp(self):
        super().setUp()
        self.clock = patch('time.time',side_effect=lambda:self.now); self.clock.start(); self.addCleanup(self.clock.stop)
        self.calls = DatabaseCalls(); self.addAsyncCleanup(self.calls.close)
        self.ledger = MessageLedger(self.database); self.addCleanup(self.ledger.close)
        self.jobs = DurableJobStore(self.database); self.addCleanup(self.jobs.close)
        self.requests,self.action = [],None
        async def transport(request):
            self.requests.append(json.loads(request.content))
            if self.action:await self.action()
            return httpx.Response(200,json={'data':[{'index':0,'embedding':[1]+[0]*1023}],'usage':{'prompt_tokens':12}})
        service = SemanticRecallService(EmbeddingClient(base_url='https://embedding.example.invalid/v1',api_key='',model='TEST_EMBED',
            dimensions=1024,transport=httpx.MockTransport(transport)),PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema))
        self.app = SimpleNamespace(settings=SimpleNamespace(semantic_enabled=True),semantic_recall=service,
            model_billing=ModelBilling(self.database,clock=lambda:self.now),db_call=self.calls)
        self.runtime = SimpleNamespace(application=self.app,database=self.database,jobs=self.jobs,_calls=self.calls,logger=Mock(),
            store=SimpleNamespace(admitted=lambda *_:True,clock=lambda:self.now))
        self.worker = MessageVectorWorker(self.runtime,self.token)

    def prepare(self):
        self.message(); self.worker.catalog.enqueue(self.endpoint)

    async def test_owned_worker_accounts_real_usage_and_does_not_repeat_finished_content(self):
        self.prepare()
        self.assertEqual(await self.worker.worker.run_once(),1)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.query('SELECT count(*) AS n FROM semantic_documents')[0]['n'],1)
        receipt = self.query('SELECT * FROM model_usage_calls')[0]
        self.assertEqual((receipt['purpose'],receipt['scope_key'],receipt['actor_key']),('semantic_index',self.endpoint.scope.key,None))
        self.assertEqual((receipt['input_tokens'],receipt['output_tokens'],receipt['profile']),(12,0,'embedding'))
        self.assertIsNotNone(receipt['finished_at']); self.assertFalse(receipt['cost_complete'])
        self.assertEqual(self.worker.catalog.enqueue(self.endpoint),[])
        self.worker = MessageVectorWorker(self.runtime,self.token)
        self.assertEqual(await self.worker.worker.run_once(),0)

    async def test_provider_waiting_after_stop_start_cannot_save_or_repeat_the_original_job(self):
        self.prepare()
        async def revoke():
            with self.tx() as cursor:
                change(cursor,self.endpoint,'stop',now=self.now,actor=self.actor.key,authorized=True)
                change(cursor,self.endpoint,'start',now=self.now+1,actor=self.actor.key,authorized=True)
        self.action = revoke
        self.assertEqual(await self.worker.worker.run_once(),1)
        self.assertEqual(self.query('SELECT * FROM semantic_documents'),[])
        self.assertEqual(self.query('SELECT status FROM durable_jobs')[0]['status'],'failed')
        self.assertEqual(len(self.requests),1)
        self.assertEqual(await self.worker.worker.run_once(),0)
        receipt = self.query('SELECT * FROM model_usage_calls')[0]
        self.assertIsNotNone(receipt['finished_at']); self.assertIsNone(receipt['input_tokens'])

    async def test_stop_start_interrupts_pending_headers_without_waiting_for_provider(self):
        self.prepare(); entered,closed = asyncio.Event(),asyncio.Event()
        async def pending():
            entered.set()
            try:await asyncio.Event().wait()
            finally:closed.set()
        self.action = pending
        task = asyncio.create_task(self.worker.worker.run_once())
        await asyncio.wait_for(entered.wait(),5)
        with self.tx() as cursor:
            change(cursor,self.endpoint,'stop',now=self.now,actor=self.actor.key,authorized=True)
            change(cursor,self.endpoint,'start',now=self.now+1,actor=self.actor.key,authorized=True)
        self.assertEqual(await asyncio.wait_for(task,5),1)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.query('SELECT * FROM semantic_documents'),[])
        self.assertEqual(self.query('SELECT status FROM durable_jobs')[0]['status'],'failed')
        receipt = self.query('SELECT * FROM model_usage_calls')[0]
        self.assertIsNotNone(receipt['finished_at']); self.assertIsNone(receipt['input_tokens'])

    async def test_double_cancel_drains_unknown_billing_and_original_job_is_terminal(self):
        self.prepare(); entered = asyncio.Event()
        async def pending():entered.set(); await asyncio.Event().wait()
        self.action = pending
        task = asyncio.create_task(self.worker.worker.run_once())
        await asyncio.wait_for(entered.wait(),5)
        task.cancel(); await asyncio.sleep(0); task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        receipt = self.query('SELECT * FROM model_usage_calls')[0]
        self.assertIsNotNone(receipt['finished_at']); self.assertIsNone(receipt['input_tokens'])
        self.assertEqual(self.query('SELECT status FROM durable_jobs')[0]['status'],'failed')
        self.assertEqual(await self.worker.worker.run_once(),0)
        self.assertEqual(len(self.requests),1)

    async def test_keyset_polling_is_bounded_fair_and_does_not_generate_model_calls(self):
        for index in range(5):
            endpoint = EndpointKey(self.account,'group','TEST_SCAN_'+str(index)); self.provision(endpoint)
            self.message(endpoint=endpoint,label='TEST_SCAN_MESSAGE_'+str(index))
        first = await self.worker.schedule_once(); second = await self.worker.schedule_once()
        self.assertLessEqual(first,64); self.assertLessEqual(second,64)
        self.assertEqual(first+second,5)
        self.assertEqual(await self.worker.schedule_once(),0)
        self.assertEqual(self.requests,[])

    async def test_disabled_or_unmetered_factory_never_creates_a_model_channel(self):
        self.app.settings.semantic_enabled = False
        self.assertIsNone(build_worker(self.runtime,self.token))
        self.app.settings.semantic_enabled = True; self.app.model_billing = None
        with self.assertRaisesRegex(RuntimeError,'semantic_provider_or_accounting_unavailable'):build_worker(self.runtime,self.token)
        self.assertEqual(self.requests,[])
