import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import httpx

from src.plugins.ai_chat.billing_context import BillingContext,billing_scope
from src.plugins.ai_chat.config import Settings
from src.plugins.ai_chat.runtime import build_app_context
from src.plugins.ai_chat.semantic_recall import EmbeddingClient,SemanticDocument
from tests.postgres_fixture import postgres_for_test


class EmbeddingProtocolTests(unittest.IsolatedAsyncioTestCase):
    def client(self,handler):
        return EmbeddingClient(base_url='https://embedding.example.invalid/v1',api_key='TEST_KEY',model='TEST_EMBED',
            dimensions=3,transport=httpx.MockTransport(handler))

    async def test_batch_indices_must_be_complete_unique_exact_integers(self):
        for indices in ((0,0),(1,1),(-1,0),(0,2),(0,None),(0,True),(0,'1'),(0,1.0)):
            with self.subTest(indices=indices):
                client = self.client(lambda _:httpx.Response(200,json={'data':[{'index':i,'embedding':[1,0,0]} for i in indices]}))
                with self.assertRaises((ValueError,RuntimeError)):await client.embed(['first','second'])
        client = self.client(lambda _:httpx.Response(200,json={'data':[{'index':1,'embedding':[0,1,0]},{'index':0,'embedding':[1,0,0]}]}))
        self.assertEqual(await client.embed(['first','second']),[[1,0,0],[0,1,0]])

    async def test_vectors_must_be_numeric_finite_nonzero_and_exact_count(self):
        for vector in ([0,0,0],[True,0,1],['1',0,1],[1,None,1],[1,2],[1,float('nan'),0],[1,float('inf'),0]):
            with self.subTest(vector=vector),self.assertRaises((ValueError,RuntimeError)):
                await self.client(lambda _:httpx.Response(200,content=json.dumps({'data':[{'index':0,'embedding':vector}]}).encode())).embed(['TEST_TEXT'])
        for data in ([{'index':0,'embedding':[1,0,0]},False],[],[{'embedding':[1,0,0]}]):
            with self.subTest(data=data),self.assertRaises((ValueError,RuntimeError)):
                await self.client(lambda _:httpx.Response(200,json={'data':data})).embed(['TEST_TEXT'])

    async def test_redirect_errors_large_bodies_are_safe_and_never_retried(self):
        for response in (httpx.Response(302,headers={'Location':'https://other.example.invalid/TEST_KEY'}),
            httpx.Response(429,json={'error':{'message':'TEST_KEY'}}),httpx.Response(200,content=b'x'*(4*1024*1024+1))):
            seen = []
            def handle(request):seen.append(request); return response
            with self.assertRaises(RuntimeError) as error:await self.client(handle).embed(['TEST_TEXT'])
            self.assertNotIn('TEST_KEY',str(error.exception)); self.assertNotIn('example.invalid',str(error.exception))
            self.assertEqual(len(seen),1)

    async def test_untrusted_endpoint_syntax_is_rejected_and_explicit_keyless_local_is_allowed(self):
        for url in ('https://TEST_KEY@embedding.example.invalid/v1','https://embedding.example.invalid/v1?secret=TEST_KEY',
            'https://embedding.example.invalid:TEST_KEY/v1','https://embedding.example.invalid/v1#fragment',
            'https://embedding.example.invalid/v1/../hidden',
            'https://embedding.example.invalid/v1\\hidden'):
            with self.subTest(url=url),self.assertRaisesRegex(ValueError,'invalid_embedding_endpoint'):
                EmbeddingClient(base_url=url,api_key='',model='TEST_EMBED',dimensions=3)
        client = EmbeddingClient(base_url='http://127.0.0.1:11434/v1',api_key='',model='TEST_EMBED',dimensions=3)
        self.assertTrue(client.configured)

    async def test_deadline_bounds_waiting_for_headers_without_retry(self):
        seen = []
        async def waiting(request):seen.append(request); await asyncio.sleep(2)
        client = self.client(waiting); client.timeout_seconds = .01
        with self.assertRaises(TimeoutError):await client.embed(['TEST_TEXT'])
        self.assertEqual(len(seen),1)

    async def test_double_cancellation_drains_original_unknown_receipt_once(self):
        entered,finishing,release = asyncio.Event(),asyncio.Event(),asyncio.Event()
        rows = []
        async def finish(call,usage):
            finishing.set(); await release.wait(); rows.append((call,usage))
        store = SimpleNamespace(begin=lambda *_:'TEST_CALL',finish=finish)
        async def calls(function,*args):return await function(*args) if asyncio.iscoroutinefunction(function) else function(*args)
        async def handler(_):entered.set(); await asyncio.Event().wait()
        with billing_scope(BillingContext(store,'TEST_SOURCE',calls)):
            task = asyncio.create_task(self.client(handler).embed(['TEST_TEXT']))
        await asyncio.wait_for(entered.wait(),1); task.cancel()
        await asyncio.wait_for(finishing.wait(),1); task.cancel(); release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(rows),1); self.assertIsNone(rows[0][1].input_tokens)

    async def test_bad_vectors_still_record_reported_usage_and_missing_usage_stays_unknown(self):
        for body,expected in (({'data':[{'index':0,'embedding':[0,0,0]}],'usage':{'prompt_tokens':9}},9),
            ({'data':[{'index':0,'embedding':[1,0,0]}]},None)):
            rows = []
            store = SimpleNamespace(begin=lambda *_:'TEST_CALL',finish=lambda call,usage:rows.append(usage))
            async def calls(function,*args):return function(*args)
            with billing_scope(BillingContext(store,'TEST_SOURCE',calls)):
                if expected is not None:
                    with self.assertRaises(ValueError):await self.client(lambda _:httpx.Response(200,json=body)).embed(['TEST_TEXT'])
                else:await self.client(lambda _:httpx.Response(200,json=body)).embed(['TEST_TEXT'])
            self.assertEqual(len(rows),1); self.assertEqual(rows[0].input_tokens,expected)

    async def test_host_usage_is_finished_with_original_embedding_profile_and_source_guard(self):
        rows = []
        store = SimpleNamespace(begin=lambda source,profile:rows.append([source,profile,None]) or 'TEST_CALL',
            finish=lambda call,usage:rows[-1].__setitem__(2,usage))
        async def calls(function,*args):return function(*args)
        checks = 0
        async def current():
            nonlocal checks
            checks += 1
        context = BillingContext(store,'TEST_SOURCE',calls,current)
        client = self.client(lambda _:httpx.Response(200,json={'data':[{'index':0,'embedding':[1,0,0]}],
            'usage':{'prompt_tokens':12,'total_tokens':12}}))
        with billing_scope(context):await client.embed(['TEST_TEXT'])
        self.assertEqual(len(rows),1); self.assertEqual(rows[0][0],'TEST_SOURCE')
        self.assertEqual((rows[0][1].provider,rows[0][1].name,rows[0][1].model),('openai-embeddings','embedding','TEST_EMBED'))
        self.assertIsNone(rows[0][1].pricing); self.assertEqual((rows[0][2].input_tokens,rows[0][2].output_tokens),(12,0))
        self.assertGreaterEqual(checks,3)

    async def test_physical_header_guard_after_connection_delay_prevents_dispatch(self):
        allowed,sent = True,[]
        async def current():
            if not allowed:raise PermissionError('TEST_ORIGINAL_SOURCE_REVOKED')
        async def transport(request):
            nonlocal allowed
            allowed = False
            await request.extensions['trace']('http11.send_request_headers.started',{})
            sent.append(request)
        store = SimpleNamespace(begin=lambda *_:'TEST_CALL',finish=lambda *_:None)
        async def calls(function,*args):return function(*args)
        with billing_scope(BillingContext(store,'TEST_SOURCE',calls,current)),self.assertRaises(PermissionError):
            await self.client(transport).embed(['TEST_TEXT'])
        self.assertEqual(sent,[])

    async def test_permission_change_after_headers_blocks_result_but_keeps_real_usage(self):
        rows,current_allowed = [],True
        store = SimpleNamespace(begin=lambda *_:'TEST_CALL',finish=lambda call,usage:rows.append(usage))
        async def calls(function,*args):return function(*args)
        async def current():
            if not current_allowed:raise PermissionError('TEST_REVOKED')
        def handle(_):
            nonlocal current_allowed
            current_allowed = False
            return httpx.Response(200,json={'data':[{'index':0,'embedding':[1,0,0]}],'usage':{'prompt_tokens':7}})
        with billing_scope(BillingContext(store,'TEST_SOURCE',calls,current)),self.assertRaises(PermissionError):
            await self.client(handle).embed(['TEST_TEXT'])
        self.assertEqual(len(rows),1)
        # Headers-only revocation may prevent consuming the usage; never guess.
        self.assertIn(rows[0].input_tokens,(None,7))


class EmbeddingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_application_keyless_runtime_indexes_in_current_postgres_schema(self):
        database = postgres_for_test(self)
        settings = replace(Settings.from_env({}),postgres_dsn=database.dsn,postgres_schema=database.schema,
            semantic_enabled=True,embedding_api_key='',embedding_base_url='https://embedding.example.invalid/v1',
            media_enabled=False,archive_enabled=False)
        with tempfile.TemporaryDirectory() as temporary,patch('src.plugins.ai_chat.runtime.PostgresDatabase',return_value=database):
            context = build_app_context(settings,state_dir=Path(temporary),project_root=Path(temporary),logger=Mock())
            try:
                self.assertIsNotNone(context.semantic_recall)
                context.semantic_recall.embedder._transport = httpx.MockTransport(lambda request:httpx.Response(200,json={
                    'data':[{'index':0,'embedding':[1]+[0]*1023}]}))
                self.assertEqual(await context.semantic_recall.index([SemanticDocument('TEST_SCOPE','message','TEST_MESSAGE','TEST_CONTENT',{})]),1)
                self.assertEqual((await context.semantic_recall.stats())['documents'],1)
            finally:await context.shutdown()
