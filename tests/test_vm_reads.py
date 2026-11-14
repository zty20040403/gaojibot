"""Scoped VM reads over real HTTP/SQL; only public web providers are doubled."""
import asyncio
from dataclasses import replace
import unittest
import httpx
from unittest.mock import AsyncMock, patch

from tests import test_vm_lifecycle as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.message_vectors import REVISION,embedding_identity
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.semantic_recall import EmbeddingClient,PgVectorBackend,SemanticDocument,SemanticRecallService
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.web_fetch import FetchedPage
from src.plugins.ai_chat.web_search import SearchResult


class VMReadTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.VMLifecycleTests.asyncSetUp(self)
        self.base = f'http://127.0.0.1:{self.service.gateway.port}'
        self.reads = self.lifecycle.http.reads
        self.app = self.reads.app
        self.app.settings = replace(self.app.settings,search_enabled=True)
        self.app.db_call = self.service.runtime._calls
        self.app.message_ledger = MessageLedger(self.database)
        self.addCleanup(self.app.message_ledger.close)

    async def post(self, route, **arguments):
        return await self.client.post(self.base+'/vm/'+route,json=arguments)

    async def test_history_is_physical_scoped_and_still_works_when_stopped_without_sender(self):
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        for endpoint,text in ((self.endpoint,'TEST_NEEDLE own'),(other,'TEST_NEEDLE hidden')):
            actor = ActorKey(self.account,'group_member',endpoint.native_conversation_id,'TEST_ACTOR')
            self.app.message_ledger.record_message(endpoint.scope,native_message_id='TEST_MESSAGE',sender_native_user_id='TEST_ACTOR',
                sender_display='TEST_DISPLAY',body=MessageBody((TextNode(0,text),)),occurred_at=self.now,
                actor=actor,source_endpoint=endpoint)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,programs_enabled) VALUES (?,TRUE,FALSE)',(self.endpoint.scope.key,))
        response = await self.post('history',query='TEST_NEEDLE')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('own',response.text); self.assertNotIn('hidden',response.text)
        self.assertEqual(self.query('SELECT * FROM program_quota'),[])
        self.assertEqual(self.query('SELECT * FROM usage_events'),[])
        self.assertEqual((await self.post('history',query='TEST',scope_key=other.scope.key)).status_code,400)
        self.assertEqual((await self.post('history',limit=True)).status_code,400)
        self.assertEqual((await self.post('history')).status_code,400)

    async def test_vm_history_semantic_query_is_fixed_host_read_and_not_a_human_model_channel(self):
        ledger = self.app.message_ledger
        message = ledger.record_message(self.endpoint.scope,native_message_id='TEST_SEMANTIC_EVIDENCE',sender_native_user_id='TEST_ACTOR',
            sender_display='TEST_DISPLAY',body=MessageBody((TextNode(0,'TEST SlidesForge builds presentations'),)),occurred_at=self.now,
            actor=ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_ACTOR'),source_endpoint=self.endpoint)
        requests = []
        def provider(request):
            requests.append(request)
            return httpx.Response(200,json={'data':[{'index':0,'embedding':[1]+[0]*1023}],'usage':{'prompt_tokens':8}})
        embedder = EmbeddingClient(base_url='https://embedding.example.invalid/v1',api_key='',model='TEST_EMBED',dimensions=1024,
            transport=httpx.MockTransport(provider))
        backend = PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema)
        revision = self.query(f'SELECT {REVISION} AS revision FROM messages_with_captions m WHERE canonical_message_id=?',
            (message.canonical_message_id,))[0]['revision']
        backend.upsert(SemanticDocument(self.endpoint.scope.key,'message','msg#'+str(message.canonical_message_id),message.prompt_text,
            {'source_revision':revision,'embedding_identity':embedding_identity(embedder)}),[1]+[0]*1023,model=embedder.model)
        self.app.settings = replace(self.app.settings,semantic_enabled=True)
        self.app.model_billing = ModelBilling(self.database)
        self.app.semantic_recall = SemanticRecallService(embedder,backend)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,programs_enabled) VALUES (?,TRUE,FALSE)',(self.endpoint.scope.key,))
        response = await self.post('history',query='怎么排版幻灯片')
        self.assertEqual(response.status_code,200,response.text); self.assertIn('SlidesForge',response.text)
        row = self.query('SELECT * FROM model_usage_calls')[0]
        self.assertEqual((row['purpose'],row['input_tokens'],row['actor_key']),('semantic_index',8,None))
        self.assertEqual(row['scope_key'],self.endpoint.scope.key); self.assertIsNotNone(row['finished_at'])
        self.assertEqual((await self.post('history',query='TEST',model='TEST_ARBITRARY')).status_code,400)
        self.assertEqual(len(requests),1)

    async def test_web_tools_keep_host_provider_and_public_only_fetch_policy(self):
        search = AsyncMock(return_value=[SearchResult('TEST_TITLE','https://example.test','TEST_SNIPPET')])
        fetch = AsyncMock(return_value=FetchedPage('https://example.test','TEST_TITLE','TEST_BODY'))
        with patch('src.plugins.ai_chat.tools.sources.search_web',search),patch('src.plugins.ai_chat.tools.sources.fetch_page',fetch):
            response = await self.post('web-search',query='TEST_QUERY')
            self.assertEqual((response.status_code,response.json()['results'][0]['title']),(200,'TEST_TITLE'))
            response = await self.post('web-fetch',url='https://example.test')
            self.assertEqual((response.status_code,response.json()['text']),(200,'TEST_BODY'))
            self.assertEqual((await self.post('web-search',query='TEST',api_key='TEST_KEY')).status_code,400)
            self.app.settings = replace(self.app.settings,search_enabled=False)
            self.assertEqual((await self.post('web-search',query='TEST')).status_code,403)
        search.assert_awaited_once(); fetch.assert_awaited_once()
        self.app.settings = replace(self.app.settings,search_enabled=True)
        blocked = await self.post('web-fetch',url='http://127.0.0.1:9/TEST')
        self.assertEqual(blocked.status_code,502,blocked.text)
        self.assertIn('公网',blocked.json()['error'])
        task_token,_ = self.service.gateway.open(source={'job_id':1},current=AsyncMock())
        denied = await self.client.post(self.base+'/vm/history',headers={'Authorization':'Bearer '+task_token},json={'query':'TEST'})
        self.assertEqual(denied.status_code,401)
        self.assertEqual((await self.client.post(self.base+'/vm/history',content=b'X'*16385)).status_code,413)

    async def test_read_concurrency_is_bounded_and_stop_does_not_cancel_reads(self):
        entered,release = asyncio.Event(),asyncio.Event()
        calls = 0
        async def slow(*args,**kwargs):
            nonlocal calls
            calls += 1
            if calls==4:
                entered.set()
            await release.wait()
            return FetchedPage('https://example.test','TEST_TITLE','TEST_BODY')
        with patch('src.plugins.ai_chat.tools.sources.fetch_page',slow):
            requests = [asyncio.create_task(self.post('web-fetch',url='https://example.test')) for _ in range(4)]
            try:
                await asyncio.wait_for(entered.wait(),5)
                full = await self.post('web-fetch',url='https://example.test')
                self.assertEqual((full.status_code,full.json()['error']),(429,'program_reads_busy'))
                self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
                release.set()
                replies = await asyncio.gather(*requests)
                self.assertEqual([item.status_code for item in replies],[200]*4)
            finally:
                release.set()
                await asyncio.gather(*requests,return_exceptions=True)
        self.assertEqual(self.reads.active,{})

    async def test_timeout_client_disconnect_and_rotation_cancel_the_actual_tool(self):
        for reason in ('timeout','disconnect','rotation'):
            with self.subTest(reason=reason):
                entered,cancelled = asyncio.Event(),asyncio.Event()
                async def slow(*args,**kwargs):
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()
                with patch('src.plugins.ai_chat.tools.sources.fetch_page',slow),patch(
                        'src.plugins.ai_chat.adapters.qq_official.program_reads.READ_TIMEOUT',0.1 if reason=='timeout' else 60):
                    pending = asyncio.create_task(self.post('web-fetch',url='https://example.test'))
                    try:
                        await asyncio.wait_for(entered.wait(),3)
                        if reason=='disconnect':
                            pending.cancel()
                        elif reason=='rotation':
                            self.boot.write_text('00000000-0000-4000-8000-000000000002')
                            await self.lifecycle.ready(self.owner,'s123456')
                        if reason!='disconnect':
                            response = await asyncio.wait_for(pending,3)
                            self.assertEqual(response.status_code,408 if reason=='timeout' else 401,response.text)
                        await asyncio.wait_for(cancelled.wait(),3)
                    finally:
                        pending.cancel()
                        await asyncio.gather(pending,return_exceptions=True)
        self.assertEqual(self.reads.active,{})
