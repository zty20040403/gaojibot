"""Actual gateway/shared model loop/file worker/M3 receipts with loopback I/O."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

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
        self.manager._guest_exec = AsyncMock(return_value=(0, b'', b''))
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
        calls, phases = [], {'foreground': 0, 'background': 0}
        async def completion(**kwargs):
            calls.append(kwargs)
            names = {t['function']['name'] for t in kwargs['tools']}
            self.assertIn('agent', names)
            phases['foreground'] += 1
            step = phases['foreground']
            self.assertFalse(names & {'host_exec', 'project_file_upload', 'sandbox_exec'})
            responses = [m['content'] for m in kwargs['messages'] if m['role'] == 'tool']
            name, args = None, None
            content = '[silence]'
            if step == 1:
                name, args = 'say', {'text': 'TEST beginning PDF'}
            elif step == 2:
                name, args = 'agent', {'task': 'TEST generate a PDF', 'acceptance': 'TEST PDF file exists and is readable', 'title': 'TEST PDF'}
            else:
                self.assertIn('job#', responses[-1])
            tool_calls = [SimpleNamespace(id='TEST_GENERATE_' + str(len(calls)), type='function',
                function=SimpleNamespace(name=name, arguments=json.dumps(args)))] if name else []
            message = SimpleNamespace(content='' if name else content, tool_calls=tool_calls)
            if kwargs.get('stream'):
                async def stream():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=message, finish_reason='stop')])
                return stream()
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        async def background(engine, row, session, *, feedback, progress, trace):
            # Pi in the container, reduced to the file it leaves behind.
            from src.plugins.ai_chat.background_tools import workspace_owner
            from src.plugins.ai_chat.platform_types import EndpointKey
            phases['background'] += 1
            owner = workspace_owner(EndpointKey.parse(row['endpoint_key']))
            sandbox = (await self.manager.ensure_default(owner))['sandbox_id']
            await self.manager.write_file(owner, sandbox, 'input.md', b'TEST source-bound PDF')
            await self.manager.exec(owner, sandbox, 'generate-pdf')
            from tests.test_frontend_direct_exec import _tiny_png
            await self.manager.write_file(owner, sandbox, 'result.png', _tiny_png())
            await progress('shell')
            import httpx
            await engine.gateway.start()
            token, channel = engine.gateway.open(source=row, current=session.current)
            channel.session = session
            url = f'http://127.0.0.1:{engine.gateway.port}'
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token}) as client:
                tools = (await client.get(url + '/job/tools')).json()['tools']
                self.assertTrue({'send_file', 'send_image', 'action_status'} <= {t['name'] for t in tools})
                async def invoke(name, arguments):
                    response = await client.post(url + '/job/tool', json={'name': name, 'arguments': arguments})
                    event = json.loads(response.text.strip().splitlines()[-1])
                    self.assertTrue(event['ok'], event)
                    return json.loads(event['content'][0]['text'])
                args = {'path': '/work/result.pdf', 'filename': 'report.pdf', 'request_id': 'TEST_PDF'}
                receipt = await invoke('send_file', args)
                self.assertEqual(receipt['state'], 'completed', receipt)
                self.assertEqual(await invoke('send_file', args), receipt)
                self.assertEqual(await invoke('action_status', {'action_id': receipt['action_id']}), receipt)
                image = await invoke('send_image', {'path': '/work/result.png', 'request_id': 'TEST_IMAGE'})
                self.assertEqual(image['state'], 'completed', image)
                self.assertEqual(await invoke('action_status', {'request_id': 'TEST_IMAGE'}), image)
                self.fake.drop_next_response = True
                uncertain_args = {'path': '/work/result.pdf', 'request_id': 'TEST_UNCERTAIN'}
                uncertain = await invoke('send_file', uncertain_args)
                self.assertEqual(uncertain['state'], 'ambiguous', uncertain)
                self.assertEqual(await invoke('send_file', uncertain_args), uncertain)
                conflict = await client.post(url + '/job/tool', json={'name': 'send_file',
                    'arguments': uncertain_args | {'path': '/work/result.png'}})
                self.assertFalse(json.loads(conflict.text.strip().splitlines()[-1])['ok'])
                missing = await invoke('send_file', {'path': '/work/missing.pdf', 'request_id': 'TEST_MISSING'})
                self.assertFalse(missing['ok'])
                from src.plugins.ai_chat.adapters.qq_official.job_actions import receipt_details
                with app.official_runtime.store.transaction() as cursor:
                    self.assertEqual(len(receipt_details(cursor, row)), 3)
                    for changed in ({'job_id': row['job_id'] + 1}, {'endpoint_key': 'TEST_OTHER_GROUP'},
                                    {'actor_key': 'TEST_OTHER_ACTOR'}):
                        self.assertEqual(receipt_details(cursor, row | changed), [])
            engine.gateway.revoke(token)
            return ('结果：成功\n[纯文本报告 v1]\n结论 1: TEST PDF ready.\n'
                    '证据 1: TEST local result\n[/纯文本报告]')

        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=completion), \
                patch('src.plugins.ai_chat.pi_engine.PiEngine.__call__', new=background):
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
            self.assertEqual(self.query('SELECT state FROM background_jobs')[0]['state'], 'completed')
            body = self.fake.uploaded[('groups', 'TEST_GROUP') if group else ('users', 'TEST_RUNTIME_OWNER')]
            self.assertTrue(len(body) > 0)
            media = [item for item in self.fake.received if item['payload']['msg_type'] == 7]
            self.assertEqual(len(media), 3)
            self.assertNotIn('msg_id', media[0]['payload'])
            self.assertEqual(len(self.fake.media_calls), 3)
            self.assertEqual(phases, {'foreground': 3, 'background': 1})
            self.assertEqual(len(self.manager.items), 1)
            await self.wait_db('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL', predicate=lambda rows: len(rows) == 4)
            notices = self.query("SELECT d.body_json FROM deliveries d JOIN delivery_plans p USING(plan_id) WHERE p.purpose='background_result'")
            self.assertIn('已送达 2', notices[0]['body_json'])
            self.assertIn('结果不确定 1', notices[0]['body_json'])
            self.assertNotIn('TEST_FILE_INFO', json.dumps(self.query('SELECT body_json FROM messages')))
            if group:
                self.assertEqual(self.query('SELECT DISTINCT scope_key FROM conversations'), [{'scope_key': scope.key}])
            await runtime.stop()
            self.assertIsNone(runtime.file_service)

    async def test_two_group_jobs_and_a_private_job_each_own_their_receipt(self):
        from src.plugins.ai_chat.background_tools import workspace_owner
        from src.plugins.ai_chat.platform_types import EndpointKey
        from src.plugins.ai_chat.sandbox_gaoji import Client

        app, actions = self.application(), []
        async def completion(**kwargs):
            new = not any(m['role'] == 'tool' for m in kwargs['messages'])
            args = json.dumps({'task': 'TEST file', 'acceptance': 'TEST receipt'})
            calls = [SimpleNamespace(id='TEST_START', type='function', function=SimpleNamespace(name='agent', arguments=args))] if new else []
            msg = SimpleNamespace(content='' if new else '[silence]', tool_calls=calls)
            if kwargs.get('stream'):
                async def stream():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=msg, finish_reason='stop')])
                return stream()
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

        async def background(engine, row, session, **kwargs):
            owner = workspace_owner(EndpointKey.parse(row['endpoint_key']))
            sid = (await self.manager.ensure_default(owner))['sandbox_id']
            await self.manager.write_file(owner, sid, 'result.pdf', b'TEST independent document')
            await engine.gateway.start()
            token, channel = engine.gateway.open(source=row, current=session.current)
            channel.session = session
            client = Client()
            try:
                with patch.object(client, 'credentials', return_value=('127.0.0.1', engine.gateway.port, token)):
                    code, receipt = await asyncio.to_thread(client.call_tool, 'send_file',
                        {'request_id': 'TEST_EXPORT', 'path': '/work/result.pdf'})
                    self.assertEqual((code, receipt.get('state')), (200, 'completed'), receipt)
                    code, own = await asyncio.to_thread(client.call_tool, 'action_status', {'request_id': 'TEST_EXPORT'})
                    self.assertEqual((code, own), (200, receipt))
                    for _, _, previous in actions:
                        code, result = await asyncio.to_thread(client.call_tool, 'action_status', {'action_id': previous})
                        self.assertFalse(result['ok'])
                        self.assertNotEqual(code, 200)
                    actions.append((row['job_id'], row['endpoint_key'], receipt['action_id']))
            finally:
                engine.gateway.revoke(token)
            return '结果：成功\n[纯文本报告 v1]\n结论 1: TEST result\n证据 1: TEST local\n[/纯文本报告]'

        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=completion), patch('src.plugins.ai_chat.pi_engine.PiEngine.__call__', new=background):
            runtime = await self.open(app)
            try:
                endpoint = EndpointKey(runtime.loaded.account, 'group', 'TEST_GROUP')
                self.query("INSERT INTO qq_official_endpoints(endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,proactive_state,created_at,updated_at) VALUES (?,?,'group','TEST_GROUP',?,'active','present','allowed',1,1) RETURNING endpoint_key", (endpoint.key,endpoint.account.key,endpoint.scope.key))
                self.fake.proactive_allowed[('group', 'TEST_GROUP')] = True
                for i, group in enumerate((True, True, False), 1):
                    await self.emit(self.fake, native=f'TEST_JOB_SOURCE_{i}', content=f'TEST file {i}', group=group)
                    rows = await self.wait_db('SELECT state,error FROM background_jobs WHERE delivered_at IS NOT NULL',
                        predicate=lambda rows: len(rows) == i, timeout=20)
                    self.assertTrue(all(r['state'] == 'completed' for r in rows), rows)
                self.assertEqual(len({job for job, _, _ in actions}), 3)
                self.assertEqual(len({ep for _, ep, _ in actions}), 2)
                self.assertEqual(len({action for _, _, action in actions}), 3)
            finally:
                await runtime.stop()

    async def test_cancelled_job_cannot_dispatch_an_already_queued_file(self):
        app = self.application()
        seen, ready, release, exited = {}, asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def completion(**kwargs):
            responses = [m for m in kwargs['messages'] if m['role'] == 'tool']
            calls = [] if responses else [SimpleNamespace(id='TEST_START', type='function',
                function=SimpleNamespace(name='agent', arguments=json.dumps(
                    {'task': 'TEST make file', 'acceptance': 'TEST file delivered'})))]
            message = SimpleNamespace(content='' if calls else '[silence]', tool_calls=calls)
            if kwargs.get('stream'):
                async def stream():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=message, finish_reason='stop')])
                return stream()
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        async def background(engine, row, session, **kwargs):
            from src.plugins.ai_chat.background_tools import workspace_owner
            from src.plugins.ai_chat.platform_types import EndpointKey
            from src.plugins.ai_chat.adapters.qq_official.send_types import SendOutcome
            owner = workspace_owner(EndpointKey.parse(row['endpoint_key']))
            sid = (await self.manager.ensure_default(owner))['sandbox_id']
            await self.manager.write_file(owner, sid, 'result.pdf', b'TEST_PDF')
            runtime = app.official_runtime
            sender = runtime.sender
            try:
                with patch.object(sender, 'send_delivery', new=AsyncMock(return_value=SendOutcome('pending'))):
                    result = json.loads(await session.execute_tool('send_file',
                        {'path': '/work/result.pdf', 'request_id': 'TEST_QUEUED'}))
                    self.assertEqual(result['state'], 'queued')
                    seen['row'] = row
                    seen['delivery'] = self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?', (result['action_id'],))[0]
                    ready.set()
                    await release.wait()
            finally:
                exited.set()
            return 'TEST cancelled report'

        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=completion), \
                patch('src.plugins.ai_chat.pi_engine.PiEngine.__call__', new=background):
            runtime = await self.open(app)
            await self.emit(self.fake, native='TEST_CANCEL_SOURCE', content='TEST request')
            await asyncio.wait_for(ready.wait(), 10)
            row = seen['row']
            self.assertTrue(runtime.agent_service.store.cancel(row['job_id'], row['endpoint_key'], row['actor_key']))
            release.set()
            await asyncio.wait_for(exited.wait(), 5)
            outcome = await runtime.sender.send_delivery(seen['delivery']['delivery_id'])
            await self.wait_db("SELECT * FROM background_jobs WHERE state='cancelled' AND delivered_at IS NOT NULL", timeout=20)
            self.assertEqual(outcome.status, 'cancelled')
            self.assertEqual([m for m in self.fake.received if m['payload']['msg_type'] == 7], [])
            notice = self.query("SELECT d.body_json FROM deliveries d JOIN delivery_plans p USING(plan_id) WHERE p.purpose='background_result'")
            self.assertTrue('取消 1' in notice[0]['body_json'] or '排队中 1' in notice[0]['body_json'])
            await runtime.stop()

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
