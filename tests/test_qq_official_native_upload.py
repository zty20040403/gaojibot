"""Real loopback token/REST/PUT tests. No QQ connections, credentials or messages."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import unittest
from dataclasses import replace
from urllib.parse import urlsplit

from aiohttp import web
import httpx

from src.qq_official.credentials import Credentials
from src.qq_official.errors import QQAPIError
from src.qq_official.http import RestClient, request_json, request_json_response
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.native_media import fingerprint, NativeProtocolError
from src.plugins.ai_chat.adapters.qq_official.native_upload import (
    UploadIntent, UploadPermit, OfficialMediaUploader, UploadGuardError,
    issue_upload_permit, PutResponse,
)


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


class TestOnlyPartTransport:
    """Deliberately maps one TEST host to fixture loopback, never arbitrary URLs."""
    __test__ = False
    def __init__(self, fake):
        self.fake = fake
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self.calls = 0
    async def put(self, part, content, *, timeout, before_dispatch, ensure_current):
        parsed = urlsplit(part.presigned_url)
        if parsed.netloc != 'uploads.example' or parsed.query != 'signed=TEST_ONLY':
            raise UploadGuardError()
        if parsed.path != f'/put/{part.index}':
            raise UploadGuardError()
        await ensure_current()
        before_dispatch()
        self.calls += 1
        response = await self.client.put(self.fake.base_url + parsed.path,
            content=content, timeout=timeout, follow_redirects=False)
        return PutResponse(response.status_code)


class UploaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = MediaFake()
        await self.fake.start()
        self.tokens = TokenManager(Credentials(self.fake.app_id, self.fake.app_secret),
            self.fake.base_url, allow_test_server=True, max_attempts=1)
        self.rest = RestClient(self.tokens, allow_test_server=True)
        self.now = 1000
        self.content = b'TEST_PDF_ZIP_BYTES_1234567890'
        self.artifact = fingerprint([self.content], filename='fixture.pdf', file_type=4)
        self.account = AccountKey('qq-official', self.fake.app_id, 'test')
        self.endpoint = EndpointKey(self.account, 'group', 'TEST_TARGET')
        self.intent = UploadIntent('1'*32, self.endpoint, self.artifact, 1100)
        self.allowed = True
        self.checks = []
        self.stages = []
        self.permits = []
        self.replay_permit = None
        self.expire_after_begin = False
        self.invalidate_after_begin = False
        self.part = TestOnlyPartTransport(self.fake)
        self.uploader = self.make_uploader()

    def make_uploader(self, **kwargs):
        return OfficialMediaUploader(self.rest, ensure_current=self.check,
            before_network=self.begin, clock=lambda:self.now, **kwargs)

    async def check(self, intent, operation):
        self.checks.append(operation)
        if not self.allowed:
            raise PermissionError('TEST_PRIVATE_AUTHORITY_DETAIL')
        self.assertEqual(intent, self.intent)

    async def begin(self, stage):
        self.stages.append(stage)
        if self.expire_after_begin:
            self.now = 1100
        if self.invalidate_after_begin:
            self.tokens.invalidate()
        if self.replay_permit is not None:
            return self.replay_permit
        def sync_check():
            if not self.allowed:
                raise UploadGuardError()
        permit = issue_upload_permit(stage, stage_attempt_id=secrets.token_hex(16), check_current=sync_check)
        self.permits.append(permit)
        return permit

    async def asyncTearDown(self):
        await self.part.client.aclose()
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()

    async def test_small_group_and_c2c_actual_token_upload_no_message(self):
        for kind in ('group', 'private'):
            self.intent = replace(self.intent, endpoint=EndpointKey(self.account, kind, 'TEST_TARGET'))
            result = await self.uploader.upload_small(self.intent, self.content)
            self.assertEqual(result.disposition, 'confirmed')
            self.assertEqual(result.http_status, 200)
            self.assertEqual(result.value.endpoint, self.intent.endpoint)
            collection = 'groups' if kind == 'group' else 'users'
            self.assertEqual(self.fake.uploaded[(collection, 'TEST_TARGET')], self.content)
        self.assertEqual(self.fake.token_calls, 1)
        self.assertEqual(self.fake.received, [])
        self.assertEqual(len(self.stages), 2)
        self.assertNotIn('TEST_FILE_INFO', repr(result))
        self.assertNotIn('TEST_TARGET', repr(self.stages))
        self.assertNotIn('TEST_PRIVATE', repr(result))
        self.assertTrue(all(call[3]['srv_send_msg'] is False for call in self.fake.media_calls))
        self.assertTrue(all(call[4]['Authorization'] == 'QQBot TEST_ACCESS_TOKEN' for call in self.fake.media_calls))

    async def test_opaque_unicode_target_percent_once_route_roundtrip(self):
        self.intent = replace(self.intent, endpoint=EndpointKey(self.account, 'private', '群:测 %2F?#'))
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'confirmed')
        self.assertEqual(self.fake.media_calls[0][1], '群:测 %2F?#')

    async def test_unsafe_route_cannot_fetch_token(self):
        for native in ('a/b', '.', '..', r'a\b'):
            with self.subTest(native=native), self.assertRaises(NativeProtocolError):
                await self.uploader.upload_small(replace(self.intent,
                    endpoint=EndpointKey(self.account, 'group', native)), self.content)
        self.assertEqual(self.fake.token_calls, 0)

    async def test_unknown_auth_stops_after_one_physical_request(self):
        self.fake.token_failures = 1
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.token_calls, 1)
        self.assertEqual(self.stages, [])
        self.assertEqual(self.fake.media_calls, [])

    async def test_revoked_before_auth_causes_zero_token_or_upload(self):
        self.allowed = False
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.token_calls, 0)
        self.assertEqual(self.fake.media_calls, [])
        self.assertNotIn('PRIVATE', repr(result))

    async def test_each_physical_token_retry_rechecks_source(self):
        self.fake.token_failures = 3
        async def revoke_during_backoff(_delay):
            self.allowed = False
        self.tokens._max_attempts = 3
        self.tokens._sleep = revoke_during_backoff
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.token_calls, 1)
        self.assertEqual(self.fake.media_calls, [])

    async def test_token_response_after_revoke_never_starts_upload(self):
        self.fake.token_release = asyncio.Event()
        task = asyncio.create_task(self.uploader.upload_small(self.intent, self.content))
        await asyncio.wait_for(self.fake.token_seen.wait(), 2)
        self.allowed = False
        self.fake.token_release.set()
        result = await task
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.media_calls, [])

    async def test_plain_bool_or_constructed_permit_never_authorizes_upload(self):
        for forged in (True, UploadPermit()):
            self.replay_permit = forged
            result = await self.uploader.upload_small(self.intent, self.content)
            self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.media_calls, [])

    async def test_permit_replay_and_cross_request_binding_rejected(self):
        first = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(first.disposition, 'confirmed')
        self.replay_permit = self.permits[0]
        repeated = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(repeated.disposition, 'proven_unsent')
        self.assertEqual(len(self.fake.media_calls), 1)

    async def test_expiry_or_token_invalidation_after_persisted_intent_is_unsent(self):
        self.expire_after_begin = True
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.now = 1000
        self.expire_after_begin = False
        self.invalidate_after_begin = True
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(len(self.stages), 2)
        self.assertEqual(self.fake.media_calls, [])

    async def test_accept_then_response_loss_is_unknown_without_retry(self):
        self.fake.drop_response = True
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'unknown')
        self.assertEqual(len(self.fake.media_calls), 1)
        self.assertEqual(self.fake.received, [])

    async def test_final_401_invalidates_used_token_without_hidden_retry(self):
        self.fake.response_status = 401
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual((result.disposition, result.category, result.http_status),
                         ('confirmed_rejected', 'authentication', 401))
        self.assertEqual(self.tokens._invalidated_token, 'TEST_ACCESS_TOKEN')
        self.assertEqual(self.fake.token_calls, 1)
        self.assertEqual(len(self.fake.media_calls), 1)

    async def test_late_401_cannot_invalidate_a_newer_token(self):
        self.fake.response_status = 401
        self.fake.files_release = asyncio.Event()
        task = asyncio.create_task(self.uploader.upload_small(self.intent, self.content))
        await asyncio.wait_for(self.fake.files_seen.wait(), 2)
        self.fake.access_token = 'TEST_REFRESHED_TOKEN'
        self.tokens.invalidate()
        self.assertEqual(await self.tokens.get(), 'TEST_REFRESHED_TOKEN')
        self.fake.files_release.set()
        result = await task
        self.assertEqual(result.category, 'authentication')
        self.assertEqual(await self.tokens.get(), 'TEST_REFRESHED_TOKEN')
        self.assertEqual(self.fake.token_calls, 2)
        self.assertEqual(len(self.fake.media_calls), 1)

    async def test_cancel_after_request_preserves_durable_unknown_for_caller(self):
        self.fake.files_release = asyncio.Event()
        task = asyncio.create_task(self.uploader.upload_small(self.intent, self.content))
        await asyncio.wait_for(self.fake.files_seen.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.fake.files_release.set()
        self.assertEqual(len(self.stages), 1)
        self.assertEqual(len(self.fake.media_calls), 1)
        self.assertEqual(self.fake.uploaded[('groups','TEST_TARGET')], self.content)
        self.assertEqual(self.fake.received, [])
        self.replay_permit = self.permits[0]
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(len(self.fake.media_calls), 1)

    async def test_201_202_5xx_body_errors_are_not_confirmed(self):
        for status, code in ((201,None),(202,None),(500,None),(200,999999),(500,50015014)):
            self.fake.response_status, self.fake.response_code = status, code
            result = await self.uploader.upload_small(self.intent, self.content)
            self.assertEqual(result.disposition, 'unknown')
        self.assertEqual(len(self.fake.media_calls), 5)
        self.assertEqual(self.fake.token_calls, 1)

    async def test_explicit_native_file_rejection_is_observation_no_fallback(self):
        self.fake.response_code = 50015014
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'confirmed_rejected')
        self.assertEqual(result.category, 'native_file_unavailable')
        self.assertEqual(result.code, 50015014)
        self.assertEqual(len(self.fake.media_calls), 1)
        self.assertEqual(self.fake.received, [])

    async def test_implicit_message_receipt_alarm_prevents_second_send(self):
        self.fake.implicit_id = 'TEST_UNEXPECTED_MESSAGE'
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'unknown')
        self.assertTrue(result.message_evidence)
        self.assertEqual(self.fake.received, [])
        self.assertNotIn('TEST_UNEXPECTED', repr(result))

    async def test_error_code_cannot_erase_conflicting_upload_message_receipt(self):
        self.fake.implicit_id = 'TEST_PRIVATE_CONFLICTING_MESSAGE'
        self.fake.response_code = 50015014
        for status in (200, 400):
            self.fake.response_status = status
            result = await self.uploader.upload_small(self.intent, self.content)
            self.assertEqual(result.disposition, 'unknown')
            self.assertEqual(result.category, 'unexpected_upload_message_evidence')
            self.assertTrue(result.message_evidence)
            self.assertNotIn('TEST_PRIVATE', repr(result))
        self.assertEqual(len(self.fake.media_calls), 2)
        self.assertEqual(self.fake.received, [])

    async def test_same_app_wrong_environment_or_other_account_zero_io(self):
        for account in (replace(self.account, environment='production'), replace(self.account, app_id='TEST_OTHER')):
            result = await self.uploader.upload_small(replace(self.intent,
                endpoint=EndpointKey(account, 'group', 'TEST_TARGET')), self.content)
            self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.token_calls, 0)

    async def test_test_flag_cannot_label_real_api_as_test_environment(self):
        self.rest.base_url = 'https://api.bot.qq.com'
        result = await self.uploader.upload_small(self.intent, self.content)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.token_calls, 0)
        self.assertEqual(self.stages, [])

    async def test_small_artifact_mutation_fails_before_auth(self):
        with self.assertRaises(NativeProtocolError):
            await self.uploader.upload_small(self.intent, b'x'*len(self.content))
        self.assertEqual(self.fake.token_calls, 0)

    async def test_chunk_prepare_default_disabled_without_creating_server_session(self):
        result = await self.uploader.prepare(self.intent)
        self.assertEqual(result.disposition, 'proven_unsent')
        self.assertEqual(self.fake.token_calls, 0)

    async def test_chunk_actual_prepare_put_finish_merge_no_message(self):
        uploader = self.make_uploader(part_transport=self.part, allowed_part_hosts={'uploads.example'})
        response = await uploader.prepare(self.intent)
        self.assertEqual(response.disposition, 'confirmed')
        prepared = response.value
        for part in prepared.parts:
            data = self.content[part.offset:part.offset + part.length]
            sent = await uploader.put_part(self.intent, prepared, part.index, data)
            self.assertEqual(sent.disposition, 'confirmed')
            finished = await uploader.finish_part(self.intent, prepared, part.index, data)
            self.assertEqual(finished.disposition, 'confirmed')
        merged = await uploader.complete(self.intent, prepared)
        self.assertEqual(merged.disposition, 'confirmed')
        self.assertEqual(self.fake.uploaded[('groups','TEST_TARGET')], self.content)
        self.assertEqual(self.fake.received, [])
        self.assertEqual(self.part.calls, len(prepared.parts))
        self.assertEqual([s.operation for s in self.stages], ['upload_prepare'] +
            [x for _ in prepared.parts for x in ('upload_put','upload_part_finish')] + ['upload_complete'])

    async def test_chunk_put_response_loss_stays_unknown_no_finish_or_merge(self):
        uploader = self.make_uploader(part_transport=self.part, allowed_part_hosts={'uploads.example'})
        prepared = (await uploader.prepare(self.intent)).value
        self.fake.put_drop = True
        part = prepared.parts[0]
        result = await uploader.put_part(self.intent, prepared, 0, self.content[:part.length])
        self.assertEqual(result.disposition, 'unknown')
        self.assertEqual(self.part.calls, 1)
        self.assertEqual(self.fake.finished, set())
        self.assertEqual(self.fake.uploaded, {})

    async def test_chunk_host_policy_and_session_endpoint_are_not_model_overrideable(self):
        uploader = self.make_uploader(part_transport=self.part, allowed_part_hosts={'different.example'})
        result = await uploader.prepare(self.intent)
        self.assertEqual(result.disposition, 'unknown')
        self.assertEqual(self.part.calls, 0)
        uploader = self.make_uploader(part_transport=self.part, allowed_part_hosts={'uploads.example'})
        prepared = (await uploader.prepare(self.intent)).value
        wrong = replace(self.intent, endpoint=EndpointKey(self.account,'private','TEST_OTHER'))
        with self.assertRaises(NativeProtocolError):
            await uploader.complete(wrong, prepared)

    async def test_protocol_old_dict_api_and_internal_status_api(self):
        async with httpx.AsyncClient(trust_env=False) as client:
            url = self.fake.base_url + '/app/getAppAccessToken'
            payload = {'appId':self.fake.app_id, 'clientSecret':self.fake.app_secret}
            old = await request_json(client,'POST',url,json=payload)
            new = await request_json_response(client,'POST',url,json=payload)
            self.assertIsInstance(old,dict)
            self.assertEqual(new.body,old)
            self.assertEqual(new.status,200)
        self.assertIsInstance(await self.rest.gateway(),dict)

    async def test_protocol_prepared_upload_single_use_wrong_route_and_forced_false(self):
        path = '/v2/groups/TEST_TARGET/files'
        ticket = await self.rest.prepare_upload(path)
        payload = {'file_type':4,'file_name':'test.pdf','file_data':base64.b64encode(self.content).decode(),'srv_send_msg':False}
        result = await self.rest.upload(path,payload,prepared=ticket,before_dispatch=lambda:None)
        self.assertEqual(result.status,200)
        with self.assertRaises(QQAPIError):
            await self.rest.upload(path,payload,prepared=ticket,before_dispatch=lambda:None)
        wrong = await self.rest.prepare_upload(path)
        with self.assertRaises(QQAPIError):
            await self.rest.upload('/v2/users/TEST_TARGET/files',payload,prepared=wrong,before_dispatch=lambda:None)
        invalid = await self.rest.prepare_upload(path)
        with self.assertRaises(ValueError):
            await self.rest.upload(path,{**payload,'srv_send_msg':True},prepared=invalid,before_dispatch=lambda:None)
        self.assertEqual(len(self.fake.media_calls),1)


if __name__ == '__main__':
    unittest.main()
