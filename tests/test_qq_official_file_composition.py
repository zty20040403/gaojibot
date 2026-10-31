"""Actual gateway/shared model loop/file worker/M3 receipts with loopback I/O."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web

from src.plugins.ai_chat.adapters.qq_official.file_services import (
    FileServiceConfig, file_services_factory,
)
from src.plugins.ai_chat.application.owned_sandbox import GenerationLimits
from tests import test_qq_official_runtime as runtime_fixture
from tests import test_qq_official_turn_endtoend as turn_fixture
from tests.qq_official_file_http_fixture import FileMediaFake
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
        self.logger = logging.getLogger(__name__)
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
        return FileServiceConfig(Path(self.temporary.name), self.manager,
            generation_enabled=True, max_file_bytes=1024*1024, min_free_bytes=0,
            generation_limits=GenerationLimits(max_file_bytes=1024*1024))

    async def open(self, app, **kwargs):
        runtime = self.runtime(loaded=runtime_fixture.loaded_config(self.fake.app_id, base=self.fake.base_url),
            application=app, ledger=app.message_ledger, journal=app.turn_journal,
            file_services_factory=file_services_factory(self.config()), **kwargs)
        from src.plugins.ai_chat.adapters.qq_official.host_services import HostServices
        def host(current, token):
            service = HostServices(database=current.database, transaction=current.deliveries.transaction, database_calls=current._calls)
            return service.official_factory(current, token)
        runtime.migration_factory = host
        app.official_runtime = runtime
        app.sandbox_manager = self.manager
        app.semantic_recall = None
        self.assertTrue(await runtime.start())
        await asyncio.wait_for(self.fake.wait_ready(), 5)
        await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
        return runtime

    async def test_bound_group_file_reply_keeps_original_timeline(self):
        await self.test_foreground_gets_file_receipt_before_final_answer()

    async def test_foreground_gets_file_receipt_before_final_answer(self):
        app = self.application()
        calls, phases = [], {'foreground': 0, 'background': 0, 'delivery': 0}
        async def completion(**kwargs):
            calls.append(kwargs)
            names = {t['function']['name'] for t in kwargs['tools']}
            phase = 'background' if 'shell' in names else 'foreground' if 'agent' in names else 'delivery'
            phases[phase] += 1
            step = phases[phase]
            self.assertFalse(names & {'host_exec', 'project_file_upload', 'sandbox_exec'})
            responses = [m['content'] for m in kwargs['messages'] if m['role'] == 'tool']
            name, args = None, None
            content = '[silence]'
            if phase == 'foreground':
                if step == 1:
                    name, args = 'say', {'text': 'TEST beginning PDF'}
                elif step == 2:
                    name, args = 'agent', {'task': 'TEST generate a PDF', 'title': 'TEST PDF'}
                else:
                    self.assertIn('job#', responses[-1])
            elif phase == 'background':
                if step == 1:
                    name, args = 'write_file', {'path': 'input.md', 'content': 'TEST source-bound PDF'}
                elif step == 2:
                    name, args = 'shell', {'command': 'generate-pdf'}
                else:
                    content = 'TEST finished; file is /work/result.pdf'
            elif step == 1:
                name, args = 'send_file_from_sandbox', {'path': '/work/result.pdf', 'filename': 'report.pdf'}
            else:
                receipt = json.loads(responses[-1][responses[-1].index('{'):responses[-1].rindex('}')+1])
                self.assertTrue(receipt['stored'])
                self.assertTrue(receipt['message_accepted'])
                content = 'TEST file delivered'
            tool_calls = [SimpleNamespace(id='TEST_GENERATE_' + str(len(calls)), type='function',
                function=SimpleNamespace(name=name, arguments=json.dumps(args)))] if name else []
            message = SimpleNamespace(content='' if name else content, tool_calls=tool_calls)
            if kwargs.get('stream'):
                async def stream():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=message, finish_reason='stop')])
                return stream()
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=completion):
            runtime = await self.open(app)
            group = self._testMethodName == 'test_bound_group_file_reply_keeps_original_timeline'
            if group:
                from src.plugins.ai_chat.platform_types import EndpointKey
                from src.plugins.ai_chat.conversation_scope import ConversationScope
                endpoint = EndpointKey(runtime.loaded.account, 'group', 'TEST_GROUP')
                scope = ConversationScope('onebot-v11', 'group', '1234567')
                self.query("INSERT INTO qq_official_endpoints (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,proactive_state,created_at,updated_at) VALUES (?,?,'group','TEST_GROUP',?,'active','present','allowed',?,?) RETURNING endpoint_key",
                    (endpoint.key, endpoint.account.key, scope.key, int(time.time()), int(time.time())))
                self.fake.proactive_allowed[('group', 'TEST_GROUP')] = True
            await self.emit(self.fake, native='TEST_FILE_SOURCE', content='TEST generate and send PDF', group=group)
            try:
                finished = await self.wait_db("SELECT * FROM background_jobs WHERE state NOT IN ('queued','running')", timeout=20)
            except TimeoutError:
                self.fail(str({'phases': phases, 'jobs': self.query('SELECT job_id,state,error FROM background_jobs'),
                    'responses': [m['content'] for c in calls for m in c['messages'] if m['role'] == 'tool']}))
            self.assertEqual(finished[0]['state'], 'completed', finished[0]['error'])
            try:
                await asyncio.wait_for(self.fake.files_seen.wait(), 20)
            except TimeoutError:
                self.fail(str({'calls': len(calls),
                    'files': self.query('SELECT state,reason_code FROM qq_official_file_deliveries'),
                    'jobs': self.query('SELECT kind,status,last_error FROM durable_jobs'),
                    'responses': [m['content'] for c in calls for m in c['messages'] if m['role'] == 'tool']}))
            self.assertEqual(self.query("SELECT job_id FROM durable_jobs WHERE kind LIKE ? OR kind LIKE ?", ('qq_official.ingest.%', 'qq_official.turn.%')), [])
            await self.wait_db('SELECT * FROM background_jobs WHERE delivered_at IS NOT NULL', timeout=20)
            rows = self.query('SELECT * FROM qq_official_file_artifacts')
            self.assertEqual(len(rows), 1)
            self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries')[0]['state'], 'message_accepted')
            self.assertEqual(self.query('SELECT state FROM background_jobs')[0]['state'], 'completed')
            await self.wait_db("SELECT * FROM qq_official_file_deliveries WHERE state='message_accepted'")
            body = self.fake.uploaded[('groups', 'TEST_GROUP') if group else ('users', 'TEST_RUNTIME_OWNER')]
            self.assertEqual(hashlib.sha256(body).hexdigest(), rows[0]['sha256'])
            media = [item for item in self.fake.received if item['payload']['msg_type'] == 7]
            self.assertEqual(len(media), 1)
            self.assertNotIn('msg_id', media[0]['payload'])
            self.assertEqual(len(self.fake.media_calls), 1)
            self.assertEqual(phases, {'foreground': 3, 'background': 3, 'delivery': 2})
            self.assertEqual(len(self.manager.items), 1)
            self.assertEqual(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'"), [])
            await self.wait_db('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL', predicate=lambda rows: len(rows) == 3)
            # Private upload receipt never enters context or model replay.
            self.assertNotIn('TEST_FILE_INFO', json.dumps(self.query('SELECT body_json FROM messages')))
            if group:
                self.assertEqual(self.query('SELECT DISTINCT scope_key FROM conversations'), [{'scope_key': scope.key}])
            outbox = runtime.outbox
            await runtime.stop()
            self.assertIsNone(runtime.file_service)
            self.assertIsNone(outbox.protected_payloads)

    async def test_generation_does_not_require_native_route_proof(self):
        self.verified = False
        async def completion(**kwargs):
            names = {t['function']['name'] for t in kwargs['tools']}
            self.assertTrue({'agent', 'sandbox_read_file', 'send_file_from_sandbox'} <= names)
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
