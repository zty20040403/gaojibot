"""The host relay swaps in its own model and key, and only for a live job token."""
import asyncio
import json
import logging
import socket
import unittest

import httpx

from src.plugins.ai_chat.codex_gateway import CodexGateway


def free_port():
    # The relay takes this port and the next one.
    while True:
        with socket.socket() as first:
            first.bind(('127.0.0.1', 0))
            port = first.getsockname()[1]
            with socket.socket() as second:
                try:
                    second.bind(('127.0.0.1', port + 1))
                except OSError:
                    continue
            return port


class CodexGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []

        def upstream(request):
            self.seen.append((str(request.url), request.headers['authorization'], json.loads(request.content)))
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={'content-type': 'text/event-stream'})

        self.gateway = CodexGateway(port=free_port(), upstream=lambda: ('http://upstream.test/v1', 'TEST_REAL_KEY'),
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
