from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
import zipfile

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from src.plugins.ai_chat.application.owned_sandbox import (OwnedSandboxExecutor, GenerationLimits, GenerationError,
    ArtifactAvailability, ArtifactRegistration)
from src.plugins.ai_chat.tools.owned_sandbox import HANDLERS
from src.plugins.ai_chat.execution_control import LeaseLost
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


PDF = b'%PDF-1.4\nTEST synthetic exported PDF bytes\n%%EOF\n'


class Binding:
    def __init__(self, source='TEST_EXEC_1', *, endpoint=None, actor=None):
        self.endpoint = endpoint or EndpointKey(AccountKey('qq-official', 'TEST_APP', 'test'), 'group', 'TEST_GROUP')
        self.actor = actor or ActorKey(self.endpoint.account, 'group_member', self.endpoint.native_conversation_id, 'TEST_ACTOR')
        self.source_key, self.source_native_message_id = source, 'TEST_NATIVE'
        self.deadline_at, self.valid = 200, True
        self.reservations, self.released, self.checks = [], [], 0
    async def assert_current(self):
        self.checks += 1
        if not self.valid:
            raise LeaseLost('TEST_lost_source')
    async def reserve_capacity(self, owner):
        reservation = object()
        self.reservations.append((owner, reservation))
        return reservation
    async def release_capacity(self, reservation):
        self.released.append(reservation)


class FakeManager:
    """Real manager signatures, in-memory owned guest; no subprocess/network."""
    backend = 'vm'
    max_file_bytes = 1024 * 1024
    def __init__(self):
        self.items, self.calls = {}, []
        self.after, self.fail = None, None
        self.unembedded, self.cancel_exec = False, False
    def event(self, name, owner, sid=None):
        self.calls.append((name, owner, sid))
        if self.fail == name:
            raise RuntimeError('TEST_SECRET /host/private/path https://TEST_SECRET.invalid')
        if self.after:
            self.after(name, owner, sid)
    def own(self, owner, sid):
        if sid not in self.items or self.items[sid]['owner'] != owner:
            raise ValueError('TEST_wrong_owner')
        return self.items[sid]
    async def create(self, owner, runtime='python', *, purpose='task'):
        assert purpose == 'task'
        await asyncio.sleep(0)
        sid = 's' + format(len(self.items) + 1, '06x')
        while sid in self.items:
            sid = 's' + format(int(sid[1:], 16) + 1, '06x')
        self.items[sid] = {'owner': owner, 'files': {}}
        self.event('create', owner, sid)
        return {'sandbox_id': sid, 'purpose': purpose}
    async def list(self, owner):
        self.event('list', owner)
        return [{'sandbox_id': sid} for sid, item in self.items.items() if item['owner'] == owner]
    async def start_owned(self, owner, sandbox_id):
        self.own(owner, sandbox_id)
        self.event('start', owner, sandbox_id)
    async def stop_owned(self, owner, sandbox_id):
        self.own(owner, sandbox_id)
        self.event('stop', owner, sandbox_id)
    async def destroy(self, owner, sandbox_id):
        self.own(owner, sandbox_id)
        self.items.pop(sandbox_id)
        self.event('destroy', owner, sandbox_id)
    async def write_file(self, owner, sandbox_id, path, content, *, allow_large=False):
        self.own(owner, sandbox_id)['files'][path] = bytes(content)
        self.event('write', owner, sandbox_id)
        return len(content)
    async def read_file(self, owner, sandbox_id, path, max_bytes=65536):
        data = self.own(owner, sandbox_id)['files'][path]
        if len(data) > max_bytes:
            raise ValueError('too_large')
        self.event('read', owner, sandbox_id)
        return data
    async def exec(self, owner, sandbox_id, command, timeout_seconds=None, packages=None):
        files = self.own(owner, sandbox_id)['files']
        if self.cancel_exec:
            raise asyncio.CancelledError()
        stdout = ''
        if command == 'generate-pdf':
            assert 'input.md' in files
            files['result.pdf'] = PDF + files['input.md']
        elif command == 'generate-zip':
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('main.py', files['input.md'])
            files['result.zip'] = output.getvalue()
        elif command == 'pdffonts acceptance.pdf':
            stdout = 'TESTFONT TrueType yes yes yes 3 0' if not self.unembedded else 'TESTFONT TrueType no no no 3 0'
        elif command == 'pdftotext acceptance.pdf -':
            stdout = 'TEST readable text'
        elif command not in {'pdfinfo acceptance.pdf', 'unzip -t acceptance.zip'}:
            raise ValueError('TEST unexpected command')
        self.event('exec', owner, sandbox_id)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='', manifest=None)
    async def export_artifact(self, owner, sandbox_id, path, *, file_only=False, max_bytes=None):
        assert file_only is True and type(max_bytes) is int and max_bytes > 0
        content = self.own(owner, sandbox_id)['files'][path]
        if not 0 < len(content) <= min(max_bytes, self.max_file_bytes):
            raise ValueError('too_large')
        self.event('export', owner, sandbox_id)
        return bytes(content), False


