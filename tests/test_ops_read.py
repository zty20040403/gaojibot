"""Host-only read transport; every network connection is a local httpx double."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import unittest

import httpx
from src.plugins.ai_chat import ops_read as ops
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey


class Body(httpx.AsyncByteStream):
    def __init__(self, value):
        self.value,self.closed = value,False
    async def __aiter__(self):
        yield self.value
    async def aclose(self):
        self.closed = True


class OpsReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.token = Path(temporary.name)/'token'; self.token.write_text('TEST_PRIVATE_TOKEN'); self.token.chmod(0o600)
        self.account = AccountKey('qq-official','TEST_APP','test')
        self.inbound = SimpleNamespace(endpoint=EndpointKey(self.account,'private','TEST_OWNER'),
            actor=ActorKey(self.account,'c2c_user','','TEST_OWNER'))
        self.current = AsyncMock()
        async def calls(fn,*args):
            return fn(*args)
        self.app = SimpleNamespace(db_call=calls,official_runtime=object(),settings=SimpleNamespace(
            ops_read_base_url='https://cluster.example.test',ops_read_token_file=str(self.token)))
        self.owner_allowed = ops.owner_allowed
        authority = patch.object(ops,'owner_allowed',return_value=True)
        self.authority = authority.start(); self.addCleanup(authority.stop)
        self.seen = []
        self.body = Body(json.dumps({'cpu':10}).encode())
        self.server = lambda request:httpx.Response(200,stream=self.body)
        client = httpx.AsyncClient
        async def server(request):
            self.seen.append(request)
            result = self.server(request)
            return await result if asyncio.iscoroutine(result) else result
        network = patch.object(ops.httpx,'AsyncClient',side_effect=lambda **kw:client(transport=httpx.MockTransport(server),**kw))
        network.start(); self.addCleanup(network.stop)

    async def read(self,args=None):
        return await ops.read(self.app,self.inbound,args or {'action':'resources','host':'TEST_HOST'},current=self.current)

    async def test_get_only_bounds_log_ranges_and_redacts_transport_secrets(self):
        self.body = Body(json.dumps({'text':'TEST_PRIVATE_TOKEN https://cluster.example.test'}).encode())
        result = await self.read({'action':'logs','host':'TEST_HOST','unit':'TEST_UNIT.service','lines':200,'since_seconds':60})
        self.assertNotIn('TEST_PRIVATE_TOKEN',result); self.assertNotIn('cluster.example.test',result)
        self.assertEqual(self.seen[0].method,'GET')
        self.assertEqual(self.seen[0].url.path,'/v1/hosts/TEST_HOST/units/TEST_UNIT.service/logs')
        self.assertEqual(dict(self.seen[0].url.params),{'lines':'200','since_seconds':'60'})
        self.assertEqual(self.seen[0].headers['authorization'],'Bearer TEST_PRIVATE_TOKEN')
        self.assertTrue(self.body.closed); self.assertGreaterEqual(self.current.await_count,3)

    async def test_invalid_arguments_never_reach_transport(self):
        for args in ({'action':'restart'},{'action':'overview','method':'POST'},
                {'action':'overview','host':'..'},{'action':'service','host':'TEST_HOST','unit':'../TEST.service'},
                {'action':'logs','host':'TEST_HOST','unit':'TEST.service','lines':True},
                {'action':'logs','host':'TEST_HOST','unit':'TEST.service','since_seconds':86401},
                {'action':'logs','host':'TEST_HOST','unit':'TEST.service','lines':201},
                {'action':'resources','host':'https://TEST_INVALID'},{'action':'service','host':'TEST_HOST'}):
            with self.subTest(args=args),self.assertRaises(ValueError):
                await self.read(args)
        self.assertEqual(self.seen,[])

    async def test_initial_revocation_or_job_cancellation_never_sends(self):
        self.authority.return_value = False
        with self.assertRaises(PermissionError):
            await self.read()
        self.authority.return_value = True
        self.current.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.read()
        self.assertEqual(self.seen,[])

    async def test_revocation_interrupts_waiting_response_headers(self):
        entered,ended = asyncio.Event(),asyncio.Event()
        async def server(request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()
        self.server = server
        pending = asyncio.create_task(self.read())
        await asyncio.wait_for(entered.wait(),1)
        self.authority.return_value = False
        with self.assertRaises(PermissionError):
            await asyncio.wait_for(pending,2)
        self.assertTrue(ended.is_set())

    async def test_job_cancellation_closes_inflight_body_and_discards_result(self):
        entered = asyncio.Event()
        class WaitingBody(Body):
            async def __aiter__(self):
                entered.set()
                yield b'{"value":'
                await asyncio.Future()
        self.body = WaitingBody(b'')
        pending = asyncio.create_task(self.read())
        await asyncio.wait_for(entered.wait(),1)
        self.current.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(pending,2)
        self.assertTrue(self.body.closed)

    async def test_total_deadline_closes_body_and_does_not_retry(self):
        class WaitingBody(Body):
            async def __aiter__(self):
                yield b'{'
                await asyncio.Future()
        self.body = WaitingBody(b'')
        with patch.object(ops,'DEADLINE',0.02),self.assertRaises(ops.OpsReadError):
            await asyncio.wait_for(self.read(),1)
        self.assertTrue(self.body.closed); self.assertEqual(len(self.seen),1)

    async def test_runtime_change_during_final_permission_lookup_discards_response(self):
        original = self.app.db_call
        async def calls(fn,*args):
            result = await original(fn,*args)
            if self.body.closed:
                self.app.official_runtime = object()
            return result
        self.app.db_call = calls
        with self.assertRaises(PermissionError):
            await self.read()
        self.assertTrue(self.body.closed)

    async def test_authority_and_runtime_checked_after_response(self):
        def server(request):
            self.app.official_runtime = object()
            return httpx.Response(200,stream=self.body)
        self.server = server
        with self.assertRaises(PermissionError):
            await self.read()
        self.assertTrue(self.body.closed)

    async def test_redirect_status_encoding_and_oversize_fail_without_leaking_errors(self):
        for status,headers,data in ((302,{'location':'https://TEST_OTHER/'},b'TEST_PRIVATE_TOKEN'),
                (500,{},b'TEST_PRIVATE_TOKEN'),(200,{'content-encoding':'gzip'},b'TEST_PRIVATE_TOKEN'),
                (200,{},b'x'*(ops.MAX_RESPONSE+1)),(200,{},b'not JSON TEST_PRIVATE_TOKEN')):
            self.body = Body(data)
            self.server = lambda request:httpx.Response(status,headers=headers,stream=self.body)
            with self.assertRaises(ops.OpsReadError) as error:
                await self.read()
            self.assertNotIn('TEST_PRIVATE_TOKEN',str(error.exception))
            self.assertNotIn('cluster.example.test',str(error.exception))
            self.assertTrue(self.body.closed)
        self.assertEqual(len(self.seen),5)

    async def test_insecure_endpoint_and_public_secret_file_are_rejected(self):
        for base in ('http://TEST_HOST','https://user:TEST_PRIVATE_TOKEN@host.test','https://host.test/?token=TEST_PRIVATE_TOKEN'):
            self.app.settings.ops_read_base_url = base
            with self.assertRaises(ops.OpsReadError):
                await self.read()
        self.app.settings.ops_read_base_url = 'https://cluster.example.test'
        self.token.chmod(0o644)
        with self.assertRaises(ops.OpsReadError):
            await self.read()
        self.assertEqual(self.seen,[])

    def test_group_denied_without_consulting_host_configuration(self):
        group = SimpleNamespace(endpoint=EndpointKey(self.account,'group','TEST_GROUP'))
        self.assertFalse(self.owner_allowed(SimpleNamespace(),group))
