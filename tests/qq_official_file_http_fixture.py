"""Loopback file protocol fixtures shared by integration tests."""
import asyncio
import base64
import hashlib
from aiohttp import web
from src.qq_official.testing.fake_platform import FakePlatform

class MediaFake(FakePlatform):
    """TEST-only additive fixture; never proxies a supplied URL."""
    def __init__(self):
        super().__init__()
        self.app_id = 'TEST_MEDIA_UPLOADER'
        self.media_calls = []
        self.uploaded = {}
        self.parts = {}
        self.finished = set()
        self.response_status = 200
        self.response_code = None
        self.implicit_id = None
        self.drop_response = False
        self.token_failures = 0
        self.token_seen = asyncio.Event()
        self.token_release = None
        self.prepare_response_override = None
        self.block = 8
        self.receipt_ttl = 60
        self.put_drop = False
        self.files_seen = asyncio.Event()
        self.files_release = None

    async def start(self):
        app = web.Application(client_max_size=6_000_000)
        app.router.add_post('/app/getAppAccessToken', self._token)
        app.router.add_get('/gateway/bot', self._gateway)
        app.router.add_post('/v2/{collection:groups|users}/{target}/messages', self._send)
        app.router.add_post('/v2/{collection:groups|users}/{target}/files', self.files)
        app.router.add_post('/v2/{collection:groups|users}/{target}/upload_prepare', self.prepare)
        app.router.add_post('/v2/{collection:groups|users}/{target}/upload_part_finish', self.finish)
        app.router.add_put('/put/{index}', self.put)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, '127.0.0.1', 0)
        await site.start()
        self.base_url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    async def _token(self, request):
        self.token_seen.set()
        if self.token_release is not None:
            await self.token_release.wait()
        if self.token_failures:
            self.token_failures -= 1
            self.token_calls += 1
            return web.json_response({'code': 999999}, status=503)
        return await super()._token(request)

    async def record(self, request):
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        payload = await request.json()
        self.media_calls.append((request.match_info['collection'], request.match_info['target'],
                                 request.path, payload, dict(request.headers)))
        return payload

    async def files(self, request):
        body = await self.record(request)
        if body.get('srv_send_msg') is not False:
            raise web.HTTPBadRequest()
        if 'file_data' in body:
            data = base64.b64decode(body['file_data'], validate=True)
        else:
            if body['upload_id'] != 'TEST_UPLOAD' or len(self.finished) != len(self.parts):
                raise web.HTTPBadRequest()
            data = b''.join(self.parts[i] for i in sorted(self.parts))
        key = (request.match_info['collection'], request.match_info['target'])
        self.uploaded[key] = data
        self.files_seen.set()
        if self.files_release is not None:
            await self.files_release.wait()
        if self.drop_response:
            request.transport.close()
            return web.Response()
        response = {'file_info': 'TEST_FILE_INFO', 'file_uuid': 'TEST_UUID', 'ttl': self.receipt_ttl}
        if self.response_code is not None:
            response['code'] = self.response_code
        if self.implicit_id is not None:
            response['id'] = self.implicit_id
        return web.json_response(response, status=self.response_status)

    async def prepare(self, request):
        body = await self.record(request)
        self.prepared_size = int(body['file_size'])
        count = (self.prepared_size + self.block - 1) // self.block
        response = {'upload_id': 'TEST_UPLOAD', 'block_size': str(self.block),
            'parts': [{'index': i, 'block_size': str(min(self.block, self.prepared_size - i*self.block)),
                       'presigned_url': f'https://uploads.example/put/{i}?signed=TEST_ONLY'} for i in range(count)],
            'upload_config': {'concurrency': 2, 'retry_timeout': 10, 'retry_delay': 1}}
        return web.json_response(self.prepare_response_override or response)

    async def put(self, request):
        if request.headers.get('Authorization') or request.headers.get('Cookie'):
            raise web.HTTPForbidden()
        index = int(request.match_info['index'])
        self.parts[index] = await request.read()
        if self.put_drop:
            request.transport.close()
            return web.Response()
        return web.Response(status=200)

    async def finish(self, request):
        body = await self.record(request)
        index = body['part_index']
        actual = self.parts[index]
        if (body['upload_id'] != 'TEST_UPLOAD' or body['block_size'] != str(len(actual))
                or body['md5'] != hashlib.md5(actual).hexdigest()):
            raise web.HTTPBadRequest()
        self.finished.add(index)
        return web.json_response({})

class FileMediaFake(MediaFake):
    """Type-7 fixture contract; retain M0's actual window/sequence/quota checks."""
    async def _send(self, request):
        body = await request.json()
        if body.get('msg_type') != 7:
            return await super()._send(request)
        if body.get('media') != {'file_info': 'TEST_FILE_INFO'}:
            from aiohttp import web
            return web.json_response({'code': 50041}, status=400)
        original = request
        class WindowRequest:
            def __getattr__(self, name):
                return getattr(original, name)
            async def json(self):
                return {**body, 'msg_type': 0, 'content': 'TEST media-window accounting'}
        count = len(self.received)
        response = await super()._send(WindowRequest())
        if len(self.received) > count:
            self.received[-1]['payload'] = body
        return response
