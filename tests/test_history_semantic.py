import asyncio
import json
from types import SimpleNamespace
import unittest

import httpx

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.billing import scoped
from src.plugins.ai_chat.billing_context import billing_scope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.message_vectors import MessageVectorJobs
from src.plugins.ai_chat.platform_types import InboundMessage,ProgramToolSource
from src.plugins.ai_chat.semantic_recall import EmbeddingClient,PgVectorBackend,SemanticRecallService
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.tools.history import history
from src.plugins.ai_chat.tools.state import ToolContext,TurnState
from tests.qq_official_outbox_fixture import OutboxFixture
from tests import test_message_recall as recall_fixture


class HistorySemanticTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    message = recall_fixture.MessageRecallTests.message
    index = recall_fixture.MessageRecallTests.index

    def setUp(self):
        super().setUp()
        self.ledger = MessageLedger(self.database); self.addCleanup(self.ledger.close)
        self.jobs = DurableJobStore(self.database); self.addCleanup(self.jobs.close)
        self.calls = DatabaseCalls(); self.addAsyncCleanup(self.calls.close)
        self.count,self.requests,self.action,self.allowed = 0,[],None,True
        async def provider(request):
            self.requests.append(json.loads(request.content))
            if self.action:await self.action()
            return httpx.Response(200,json={'data':[{'index':0,'embedding':[1]+[0]*1023}],'usage':{'prompt_tokens':9}})
        self.embedder = EmbeddingClient(base_url='https://embedding.example.invalid/v1',api_key='',model='TEST_EMBED',
            dimensions=1024,transport=httpx.MockTransport(provider))
        self.backend = PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema)
        self.catalog = MessageVectorJobs(self.database,self.jobs,self.token,self.backend,self.embedder,admission=lambda *_:True,clock=lambda:self.now)
        self.app = SimpleNamespace(message_ledger=self.ledger,db_call=self.calls,settings=SimpleNamespace(semantic_enabled=True),
            semantic_recall=SemanticRecallService(self.embedder,self.backend),model_billing=ModelBilling(self.database,clock=lambda:self.now))
        self.inbound = InboundMessage(self.endpoint,self.actor,None,'TEST_QUERY_SOURCE','TEST_PERSON','member',
            MessageBody((TextNode(0,'TEST query'),)),True,None,self.now,None)

    async def current(self):
        if not self.allowed:raise PermissionError('TEST_SOURCE_REVOKED')

    def turn(self,*,program=False):
        context = ToolContext(SimpleNamespace(context=self.app),capabilities=frozenset({'context'}),
            inbound=None if program else self.inbound,
            program_source=ProgramToolSource(self.endpoint,'manual','TEST_MANUAL_READ') if program else None)
        return TurnState(context)

    async def call(self,*,program=False,**arguments):
        with scoped(self.app,self.endpoint,'semantic_index' if program else 'turn',None if program else self.actor,current=self.current):
            return json.loads(await history(self.turn(program=program),arguments))

    async def test_paraphrase_tool_call_has_same_output_shape_and_real_query_usage(self):
        target = self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        result = await self.call(query='怎么排版幻灯片',limit=1)
        self.assertTrue(result['ok']); self.assertIn('#'+str(target.canonical_message_id),result['results'][0])
        self.assertIn('around=#',result['hint']); self.assertEqual(len(self.requests),1)
        receipt = self.query('SELECT * FROM model_usage_calls')[0]
        self.assertEqual((receipt['purpose'],receipt['actor_key'],receipt['input_tokens'],receipt['output_tokens']),('turn',self.actor.key,9,0))
        self.assertIsNotNone(receipt['finished_at']); self.assertFalse(receipt['cost_complete'])

    async def test_disabled_keeps_keyword_path_and_read_modes_never_embed(self):
        target = self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        self.app.settings.semantic_enabled = False
        result = await self.call(query='SlidesForge')
        self.assertTrue(result['ok']); self.assertIn('SlidesForge',result['results'][0])
        self.app.settings.semantic_enabled = True
        self.assertTrue((await self.call(around='#'+str(target.canonical_message_id)))['ok'])
        self.assertTrue((await self.call(who='TEST_PERSON'))['ok'])
        self.assertEqual(self.requests,[])

    async def test_future_date_is_an_empty_range_not_an_unbounded_read(self):
        self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        result = await self.call(**{'from':'2099-01-01'})
        self.assertTrue(result['ok']); self.assertEqual(result['lines'],['（没有这样的消息）'])
        self.assertEqual(self.requests,[])

    async def test_enabled_provider_failure_never_silently_returns_keyword_hits(self):
        self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        async def fail():raise httpx.ConnectError('TEST_PROVIDER_FAILURE')
        self.action = fail
        result = await self.call(query='SlidesForge')
        self.assertFalse(result['ok']); self.assertNotIn('results',result)
        self.assertNotIn('embedding.example.invalid',result['error']); self.assertEqual(len(self.requests),1)
        self.assertIsNotNone(self.query('SELECT finished_at FROM model_usage_calls')[0]['finished_at'])

    async def test_no_billing_guard_and_program_borrowing_human_context_never_reach_provider(self):
        with billing_scope(None):result = json.loads(await history(self.turn(),{'query':'TEST'}))
        self.assertFalse(result['ok'])
        with scoped(self.app,self.endpoint,'turn',self.actor,current=self.current):
            result = json.loads(await history(self.turn(program=True),{'query':'TEST'}))
        self.assertFalse(result['ok']); self.assertEqual(self.requests,[])
        self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        result = await self.call(program=True,query='怎么排版幻灯片')
        self.assertTrue(result['ok']); self.assertIsNone(self.query('SELECT actor_key FROM model_usage_calls')[0]['actor_key'])

    async def test_provider_headers_revocation_cancels_socket_and_keeps_unknown_receipt(self):
        self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        entered,closed = asyncio.Event(),asyncio.Event()
        async def pending():
            entered.set()
            try:await asyncio.Event().wait()
            finally:closed.set()
        self.action = pending
        task = asyncio.create_task(self.call(query='怎么排版幻灯片'))
        await asyncio.wait_for(entered.wait(),5); self.allowed = False
        result = await asyncio.wait_for(task,5)
        self.assertFalse(result['ok']); self.assertTrue(closed.is_set())
        receipt = self.query('SELECT * FROM model_usage_calls')[0]
        self.assertIsNotNone(receipt['finished_at']); self.assertIsNone(receipt['input_tokens'])

    async def test_final_source_revocation_keeps_cost_but_returns_no_evidence(self):
        self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        async def calls(function,*args,**kwargs):
            result = await self.calls(function,*args,**kwargs)
            if function==self.ledger.visible_messages_by_ids:self.allowed = False
            return result
        self.app.db_call = calls
        result = await self.call(query='怎么排版幻灯片')
        self.assertFalse(result['ok']); self.assertNotIn('results',result)
        self.assertEqual(self.query('SELECT input_tokens FROM model_usage_calls')[0]['input_tokens'],9)

    async def test_double_cancellation_drains_same_query_receipt(self):
        self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        entered,finishing,release = asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def pending():
            entered.set(); await asyncio.Event().wait()
        async def calls(function,*args,**kwargs):
            if function==self.app.model_billing.finish:
                finishing.set(); await release.wait()
            return await self.calls(function,*args,**kwargs)
        self.action,self.app.db_call = pending,calls
        task = asyncio.create_task(self.call(query='怎么排版幻灯片'))
        try:
            await asyncio.wait_for(entered.wait(),5); task.cancel()
            await asyncio.wait_for(finishing.wait(),5); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done()); release.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,5)
            rows = self.query('SELECT * FROM model_usage_calls')
            self.assertEqual(len(rows),1); self.assertIsNotNone(rows[0]['finished_at'])
        finally:
            release.set(); task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_late_visibility_clear_during_final_hydration_drops_earlier_candidates(self):
        target = self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        async def calls(function,*args,**kwargs):
            if function==self.ledger.visible_messages_by_ids:
                self.query('UPDATE conversation_visibility SET min_canonical_message_id=?',(target.canonical_message_id+1,))
            return await self.calls(function,*args,**kwargs)
        self.app.db_call = calls
        result = await self.call(query='怎么排版幻灯片')
        self.assertTrue(result['ok']); self.assertEqual(result['results'],['（没有搜到）'])
