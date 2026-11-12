"""Fixed-origin, pinned, credential-injecting transport; no external network."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx2

from src.plugins.ai_chat.host_apis import HostAPI,ApiDenied
from src.plugins.ai_chat.host_api_transport import PinnedAPITransport,forward


class Stream(httpx2.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks,self.closed = chunks,False
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
    async def aclose(self):
        self.closed = True


class HostAPITransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        path = Path(directory.name)/'TEST_KEY'
        path.write_text('TEST_HOST_SECRET'); path.chmod(0o600)
        self.api = HostAPI.parse('test',dict(base_url='https://api.example.test/v1',secret_file=str(path),
            headers={'Authorization':'Bearer {secret}'},query={'key':'{secret}'},allow=['POST /repos']))
        self.seen,self.dns = [],[]
        self.addresses = ('8.8.8.8',)
        self.stream = Stream(b'TEST binary\x00',b'\xff answer')
        self.status,self.headers = 200,{'content-type':'application/octet-stream'}

    async def resolve(self, host, port):
        self.dns.append((host,port))
        return self.addresses

    async def wire(self, request):
        self.seen.append(request)
        return httpx2.Response(self.status,headers=self.headers,stream=self.stream)

    def transport(self, url):
        return PinnedAPITransport(url,resolver=self.resolve,transport=httpx2.MockTransport(self.wire))

    def call(self, **kwargs):
        return forward(self.api,**(dict(method='POST',raw_path=b'/repos/TEST.git/git-receive-pack',
            query=b'x=1&key=TEST_VM_KEY&KEY=TEST_OTHER',body=b'TEST pack',transport_factory=self.transport,
            headers={'Authorization':'Bearer TEST_VM_TOKEN','Cookie':'TEST_COOKIE','Connection':'X-Remove',
                'X-Remove':'TEST_HOP','Accept-Encoding':'gzip'}) | kwargs))

    async def test_request_is_pinned_and_only_host_credentials_reach_upstream(self):
        async with self.call() as response:
            body = b''.join([chunk async for chunk in response.chunks()])
            self.assertEqual(body,b'TEST binary\x00\xff answer')
        request = self.seen[0]
        self.assertEqual(request.url.host,'8.8.8.8')
        self.assertEqual(request.url.path,'/v1/repos/TEST.git/git-receive-pack')
        self.assertEqual(request.headers['host'],'api.example.test')
        self.assertEqual(request.extensions['sni_hostname'],'api.example.test')
        self.assertEqual(request.headers['authorization'],'Bearer TEST_HOST_SECRET')
        self.assertEqual(request.headers['accept-encoding'],'identity')
        self.assertEqual(request.url.params.get_list('key'),['TEST_HOST_SECRET'])
        self.assertNotIn('KEY',request.url.params)
        self.assertEqual(request.url.params['x'],'1')
        self.assertNotIn('cookie',request.headers)
        self.assertNotIn('x-remove',request.headers)
        self.assertEqual(request.content,b'TEST pack')
        self.assertEqual(self.dns,[('api.example.test',443)])
        self.assertTrue(self.stream.closed)

    async def test_any_private_or_transition_address_denies_before_connect(self):
        for addresses in (('127.0.0.1',),('8.8.8.8','10.0.0.1'),('100.64.0.1',),('::ffff:192.168.1.1',),
                ('2002:0808:0808::1',),('64:ff9b::7f00:1',),()):
            self.addresses = addresses
            with self.subTest(addresses=addresses),self.assertRaises(ApiDenied):
                async with self.call():
                    self.fail('private target admitted')
        self.assertEqual(self.seen,[])

    async def test_redirect_is_returned_not_followed_and_response_cookies_stay_out(self):
        self.status = 302
        self.headers = {'location':'https://other.example.test/TEST','set-cookie':'TEST_SESSION',
            'connection':'X-Remove','X-Remove':'TEST_HOP'}
        async with self.call() as response:
            self.assertEqual(response.status_code,302)
            self.assertEqual(dict(response.headers),{'location':'https://other.example.test/TEST'})
            _ = [chunk async for chunk in response.chunks()]
        self.assertEqual(len(self.seen),1)

    async def test_path_method_and_body_are_checked_before_connection(self):
        for options in ({'raw_path':b'/repos/%252e%252e/TEST'},{'method':'DELETE'},{'raw_path':b'/repos-extra'}):
            with self.assertRaises(ApiDenied):
                async with self.call(**options):
                    self.fail('operation admitted')
        with patch('src.plugins.ai_chat.host_api_transport.MAX_BODY',4),self.assertRaises(ApiDenied):
            async with self.call():
                self.fail('oversize body admitted')
        self.assertEqual(self.seen,[])

    async def test_network_errors_do_not_expose_query_url_or_secret(self):
        async def broken(request):
            raise httpx2.ConnectError('https://api.example.test?key=TEST_HOST_SECRET')
        self.wire = broken
        with self.assertRaisesRegex(ApiDenied,'^api_upstream_unavailable$'):
            async with self.call():
                self.fail('broken connection admitted')

    async def test_common_reflection_encodings_cannot_bypass_secret_guard(self):
        Path(self.api.secret_file).write_text('TEST/a+b=')
        for chunks in ((b'TEST%2fa',b'%2Bb%3d'),(b'TEST\\/',b'a+b=')):
            self.stream = Stream(*chunks)
            with self.assertRaisesRegex(ApiDenied,'api_response_contains_credential'):
                async with self.call() as response:
                    _ = [chunk async for chunk in response.chunks()]
        self.headers = {'content-encoding':'gzip'}
        with self.assertRaisesRegex(ApiDenied,'api_response_encoding_denied'):
            async with self.call():
                self.fail('opaque compression admitted')

    async def test_reflected_secret_is_blocked_in_headers_or_across_stream_chunks(self):
        self.headers = {'location':'https://api.example.test?key=TEST_HOST_SECRET'}
        with self.assertRaisesRegex(ApiDenied,'api_response_contains_credential'):
            async with self.call():
                self.fail('reflected header returned')
        self.headers = {}
        self.stream = Stream(b'TEST padding '*10+b'TEST_HOST_',b'SECRET')
        delivered = b''
        with self.assertRaisesRegex(ApiDenied,'api_response_contains_credential'):
            async with self.call() as response:
                async for chunk in response.chunks():
                    delivered += chunk
        self.assertNotIn(b'TEST_HOST_SECRET',delivered)
        self.assertTrue(self.stream.closed)
