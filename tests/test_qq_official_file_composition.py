"""Actual gateway/shared model loop/file worker/M3 receipts with loopback I/O."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web

from src.plugins.ai_chat.adapters.qq_official.file_services import (
    FileServiceConfig, NativeRouteEvidence, file_services_factory,
)
from src.plugins.ai_chat.application.owned_sandbox import GenerationLimits
from tests import test_qq_official_runtime as runtime_fixture
from tests import test_qq_official_turn_endtoend as turn_fixture
from tests.test_qq_official_file_dispatcher import FileMediaFake
from tests.test_owned_sandbox import FakeManager


class GatewayMediaFake(FileMediaFake):
    async def start(self):
        app = web.Application(client_max_size=6_000_000)
        app.router.add_post('/app/getAppAccessToken', self._token)
        app.router.add_get('/gateway/bot', self._gateway)
        app.router.add_get('/ws', self._websocket)
        app.router.add_post('/v2/{collection:groups|users}/{target}/messages', self._send)
        app.router.add_post('/v2/{collection:groups|users}/{target}/files', self.files)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, '127.0.0.1', 0)
        await site.start()
        self.base_url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        self.gateway_url = self.base_url.replace('http://', 'ws://') + '/ws'


class FileCompositionTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime_fixture.OfficialRuntimeTests.setUp
    runtime = runtime_fixture.OfficialRuntimeTests.runtime
    query = runtime_fixture.OfficialRuntimeTests.query
    wait_db = runtime_fixture.OfficialRuntimeTests.wait_db
    application = turn_fixture.OfficialTurnEndToEndTests.application
    emit = turn_fixture.OfficialTurnEndToEndTests.emit
    complete = turn_fixture.OfficialTurnEndToEndTests.complete

    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(prefix='qq-file-composition-')
        self.addCleanup(self.temporary.cleanup)
        self.manager = FakeManager()
        self.verified = True
        self.proof_time = int(time.time())
        self.fake = GatewayMediaFake()
        self.fake.app_id = 'TEST_FILE_COMPOSED_' + self._testMethodName
        self.fake.receipt_ttl = 1800
        await self.fake.start()
        self.addAsyncCleanup(self.fake.close)
        original = web.WebSocketResponse.send_json
        async def ready(socket, data, *args, **kwargs):
            if isinstance(data, dict) and data.get('t') == 'READY':
                data = {**data, 'd': {**data['d'], 'user': {**data['d']['user'], 'bot': True}}}
            return await original(socket, data, *args, **kwargs)
        self.ready_patch = patch.object(web.WebSocketResponse, 'send_json', new=ready)
        self.ready_patch.start()
        self.addCleanup(self.ready_patch.stop)

    def config(self):
        def evidence(cursor, endpoint):
            if not self.verified:
                return None
            row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone()
            return NativeRouteEvidence(endpoint, row['generation'], row['route_epoch'], row['capability_revision'],
                1024*1024, self.proof_time, self.proof_time + 3600)
        return FileServiceConfig(Path(self.temporary.name), self.manager,
            route_authority=lambda cursor, endpoint, actor, route: route == 'native', native_evidence=evidence,
            generation_enabled=True, max_file_bytes=1024*1024, min_free_bytes=0,
            generation_limits=GenerationLimits(max_file_bytes=1024*1024))

    async def open(self, app, **kwargs):
        runtime = self.runtime(loaded=runtime_fixture.loaded_config(self.fake.app_id, base=self.fake.base_url),
            application=app, ledger=app.message_ledger, journal=app.turn_journal,
            file_services_factory=file_services_factory(self.config()), **kwargs)
        self.assertTrue(await runtime.start())
        await asyncio.wait_for(self.fake.wait_ready(), 5)
        await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
        return runtime

    async def test_foreground_real_source_generates_uploads_and_commits_one_file_after_parent_finishes(self):
        app = self.application()
        calls, sandbox = [], None
        self.fake.files_release = asyncio.Event()
        async def completion(**kwargs):
            nonlocal sandbox
            calls.append(kwargs)
            names = {t['function']['name'] for t in kwargs['tools']}
            self.assertIn('sandbox_exec', names)
            self.assertFalse(names & {'host_exec', 'project_file_upload', 'get_group_file_list'})
            responses = [m['content'] for m in kwargs['messages'] if m['role'] == 'tool']
            if len(calls) == 1:
                name, args = 'sandbox_create', {'runtime': 'python'}
            elif len(calls) == 2:
                sandbox = json.loads(responses[-1][responses[-1].index('{'):responses[-1].rindex('}')+1])['sandbox_id']
                name, args = 'sandbox_write_file', {'sandbox_id': sandbox, 'path': 'input.md', 'content': 'TEST source-bound PDF'}
            elif len(calls) == 3:
                name, args = 'sandbox_exec', {'sandbox_id': sandbox, 'command': 'generate-pdf'}
            elif len(calls) == 4:
                name, args = 'send_file_from_sandbox', {'sandbox_id': sandbox, 'path': 'result.pdf', 'filename': 'report.pdf'}
            else:
                receipt = json.loads(responses[-1][responses[-1].index('{'):responses[-1].rindex('}')+1])
                self.assertTrue(receipt['stored'])
                self.assertFalse(receipt['message_accepted'])
                name, args = None, None
            tool_calls = [SimpleNamespace(id='TEST_GENERATE_' + str(len(calls)), type='function',
                function=SimpleNamespace(name=name, arguments=json.dumps(args)))] if name else []
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='' if name else 'TEST document stored; waiting for delivery', tool_calls=tool_calls))], usage=None)
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=completion):
            runtime = await self.open(app)
            await self.emit(self.fake, native='TEST_FILE_SOURCE', content='TEST generate and send PDF')
            await self.complete('TEST_FILE_SOURCE')
            try:
                await asyncio.wait_for(self.fake.files_seen.wait(), 5)
            except TimeoutError:
                self.fail(str({'calls': len(calls),
                    'files': self.query('SELECT state,reason_code FROM qq_official_file_deliveries'),
                    'jobs': self.query('SELECT kind,status,last_error FROM durable_jobs'),
                    'responses': [m['content'] for c in calls for m in c['messages'] if m['role'] == 'tool']}))
            rows = self.query('SELECT * FROM qq_official_file_artifacts')
            self.assertEqual(len(rows), 1)
            self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries')[0]['state'], 'uploading')
            self.assertEqual(self.query('SELECT state FROM qq_official_turn_executions')[0]['state'], 'handed_off')
            self.fake.files_release.set()
            await self.wait_db("SELECT * FROM qq_official_file_deliveries WHERE state='message_accepted'")
            body = self.fake.uploaded[('users', 'TEST_RUNTIME_OWNER')]
            self.assertEqual(hashlib.sha256(body).hexdigest(), rows[0]['sha256'])
            media = [item for item in self.fake.received if item['payload']['msg_type'] == 7]
            self.assertEqual(len(media), 1)
            self.assertEqual(media[0]['payload']['msg_id'], 'TEST_FILE_SOURCE')
            self.assertEqual(len(self.fake.media_calls), 1)
            self.assertEqual(len(calls), 5)
            self.assertFalse(self.manager.items)
            self.assertEqual(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'"), [])
            self.assertEqual(len(self.query('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL')), 2)
            # Private upload receipt never enters context or model replay.
            self.assertNotIn('TEST_FILE_INFO', json.dumps(self.query('SELECT body_json FROM messages')))
            outbox = runtime.outbox
            await runtime.stop()
            self.assertIsNone(runtime.file_service)
            self.assertIsNone(outbox.protected_payloads)

    async def test_unverified_native_route_never_advertises_generation(self):
        self.verified = False
        async def completion(**kwargs):
            names = {t['function']['name'] for t in kwargs['tools']}
            self.assertFalse(names & {'sandbox_exec', 'sandbox_create', 'send_file_from_sandbox'})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='TEST unavailable route', tool_calls=[]))], usage=None)
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=completion):
            runtime = await self.open(self.application())
            await self.emit(self.fake, native='TEST_NO_ROUTE', content='TEST request')
            await self.complete('TEST_NO_ROUTE')
            self.assertEqual(self.fake.media_calls, [])
            self.assertEqual(self.manager.calls, [])
            self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'), [])
            await runtime.stop()


if __name__ == '__main__':
    unittest.main()
