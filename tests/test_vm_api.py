"""Real HTTP and quota/audit DB; VM boot authority and upstream are doubles."""
import asyncio
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
import httpx2

from src.plugins.ai_chat.adapters.qq_official.vm_api import VMAPIAuthority
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSource
from tests import test_host_api_proxy as fixture


class VMAPITests(unittest.IsolatedAsyncioTestCase):
    call,final = fixture.HostAPIProxyTests.call,fixture.HostAPIProxyTests.final

    async def asyncSetUp(self):
        await fixture.HostAPIProxyTests.asyncSetUp(self)
        self.job_token,self.token = self.token,'gvm_'+'T'*43
        self.source = VMSource(self.endpoint,'s123456','00000000-0000-4000-8000-000000000001',1,1,1,1,
            hashlib.sha256(self.token.encode()).hexdigest())
        async def authenticate(token):
            if token!=self.token:
                raise PermissionError('TEST revoked')
            return self.source
        self.credentials = SimpleNamespace(authenticate=AsyncMock(side_effect=authenticate),check=AsyncMock())
        self.authority = self.gateway.vm_authority = VMAPIAuthority(self.credentials)
        self.addAsyncCleanup(self.authority.close)

    async def test_vm_api_uses_only_authenticated_physical_scope_and_caller(self):
        response = await self.call(headers={'Authorization':'Bearer '+self.token,'X-Scope':'TEST_OTHER'},
            content=b'{"scope_key":"TEST_OTHER","job_id":999}')
        self.assertEqual(response.status_code,200,response.text)
        row = (await self.final())[0]
        self.assertEqual(row['scope_key'],self.endpoint.scope.key)
        self.assertEqual(row['caller_ref'],'vm:s123456:1')
        self.assertNotIn(self.token,repr(self.authority.scopes))
        self.assertEqual(self.seen[0].headers['Authorization'],'Bearer TEST_HOST_SECRET')
        self.assertEqual((await self.call()).status_code,429)
        scope = next(iter(self.authority.scopes.values()))
        self.assertFalse(hasattr(scope,'connected'))
        self.assertNotIn('actor_key',scope.source)

    async def test_revalidated_source_replaces_closed_scope_not_raw_token_cache(self):
        first = await self.authority.authorize(self.token)
        first.close()
        second = await self.authority.authorize(self.token)
        self.assertIsNot(first,second)
        self.assertFalse(second.revoked)
        self.assertEqual(len(self.authority.scopes),1)
        self.assertNotIn(self.token,repr(self.authority.scopes))

    async def test_vm_token_cannot_access_model_routes_or_codex_tunnel(self):
        base = f'http://127.0.0.1:{self.gateway.port}'
        headers = {'Authorization':'Bearer '+self.token}
        for path in ('/v1/responses','/v1/responses/compact'):
            response = await self.client.post(base+path,json={},headers=headers)
            self.assertEqual(response.status_code,401)
        self.assertEqual((await self.client.get(base+'/v1/models',headers=headers)).status_code,401)
        reader,writer = await asyncio.open_connection('127.0.0.1',self.gateway.port+1)
        try:
            writer.write(self.token.encode()+b'\n'); await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        finally:
            writer.close(); await writer.wait_closed()
        self.credentials.authenticate.assert_not_awaited()
        self.assertEqual(self.seen,[])

    async def test_missing_invalid_and_revoked_job_tokens_never_fall_back_to_vm(self):
        self.gateway.revoke(self.job_token)
        for header in ('',self.token,'Bearer '+self.job_token):
            self.assertEqual((await self.call(headers={'Authorization':header})).status_code,401)
        self.credentials.authenticate.assert_not_awaited()
        self.assertEqual((await self.call(headers={'Authorization':'Bearer gvm_'+'X'*43})).status_code,401)
        self.assertEqual(self.seen,[])

    async def test_vm_boot_failure_is_not_leaked_or_used_as_a_job_identity(self):
        self.credentials.authenticate.side_effect = RuntimeError('TEST_PRIVATE_ERROR')
        response = await self.call()
        self.assertEqual(response.status_code,503)
        self.assertNotIn('TEST_PRIVATE',response.text)
        self.assertEqual(self.seen,[])

    async def test_persistent_revocation_interrupts_upstream_waiting_for_headers(self):
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
            self.credentials.check.side_effect = PermissionError('TEST permission changed')
            self.assertEqual((await asyncio.wait_for(task,3)).status_code,401)
            await asyncio.wait_for(cancelled.wait(),3)
            self.assertEqual((await self.final())[0]['outcome'],'interrupted')
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_vm_stream_revocation_keeps_binary_transport_failure_and_audit(self):
        entered,closed = asyncio.Event(),asyncio.Event()
        class Stream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b'TEST partial binary '*100
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        self.stream = Stream()
        task = asyncio.create_task(self.call())
        try:
            await asyncio.wait_for(entered.wait(),3)
            self.authority.revoke('s123456')
            with self.assertRaises(httpx.RemoteProtocolError):
                await asyncio.wait_for(task,3)
            await asyncio.wait_for(closed.wait(),3)
            row = (await self.final())[0]
            self.assertEqual((row['status_code'],row['outcome']),(200,'interrupted'))
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_shutdown_also_cancels_authentication_waiting_for_guest(self):
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def authenticate(token):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.credentials.authenticate.side_effect = authenticate
        task = asyncio.create_task(self.call())
        try:
            await asyncio.wait_for(entered.wait(),3)
            await asyncio.wait_for(self.authority.close(),3)
            await asyncio.wait_for(cancelled.wait(),3)
            self.assertEqual((await task).status_code,503)
            self.assertEqual(self.seen,[])
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
