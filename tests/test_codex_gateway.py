"""The host relay swaps in its own model and key, and only for a live job token."""
import asyncio
import json
import logging
import secrets
import socket
import unittest

import httpx

from src.plugins.ai_chat.codex_gateway import CodexGateway
from tests.test_llm_gateway import profile


def free_port():
    # Avoid the OS's ordinary ephemeral client range: a just-probed adjacent
    # port there can be consumed by our own HTTP requests before relay startup.
    # Production keeps its configured fixed ports; only loopback fixtures probe.
    for _ in range(200):
        port = 20000+2*secrets.randbelow(5000)
        with socket.socket() as first, socket.socket() as second:
            try:
                first.bind(('127.0.0.1',port))
                second.bind(('127.0.0.1',port+1))
            except OSError:
                continue
            return port
    raise RuntimeError('TEST relay port pair unavailable')


class CodexGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []

        def upstream(request):
            self.seen.append((str(request.url), request.headers['authorization'], json.loads(request.content)))
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={'content-type': 'text/event-stream'})

        self.gateway = CodexGateway(port=free_port(), upstream=lambda: profile('relay',base_url='http://upstream.test/v1',api_key='TEST_REAL_KEY'),
            model=lambda: ('TEST_HOST_MODEL', 'high'), logger=logging.getLogger(__name__),
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
        await self.gateway.start()
        self.addAsyncCleanup(self.gateway.close)
        self.base = f'http://127.0.0.1:{self.gateway.port}/v1'

    async def test_relay_needs_a_live_token_and_uses_host_model_and_key(self):
        token, _ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            denied = await client.post(self.base + '/responses', json={'model': 'x'},
                                       headers={'Authorization': 'Bearer TEST_WRONG'})
            self.assertEqual(denied.status_code, 401)
            reply = await client.post(self.base + '/responses', json={'model': 'TEST_VM_CHOICE', 'input': [],
                                      'reasoning': {'summary': 'auto'}}, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(reply.status_code, 200)
            self.assertIn(b'response.completed', reply.content)
            url, auth, body = self.seen[0]
            self.assertEqual(url, 'http://upstream.test/v1/responses')
            self.assertEqual(auth, 'Bearer TEST_REAL_KEY')
            self.assertEqual(body['model'], 'TEST_HOST_MODEL')
            self.assertEqual(body['reasoning'], {'summary': 'auto', 'effort': 'high'})
            self.gateway.revoke(token)
            revoked = await client.post(self.base + '/responses', json={}, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(revoked.status_code, 401)

    async def test_revoke_cancels_request_waiting_for_upstream_headers(self):
        entered,cancelled,release = asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def upstream(request):
            entered.set()
            try:
                await release.wait()
                return httpx.Response(200,content=b'TEST')
            finally:
                cancelled.set()
        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        token,_ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            task = asyncio.create_task(client.post(self.base+'/responses',json={},headers={'Authorization':'Bearer '+token}))
            try:
                await asyncio.wait_for(entered.wait(),2)
                self.gateway.revoke(token)
                await asyncio.wait_for(cancelled.wait(),2)
                self.assertEqual((await task).status_code,401)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_revoke_closes_upstream_stream_without_completion(self):
        entered,closed = asyncio.Event(),asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: TEST_PARTIAL\n\n'
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200,stream=Stream(),headers={'content-type':'text/event-stream'})))
        token,_ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            task = asyncio.create_task(client.post(self.base+'/responses',json={},headers={'Authorization':'Bearer '+token}))
            try:
                await asyncio.wait_for(entered.wait(),2)
                self.gateway.revoke(token)
                await asyncio.wait_for(closed.wait(),2)
                self.assertNotIn(b'response.completed',(await task).content)
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_missing_upstream_is_reported_for_the_job(self):
        token, _ = self.gateway.open()
        def broken():
            raise RuntimeError('TEST no cliproxy profile')
        self.gateway.upstream = broken
        async with httpx.AsyncClient(trust_env=False) as client:
            reply = await client.post(self.base + '/responses', json={}, headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(reply.status_code, 503)
        self.assertIn('TEST no cliproxy profile', self.gateway.failure(token))
        self.gateway.revoke(token)
        self.assertEqual(self.gateway.failure(token), '')

    async def test_revoke_closes_tunnel_even_before_engine_waits_for_it(self):
        token,channel = self.gateway.open()
        reader,writer = await asyncio.open_connection('127.0.0.1',self.gateway.port+1)
        try:
            writer.write(token.encode()+b'\n')
            await writer.drain()
            await asyncio.wait_for(asyncio.shield(channel.connected),2)
            self.assertIsNone(channel.writer)
            self.gateway.revoke(token)
            self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_tunnel_accepts_only_its_token_once(self):
        token, channel = self.gateway.open()
        _, stranger = await asyncio.open_connection('127.0.0.1', self.gateway.port + 1)
        stranger.write(b'TEST_WRONG\n')
        await stranger.drain()
        reader, writer = await asyncio.open_connection('127.0.0.1', self.gateway.port + 1)
        writer.write(token.encode() + b'\n')
        await writer.drain()
        await channel.wait(5)
        await channel.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        self.assertEqual(json.loads(await reader.readline())['method'], 'initialize')
        writer.write(b'{"id": 1, "result": {}}\n')
        await writer.drain()
        self.assertEqual(await channel.receive(), {'id': 1, 'result': {}})
        self.assertFalse(channel.connected.cancelled())
        writer.close()
        stranger.close()


if __name__ == '__main__':
    unittest.main()
