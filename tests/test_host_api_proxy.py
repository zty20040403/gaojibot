"""Real loopback relay + PostgreSQL; DNS/upstream only are local doubles."""
import asyncio
from functools import partial
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock,patch

import httpx
import httpx2

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.codex_gateway import CodexGateway
from src.plugins.ai_chat.host_apis import HostAPI
from src.plugins.ai_chat.host_api_calls import HostAPICalls
from src.plugins.ai_chat.host_api_proxy import HostAPIProxy,api_instructions
from src.plugins.ai_chat.host_api_transport import PinnedAPITransport,forward
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests.postgres_fixture import isolated_postgres_for_test
from tests.test_codex_gateway import free_port
from tests.test_host_api_transport import Stream


class HostAPIProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        path = Path(directory.name)/'TEST_PRIVATE_KEY'
        path.write_text('TEST_HOST_SECRET'); path.chmod(0o600)
        api = HostAPI.parse('test',dict(base_url='https://api.example.test/v1',secret_file=str(path),
            headers={'Authorization':'Bearer {secret}'},allow=['POST /repos'],per_hour=1,about='TEST ABOUT'))
        self.store = HostAPICalls(isolated_postgres_for_test(self))
        self.db = DatabaseCalls(); self.addAsyncCleanup(self.db.close)
        self.finished = asyncio.Event()
        async def calls(fn,*args,**kwargs):
            result = await self.db(fn,*args,**kwargs)
            if fn.__name__=='finish':
                self.finished.set()
            return result
        self.seen = []
        self.stream = Stream(b'TEST upstream response')
        async def wire(request):
            self.seen.append(request)
            return httpx2.Response(200,stream=self.stream)
        self.wire = wire
        self.resolve = AsyncMock(return_value=('8.8.8.8',))
        def transport(url):
            return PinnedAPITransport(url,resolver=self.resolve,transport=httpx2.MockTransport(self.wire))
        self.proxy = HostAPIProxy({'test':api},self.store,calls=calls,transport=partial(forward,transport_factory=transport))
        self.gateway = CodexGateway(port=free_port(),upstream=lambda:None,model=lambda:('TEST_MODEL','high'),
            logger=logging.getLogger(__name__),api_handler=self.proxy.handle)
        self.addAsyncCleanup(self.gateway.close)
        await self.gateway.start()
        self.endpoint = EndpointKey(AccountKey('qq-official','TEST_APP','test'),'group','TEST_A')
        self.current = AsyncMock()
        self.token,self.channel = self.gateway.open(source=dict(job_id=1,endpoint_key=self.endpoint.key,scope_key='TEST_ALIAS'),current=self.current)
        self.url = f'http://127.0.0.1:{self.gateway.port}/api/test/repos/TEST'
        self.client = httpx.AsyncClient(trust_env=False)
        self.addAsyncCleanup(self.client.aclose)

    async def call(self, **changes):
        return await self.client.post(self.url,**(dict(headers={'Authorization':'Bearer '+self.token},content=b'TEST_BODY')|changes))

    async def final(self):
        await asyncio.wait_for(self.finished.wait(),3)
        return (await self.db(self.store.snapshot))['items']

    async def test_scoped_quota_authentication_and_no_query_or_body_in_audit(self):
        self.assertEqual((await self.call(headers={'Authorization':'Bearer TEST_WRONG'})).status_code,401)
        self.assertEqual((await self.call(params={'secret':'TEST_QUERY'})).status_code,200)
        rows = await self.final()
        self.assertEqual(rows[0]['scope_key'],self.endpoint.scope.key)
        self.assertEqual(rows[0]['path'],'/repos/TEST')
        self.assertEqual(rows[0]['outcome'],'completed')
        self.assertNotIn('TEST_QUERY',str(rows)); self.assertNotIn('TEST_BODY',str(rows))
        self.assertNotIn('TEST_HOST_SECRET',str(rows))
        self.assertEqual((await self.call()).status_code,429)
        self.assertEqual(len(self.seen),1)
        other = EndpointKey(self.endpoint.account,'group','TEST_B')
        token,_ = self.gateway.open(source=dict(job_id=2,endpoint_key=other.key,scope_key='TEST_ALIAS'),current=self.current)
        self.finished.clear(); self.stream = Stream(b'TEST second')
        self.assertEqual((await self.call(headers={'Authorization':'Bearer '+token})).status_code,200)
        await self.final()

    async def test_path_method_and_missing_identity_fail_closed(self):
        unbound,_ = self.gateway.open()
        self.assertEqual((await self.call(headers={'Authorization':'Bearer '+unbound})).status_code,403)
        self.url = self.url.replace('/repos/TEST','/repos/%252e%252e/TEST')
        self.assertEqual((await self.call()).status_code,400)
        self.assertEqual(self.seen,[])
        self.assertEqual((await self.db(self.store.snapshot))['items'][0]['path'],'<invalid>')

    async def test_permission_change_during_dns_is_checked_before_connect(self):
        async def resolve(host,port):
            self.current.side_effect = PermissionError('TEST revoked')
            return ('8.8.8.8',)
        self.resolve.side_effect = resolve
        self.assertEqual((await self.call()).status_code,403)
        self.assertEqual(self.seen,[])
        self.assertEqual((await self.final())[0]['outcome'],'failed')

    async def test_request_size_is_bounded_before_forwarding(self):
        with patch('src.plugins.ai_chat.host_api_proxy.MAX_BODY',4):
            self.assertEqual((await self.call()).status_code,413)
        self.assertEqual(self.seen,[])
        self.assertEqual((await self.final())[0]['status_code'],413)

    async def test_revocation_cancels_waiting_headers_and_keeps_reservation(self):
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def wire(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.wire = wire
        task = asyncio.create_task(self.call())
        try:
            await asyncio.wait_for(entered.wait(),3)
            self.gateway.revoke(self.token)
            self.assertEqual((await task).status_code,401)
            await asyncio.wait_for(cancelled.wait(),3)
            self.assertEqual((await self.final())[0]['outcome'],'interrupted')
            with self.store.transaction() as cursor:
                self.assertEqual(cursor.execute('SELECT used FROM host_api_quota').fetchone()['used'],1)
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_revoked_binary_stream_is_not_reported_as_successful_eof(self):
        entered,closed = asyncio.Event(),asyncio.Event()
        class HangingStream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b'TEST partial binary '*100
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        self.stream = HangingStream()
        task = asyncio.create_task(self.call())
        try:
            await asyncio.wait_for(entered.wait(),3)
            self.gateway.revoke(self.token)
            with self.assertRaises(httpx.RemoteProtocolError):
                await task
            await asyncio.wait_for(closed.wait(),3)
            row = (await self.final())[0]
            self.assertEqual((row['status_code'],row['outcome']),(200,'interrupted'))
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_prompt_has_metadata_only_never_secret_path_or_injected_headers(self):
        prompt = api_instructions(self.proxy.apis,self.gateway.port)
        for text in ('TEST ABOUT','GAOJI_JOB_TOKEN','/api/test'):
            self.assertIn(text,prompt)
        for text in ('TEST_PRIVATE_KEY','TEST_HOST_SECRET','{secret}','api.example.test'):
            self.assertNotIn(text,prompt)
        self.assertEqual(api_instructions({},self.gateway.port),'')