class Delivery:
    def __init__(self):
        self.available, self.max_bytes, self.route_kind = True, 1024 * 1024, 'link'
        self.registrations, self.calls, self.after = {}, [], None
    async def readiness(self, binding):
        return ArtifactAvailability(self.available, self.max_bytes, self.route_kind)
    async def register(self, binding, exported, *, logical_request_key, draft, draft_reason):
        await binding.assert_current()
        self.calls.append((binding.source_key, exported, logical_request_key, draft, draft_reason))
        item = self.registrations.setdefault(logical_request_key, exported)
        if item.sha256 != exported.sha256:
            raise ValueError('conflict')
        if self.after:
            self.after()
        return ArtifactRegistration('artifact_TEST_' + str(len(self.registrations)), 'waiting')


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.binding, self.manager, self.delivery = Binding(), FakeManager(), Delivery()
        self.executor = OwnedSandboxExecutor(manager=self.manager, binding=self.binding,
            delivery=self.delivery, clock=lambda: 100)
    async def create(self):
        result = json.loads(await self.executor.execute('sandbox_create', {'runtime': 'python'}))
        self.assertTrue(result['ok'], result)
        return result['sandbox_id']
    async def produce(self, kind='pdf'):
        sid = await self.create()
        written = json.loads(await self.executor.execute('sandbox_write_file', {
            'sandbox_id': sid, 'path': 'input.md', 'content': 'TEST content'}))
        self.assertEqual(written['bytes_written'], 12)
        generated = json.loads(await self.executor.execute('sandbox_exec', {
            'sandbox_id': sid, 'command': 'generate-' + kind}))
        self.assertTrue(generated['ok'])
        return sid
    async def send(self, sid, kind='pdf', *, call='TEST_CALL'):
        token = active_tool_call_id.set(call)
        try:
            return json.loads(await self.executor.execute('send_file_from_sandbox', {
                'sandbox_id': sid, 'path': 'result.' + kind}))
        finally:
            active_tool_call_id.reset(token)


    async def test_invalid_export_is_unsent_and_allows_workspace_cleanup(self):
        class UnknownExport(FakeManager):
            async def export_artifact(self, owner, sandbox_id, path, *, file_only=False, max_bytes=None):
                await super().export_artifact(owner,sandbox_id,path,file_only=file_only,max_bytes=max_bytes)
                return b'UNCONFIRMED', True
        self.manager = UnknownExport()
        self.executor = OwnedSandboxExecutor(binding=self.binding,delivery=self.delivery,manager=self.manager,clock=lambda:100)
        sid = await self.produce()
        result = await self.send(sid)
        self.assertEqual(result['code'],'invalid_export_receipt')
        self.assertTrue(result['retryable'])
        self.assertFalse(result['message_accepted'])
        self.assertTrue(json.loads(await self.executor.execute('sandbox_destroy',{'sandbox_id':sid}))['ok'])

    async def test_export_failure_is_unsent_and_can_retry_with_absolute_path(self):
        sid = await self.produce()
        self.manager.fail = 'export'
        result = await self.send(sid)
        self.assertEqual(result['code'],'file_export_failed')
        self.assertTrue(result['retryable'])
        self.assertFalse(result['message_accepted'])
        self.manager.fail = None
        token = active_tool_call_id.set('TEST_RETRY')
        try:
            retry = json.loads(await self.executor.execute('send_file_from_sandbox', {
                'sandbox_id': sid, 'path': '/workspace/result.pdf'}))
        finally:
            active_tool_call_id.reset(token)
        self.assertTrue(retry['stored'])

    async def test_pdf_bytes_hash_size_are_exported_and_only_registered(self):
        sid = await self.produce()
        result = await self.send(sid)
        self.assertTrue(result['stored'])
        self.assertFalse(result['message_accepted'])
        self.assertFalse(result['content_accepted'])
        self.assertEqual(result['delivery_state'], 'waiting')
        artifact = self.delivery.calls[0][1]
        self.assertEqual(artifact.content, self.manager.items[sid]['files']['result.pdf'])
        self.assertEqual(artifact.sha256, hashlib.sha256(artifact.content).hexdigest())
        self.assertEqual(artifact.size, len(artifact.content))
        self.assertFalse(any(owner.endswith(':verify') for action, owner, _ in self.manager.calls if action == 'exec'))
        self.assertNotIn('TEST_ACTOR', self.executor.owner)
        for hidden in (artifact.sha256, 'TEST content', 'TEST_GROUP', 'result.pdf'):
            self.assertNotIn(hidden, repr(artifact))

    async def test_zip_generation_uses_actual_owned_export_bytes(self):
        sid = await self.produce('zip')
        result = await self.send(sid, 'zip')
        self.assertTrue(result['stored'])
        artifact = self.delivery.calls[0][1]
        with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
            self.assertEqual(archive.read('main.py'), b'TEST content')
        self.assertEqual(artifact.mime, 'application/zip')
        self.assertEqual(self.delivery.route_kind, 'link')

    async def test_no_route_rejects_before_capacity_or_sandbox_creation(self):
        self.delivery.available = False
        result = json.loads(await self.executor.execute('sandbox_create', {}))
        self.assertEqual(result['code'], 'artifact_route_unavailable')
        self.assertFalse(self.manager.calls)
        self.assertFalse(self.binding.reservations)

    async def test_unpatched_or_unlimited_manager_fails_closed(self):
        self.manager.max_file_bytes = 0
        self.assertEqual(json.loads(await self.executor.execute('sandbox_create', {}))['code'], 'bounded_export_unavailable')
        self.assertFalse(self.manager.calls)

    async def test_rpc_runner_requires_real_bounded_export_handshake_before_create(self):
        self.manager.requires_export_handshake = True
        async def old_health():
            return {'max_file_bytes': 1024}
        self.manager.health = old_health
        result = json.loads(await self.executor.execute('sandbox_create', {}))
        self.assertEqual(result['code'], 'bounded_export_runner_unavailable')
        self.assertFalse(self.manager.calls)
        async def new_health():
            return {'bounded_file_export_version': 1, 'max_file_bytes': 1024}
        self.manager.health = new_health
        self.assertEqual(await self.executor.readiness(), 1024)
        self.assertTrue(json.loads(await self.executor.execute('sandbox_create', {}))['ok'])


    async def test_identity_mutation_and_deadline_raise_lease_lost(self):
        self.binding.source_key = 'TEST_CHANGED'
        with self.assertRaises(LeaseLost):
            await self.executor.execute('sandbox_create', {})
        self.binding.source_key = 'TEST_EXEC_1'
        self.executor.clock = lambda: 200
        with self.assertRaises(LeaseLost):
            await self.executor.execute('sandbox_create', {})
        self.assertFalse(self.manager.calls)

    async def test_loss_after_create_remembers_resource_and_does_not_continue(self):
        self.manager.after = lambda name, *_: setattr(self.binding, 'valid', False) if name == 'create' else None
        with self.assertRaises(LeaseLost):
            await self.executor.execute('sandbox_create', {})
        self.assertEqual(len(self.executor.retained_resources), 1)
        self.assertEqual([c[0] for c in self.manager.calls], ['list', 'create'])

    async def test_loss_after_start_blocks_write_boundary(self):
        sid = await self.create()
        self.manager.after = lambda name, *_: setattr(self.binding, 'valid', False) if name == 'start' else None
        with self.assertRaises(LeaseLost):
            await self.executor.execute('sandbox_write_file', {'sandbox_id': sid, 'path': 'f', 'content': 'x'})
        self.assertFalse(self.manager.items[sid]['files'])

    async def test_loss_after_export_blocks_verifier_and_registration(self):
        sid = await self.produce()
        self.manager.after = lambda name, *_: setattr(self.binding, 'valid', False) if name == 'export' else None
        count = len(self.binding.reservations)
        with self.assertRaises(LeaseLost):
            await self.send(sid)
        self.assertEqual(len(self.binding.reservations), count)
        self.assertFalse(self.delivery.calls)


    async def test_loss_after_register_never_reports_success_or_reexecutes(self):
        sid = await self.produce()
        self.delivery.after = lambda: setattr(self.binding, 'valid', False)
        with self.assertRaises(LeaseLost):
            await self.send(sid)
        self.assertEqual(len(self.delivery.registrations), 1)
        before = len(self.manager.calls)
        with self.assertRaises(LeaseLost):
            await self.send(sid)
        self.assertEqual(len(self.manager.calls), before)

    async def test_cancellation_is_not_a_tool_result(self):
        sid = await self.create()
        self.manager.cancel_exec = True
        with self.assertRaises(asyncio.CancelledError):
            await self.executor.execute('sandbox_exec', {'sandbox_id': sid, 'command': 'generate-pdf'})
        self.assertFalse(self.delivery.calls)

    async def test_foreign_forged_or_changed_export_cannot_register(self):
        sid = await self.produce()
        artifact = await self.executor.export_file(sid, 'result.pdf', 'result.pdf')
        key = 'generated:' + 'a' * 64
        for content, proof in ((artifact.content, replace(artifact)), (artifact.content + b'x', artifact)):
            with self.assertRaisesRegex(GenerationError, 'invalid_generated_provenance'):
                await self.executor.send_file_content(content, artifact.filename, provenance=proof, logical_request_key=key)
        self.assertFalse(self.delivery.calls)

    async def test_registration_replays_same_key_without_guest_regeneration(self):
        sid = await self.produce()
        artifact = await self.executor.export_file(sid, 'result.pdf', 'result.pdf')
        await self.executor.execute('sandbox_destroy', {'sandbox_id': sid})
        before = len(self.manager.calls)
        for _ in range(2):
            result = json.loads(await self.executor.send_file_content(artifact.content, artifact.filename,
                provenance=artifact, logical_request_key='generated:' + 'a' * 64))
            self.assertFalse(result['message_accepted'])
        self.assertEqual(len(self.manager.calls), before)
        self.assertEqual(len(self.delivery.registrations), 1)

    async def test_route_shrunk_before_registration_rejects_saved_export(self):
        sid = await self.produce()
        artifact = await self.executor.export_file(sid, 'result.pdf', 'result.pdf')
        self.delivery.max_bytes = 1
        with self.assertRaises(GenerationError):
            await self.executor.send_file_content(artifact.content, artifact.filename,
                provenance=artifact, logical_request_key='generated:' + 'a' * 64)
        self.assertFalse(self.delivery.calls)


    @staticmethod
    def bad_zip():
        result = io.BytesIO()
        with zipfile.ZipFile(result, 'w') as archive:
            archive.writestr('../escape', 'x')
        return result.getvalue()

    async def test_host_paths_and_project_parameters_never_reach_io(self):
        sid = await self.create()
        for path in ('/etc/passwd', '/workspace/../x', '../x', 'a/../b', 'a\\b', 'a//b'):
            result = json.loads(await self.executor.execute('sandbox_read_file', {'sandbox_id': sid, 'path': path}))
            self.assertFalse(result['ok'])
        result = json.loads(await self.executor.execute('sandbox_create', {'owner': 'OTHER', 'project_id': 1}))
        self.assertFalse(result['ok'])
        self.assertFalse(any(c[0] in {'read', 'export'} for c in self.manager.calls))

    async def test_detached_job_and_untrusted_call_id_are_rejected(self):
        sid = await self.produce()
        result = json.loads(await self.executor.execute('sandbox_exec', {'sandbox_id': sid, 'command': 'x', 'background': True}))
        self.assertEqual(result['code'], 'detached_sandbox_job_disabled')
        result = await self.send(sid, call=None)
        self.assertEqual(result['code'], 'host_tool_call_required')
        self.assertFalse(self.delivery.calls)

    async def test_arbitrary_driver_error_is_redacted(self):
        self.manager.fail = 'create'
        result = await self.executor.execute('sandbox_create', {})
        self.assertNotIn('TEST_SECRET', result)
        self.assertNotIn('/host', result)
        self.assertFalse(json.loads(result)['ok'])


    async def test_total_export_bytes_are_bounded_before_next_read(self):
        sid = await self.produce()
        size = len(self.manager.items[sid]['files']['result.pdf'])
        self.executor.limits = replace(self.executor.limits, max_total_artifact_bytes=size)
        await self.executor.export_file(sid, 'result.pdf', 'first.pdf')
        before = sum(c[0] == 'export' for c in self.manager.calls)
        with self.assertRaisesRegex(GenerationError, 'source_artifact_bytes_limit'):
            await self.executor.export_file(sid, 'result.pdf', 'second.pdf')
        self.assertEqual(sum(c[0] == 'export' for c in self.manager.calls), before)

    async def test_neutral_handlers_require_exact_actual_input_source(self):
        await self.executor.readiness()
        inbound = InboundMessage(endpoint=self.binding.endpoint, actor=self.binding.actor, event_id=None,
            native_message_id='TEST_NATIVE', sender_display='', platform_role='',
            body=MessageBody((TextNode(0, 'make pdf'),)), mentions_bot=True,
            reply_to_native_message_id=None, occurred_at=100, reply_credential=None)
        async def db_call(fn, *args):
            return fn(*args)
        context = ToolContext(SimpleNamespace(context=SimpleNamespace(db_call=db_call)), event=None,
            inbound=inbound, platform=SimpleNamespace(account=inbound.endpoint.account, features=PlatformFeatures()),
            agent_executor=self.executor, capabilities=frozenset({'sandbox'}))
        registry = ToolRegistry(HANDLERS)
        # Production neutral catalog uses the same narrow gate, and defaults closed.
        catalog = neutral_registry_for(context)
        owned_names = {handler.definition['function']['name'] for handler in HANDLERS}
        enabled_names = {d['function']['name'] for d in catalog.definitions(context)}
        self.assertEqual(enabled_names & owned_names, owned_names)
        for closed in (replace(context, capabilities=frozenset()), replace(context, agent_executor=None)):
            self.assertFalse({d['function']['name'] for d in catalog.definitions(closed)} & owned_names)
            denied = await catalog.execute(TurnState(closed), 'sandbox_create', {})
            self.assertFalse(json.loads(denied)['ok'])
        self.executor.generation_enabled = False
        self.assertFalse({d['function']['name'] for d in catalog.definitions(context)} & owned_names)
        self.executor.generation_enabled = True
        names = {d['function']['name'] for d in registry.definitions(context)}
        self.assertIn('send_file_from_sandbox', names)
        self.assertNotIn('import_file_to_sandbox', names)
        self.assertNotIn('send_image_from_sandbox', names)
        definitions = registry.definitions(context)
        spec = next(d for d in definitions if d['function']['name'] == 'sandbox_exec')
        self.assertNotIn('background', spec['function']['parameters']['properties'])
        wrong = replace(context, inbound=replace(inbound, native_message_id='TEST_OTHER'))
        self.assertFalse(registry.definitions(wrong))
        denied = await registry.execute(TurnState(wrong), 'sandbox_create', {})
        self.assertFalse(json.loads(denied)['ok'])


