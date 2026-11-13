import asyncio
from dataclasses import replace
from decimal import Decimal
import logging
import unittest

import httpx

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.billing_context import BillingContext
from src.plugins.ai_chat.codex_gateway import CodexGateway
from tests import test_model_billing as fixture
from tests.test_codex_gateway import free_port
from tests.test_responses_usage import USAGE,event


class CodexBillingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.ModelBillingTests.setUp(self)
        self.calls = DatabaseCalls(); self.addAsyncCleanup(self.calls.close)
        self.context = BillingContext(self.store,self.source,self.calls)
        self.profile = replace(self.profile,model='TEST_HOST_MODEL',base_url='http://upstream.test/v1')

    async def start(self,handler):
        self.gateway = CodexGateway(port=free_port(),upstream=lambda:self.profile,model=lambda:('TEST_HOST_MODEL','high'),
            logger=logging.getLogger(__name__),client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            billing=lambda channel:self.context)
        await self.gateway.start(); self.addAsyncCleanup(self.gateway.close)
        token,_ = self.gateway.open()
        self.client = httpx.AsyncClient(trust_env=False); self.addAsyncCleanup(self.client.aclose)
        return token,{'Authorization':'Bearer '+token}

    async def post(self,headers,path='/responses',**body):
        return await self.client.post(f'http://127.0.0.1:{self.gateway.port}/v1'+path,headers=headers,json=body)

    async def settled(self,count=1):
        def read():
            with self.store.transaction() as cursor:return [dict(row) for row in cursor.execute('SELECT * FROM model_usage_calls').fetchall()]
        async with asyncio.timeout(5):
            while True:
                rows = await self.calls(read)
                if len(rows)==count and all(row['finished_at'] is not None for row in rows):return rows
                await asyncio.sleep(.01)

    async def test_only_host_response_counts_and_matching_upstream_model_prices_are_used(self):
        def handler(request):
            self.assertEqual(request.headers['authorization'],'Bearer TEST_PRIVATE_KEY')
            return httpx.Response(200,json={'object':'response.compaction','usage':USAGE})
        _,headers = await self.start(handler)
        response = await self.post(headers,'/responses/compact',model='TEST_VM_MODEL',usage={'input_tokens':999999999},input=[])
        self.assertEqual(response.status_code,200)
        row = (await self.settled())[0]
        self.assertEqual((row['input_tokens'],row['cost_usd']),(1000,Decimal('0.00132')))
        self.assertEqual(row['scope_key'],self.endpoint.scope.key)
        self.assertTrue(row['cost_complete'])
        self.assertNotIn('TEST_PRIVATE_KEY',str(row))
        self.gateway.model = lambda:('TEST_CHANGED_MODEL','high')
        await self.post(headers)
        row = next(row for row in await self.settled(2) if row['model']=='TEST_CHANGED_MODEL')
        self.assertIsNone(row['pricing_json'])
        self.assertFalse(row['cost_complete'])

    async def test_revoke_after_terminal_usage_keeps_receipt_and_closes_transport(self):
        entered,closed = asyncio.Event(),asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield event()
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):closed.set()
        token,headers = await self.start(lambda request:httpx.Response(200,stream=Stream(),headers={'content-type':'text/event-stream'}))
        task = asyncio.create_task(self.post(headers))
        try:
            await asyncio.wait_for(entered.wait(),5)
            self.gateway.revoke(token)
            await asyncio.wait_for(closed.wait(),5)
            self.assertIn(b'response.completed',(await task).content)
            self.assertTrue((await self.settled())[0]['cost_complete'])
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_revoke_waiting_headers_and_http_error_never_become_known_zero(self):
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.Event().wait()
        token,headers = await self.start(handler)
        task = asyncio.create_task(self.post(headers))
        try:
            await asyncio.wait_for(entered.wait(),5)
            self.gateway.revoke(token)
            self.assertEqual((await task).status_code,401)
            row = (await self.settled())[0]
            self.assertFalse(row['cost_complete'])
            self.assertIsNone(row['input_tokens'])
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(503,json={'error':'TEST_DOWN'})))
        token,_ = self.gateway.open()
        self.assertEqual((await self.post({'Authorization':'Bearer '+token})).status_code,503)
        self.assertTrue(all(not row['cost_complete'] for row in await self.settled(2)))
