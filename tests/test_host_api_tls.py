"""Actual local TLS proves SNI/certificate checks survive numeric-IP pinning."""
import asyncio
from functools import partial
from pathlib import Path
import shutil
import ssl
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

import httpx2

from src.plugins.ai_chat.host_apis import HostAPI,ApiDenied
from src.plugins.ai_chat.host_api_transport import PinnedAPITransport,forward


@unittest.skipUnless(shutil.which('openssl'),'openssl is required for a local test certificate')
class HostAPITLSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config,key,cert = root/'openssl.cnf',root/'key.pem',root/'cert.pem'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
            '[dn]\nCN=api.example.test\n[ext]\nsubjectAltName=DNS:api.example.test\n')
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),
            '-out',str(cert),'-days','1','-config',str(config)],check=True,capture_output=True)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert,key)
        self.sni,self.requests,self.connects = [],[],[]
        self.connections = set()
        server_context.set_servername_callback(lambda connection,name,ctx:self.sni.append(name))
        async def handle(reader,writer):
            task = asyncio.current_task(); self.connections.add(task)
            try:
                self.requests.append(await reader.readuntil(b'\r\n\r\n'))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nTEST_OK')
                await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                self.connections.discard(task)
        self.server = await asyncio.start_server(handle,'127.0.0.1',0,ssl=server_context)
        async def close():
            self.server.close(); await self.server.wait_closed()
            await asyncio.gather(*self.connections,return_exceptions=True)
        self.addAsyncCleanup(close)
        self.port = self.server.sockets[0].getsockname()[1]
        self.context = ssl.create_default_context(cafile=str(cert))
        self.secret = root/'TEST_SECRET'; self.secret.write_text('TEST_HOST_KEY'); self.secret.chmod(0o600)
        self.resolve = AsyncMock(return_value=('8.8.8.8',))

    def transport(self, url):
        wire = httpx2.AsyncHTTPTransport(verify=self.context,trust_env=False,retries=0)
        backend = wire._pool._network_backend
        test = self
        class LocalConnector:
            async def connect_tcp(self, host, port, **kwargs):
                test.connects.append(host)
                test.assertEqual(host,'8.8.8.8')
                # Only this test connector remaps the already validated address;
                # production uses the ordinary numeric connector without a bypass.
                return await backend.connect_tcp('127.0.0.1',port,**kwargs)
        wire._pool._network_backend = LocalConnector()
        return PinnedAPITransport(url,resolver=self.resolve,transport=wire)

    def request(self, host):
        api = HostAPI.parse('test',dict(base_url=f'https://{host}:{self.port}/v1',
            secret_file=str(self.secret),headers={'Authorization':'Bearer {secret}'}))
        return partial(forward,api,method='GET',raw_path=b'/TEST',query=b'',headers={},body=b'',transport_factory=self.transport)

    async def test_real_tls_uses_original_hostname_while_connecting_validated_ip(self):
        async with self.request('api.example.test')() as response:
            self.assertEqual(b''.join([chunk async for chunk in response.chunks()]),b'TEST_OK')
        self.assertEqual(self.sni,['api.example.test'])
        self.assertEqual(self.connects,['8.8.8.8'])
        self.resolve.assert_awaited_once_with('api.example.test',self.port)
        self.assertIn(f'Host: api.example.test:{self.port}'.encode(),self.requests[0])
        self.assertIn(b'Bearer TEST_HOST_KEY',self.requests[0])

    async def test_wrong_certificate_hostname_is_rejected_before_credentials_are_sent(self):
        with self.assertRaisesRegex(ApiDenied,'^api_upstream_unavailable$'):
            async with self.request('wrong.example.test')():
                self.fail('mismatched certificate admitted')
        self.assertEqual(self.connects,['8.8.8.8'])
        self.assertEqual(self.requests,[])