class PureTests(unittest.TestCase):
    def test_positive_bounded_limits_only(self):
        for key in GenerationLimits.__dataclass_fields__:
            for value in (0, -1, True, None):
                with self.assertRaises(GenerationError):
                    GenerationLimits(**{key: value})

    def test_owner_partitions_accounts_environments_and_conversations(self):
        owners = set()
        for app in ('TEST_A', 'TEST_B'):
            for env in ('test', 'production'):
                account = AccountKey('qq-official', app, env)
                for group in ('GROUP_A', 'GROUP_B'):
                    for user in ('USER_A', 'USER_B'):
                        endpoint = EndpointKey(account, 'group', group)
                        actor = ActorKey(account, 'group_member', group, user)
                        binding = Binding(endpoint=endpoint, actor=actor)
                        executor = OwnedSandboxExecutor(manager=FakeManager(), binding=binding, delivery=Delivery())
                        self.assertLess(len(executor.owner), 200)
                        owners.add(executor.owner)
                endpoint = EndpointKey(account, 'private', 'USER_A')
                actor = ActorKey(account, 'c2c_user', '', 'USER_A')
                owners.add(OwnedSandboxExecutor(manager=FakeManager(), binding=Binding(endpoint=endpoint, actor=actor), delivery=Delivery()).owner)
        self.assertEqual(len(owners), 12)


    def test_clean_process_imports_no_nonebot_or_legacy_executor(self):
        code = "import sys;sys.path[:0]=%r;import src.plugins.ai_chat.application.owned_sandbox,src.plugins.ai_chat.tools.owned_sandbox;assert not any(n=='nonebot' or n.startswith('nonebot.') for n in sys.modules);assert 'src.plugins.ai_chat.agent_tools' not in sys.modules" % [str(ROOT)]
        result = subprocess.run([sys.executable, '-B', '-I', '-c', code], env={'PATH': os.environ.get('PATH', ''), 'PYTHON_DOTENV_DISABLED': '1'}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == '__main__':
    unittest.main()
