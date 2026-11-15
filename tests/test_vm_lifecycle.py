"""Production agent/gateway composition; real DB, files and HTTP, fake guest hop."""
from dataclasses import replace
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from src.bot_storage.async_calls import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.background_tools import workspace_owner
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.model_catalog import ModelCatalog,ModelProfile
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
from tests import test_vm_credentials as credential_fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.test_codex_gateway import free_port


class VMLifecycleTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await credential_fixture.VMCredentialTests.asyncSetUp(self)
        self.owner = workspace_owner(self.endpoint)
        self.manager.backend = 'vm'
        self.manager._all_metadata = AsyncMock(wraps=self.manager._all_metadata)
        jobs = DurableJobStore(self.database); self.addCleanup(jobs.close)
        calls = DatabaseCalls(); self.addAsyncCleanup(calls.close)
        runtime = SimpleNamespace(store=SimpleNamespace(transaction=self.tx,admitted=lambda *_:True,clock=lambda:self.now),
            jobs=jobs,_calls=calls,turn_locks={})
        profile = ModelProfile('TEST_MODEL','test','openai-chat','TEST_MODEL','https://example.invalid',api_key_required=False)
        app = SimpleNamespace(database=self.database,settings=replace(settings,background_gateway_port=free_port()),
            sandbox_manager=self.manager,logger=logging.getLogger(__name__),
            model_preferences=SimpleNamespace(get_explicit=lambda _:None),model_catalog=ModelCatalog({'TEST_MODEL':profile},default_profile='TEST_MODEL'),
            official_host=SimpleNamespace(config=HostConfiguration(self.account)))
        self.service = OfficialBackgroundAgents(runtime,self.token,app)
        self.addAsyncCleanup(self.service.close)
        await self.service.start()
        self.lifecycle = self.service.vm_lifecycle
        await self.lifecycle.restoring
        self.token_value = (self.guest/'token').read_text()
        self.client = httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+self.token_value})
        self.addAsyncCleanup(self.client.aclose)
        self.url = f'http://127.0.0.1:{self.service.gateway.port}/vm/commands'

    async def test_restart_restores_running_vm_and_api_works_without_any_job(self):
        self.assertIs(self.manager.conversation_lifecycle,self.lifecycle)
        self.assertEqual(self.service.gateway.channels,{})
        self.assertNotEqual((await self.client.get(self.url)).status_code,401)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',
            (self.endpoint.scope.key,))
        self.assertNotEqual((await self.client.get(self.url)).status_code,401)

    async def test_stopping_revokes_persisted_and_inflight_authority(self):
        scope = await self.lifecycle.api.authorize(self.token_value)
        await self.lifecycle.stopping(self.owner,'s123456')
        self.assertTrue(scope.revoked)
        self.assertEqual((await self.client.get(self.url)).status_code,401)
        # An unregistered/removed VM must still be stoppable.
        await self.lifecycle.stopping('qqo:conversation:TEST_RETIRED','s654321')

    async def test_idle_stop_keeps_scopes_and_programs_until_commands_are_finished(self):
        from src.plugins.ai_chat.adapters.qq_official.background_commands import BackgroundCommands
        await self.lifecycle.commands.close()  # This case tests the atomic idle gate, not guest execution.
        commands = BackgroundCommands(self.lifecycle.store)
        current = self.lifecycle.store.authenticate(self.token_value)
        row,_ = commands.reserve(current,generation=(0,0),program='TEST_PROGRAM',request_id='TEST_COMMAND',argv=['true'])
        scope = await self.lifecycle.api.authorize(self.token_value)
        self.assertFalse(await self.lifecycle.stopping(self.owner,'s123456',automatic=True))
        self.assertFalse(scope.revoked)
        state = (await self.manager._metadata(self.owner,'s123456'))['programs']
        self.assertFalse(state.get('planned_stop'))
        commands.cancel(current,row['command_id'])
        self.assertTrue(await self.lifecycle.stopping(self.owner,'s123456',automatic=True))
        self.assertTrue(scope.revoked)

    async def test_boot_rotation_closes_old_scope_without_revalidating_old_token(self):
        scope = await self.lifecycle.api.authorize(self.token_value)
        self.boot.write_text('00000000-0000-4000-8000-000000000002')
        current = await self.lifecycle.ready(self.owner,'s123456')
        self.assertTrue(scope.revoked)
        self.assertEqual(current.token_generation,2)
        self.assertEqual((await self.client.get(self.url)).status_code,401)
        self.client.headers['Authorization'] = 'Bearer '+(self.guest/'token').read_text()
        self.assertNotEqual((await self.client.get(self.url)).status_code,401)

    async def test_stale_stop_cannot_revoke_replacement_vm(self):
        current = await self.calls(self.lifecycle.store.issue,self.endpoint,'s654321',credential_fixture.BOOT,expected_route=(1,1))
        await self.lifecycle.stopping(self.owner,'s123456')
        self.assertEqual(self.lifecycle.store.authenticate(current.token),current.source)

    async def test_close_detaches_only_its_own_hook_and_does_not_erase_boot_hash(self):
        replacement = object()
        self.manager.conversation_lifecycle = replacement
        await self.lifecycle.close()
        self.assertIs(self.manager.conversation_lifecycle,replacement)
        self.assertIsNone(self.query('SELECT revoked_at FROM qq_official_vm_sessions')[0]['revoked_at'])
        self.assertEqual((await self.client.get(self.url)).status_code,401)


class VMManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.manager = VmSandboxManager(image='/TEST_base.qcow2',root=directory.name)
        self.manager._metadata = AsyncMock(return_value={'id':'s123456'})
        self.manager._all_metadata = AsyncMock(return_value=[])
        self.manager._defined = AsyncMock(return_value=True)
        self.manager._running = AsyncMock(return_value=False)
        self.manager._virsh,self.manager._wait_guest = AsyncMock(),AsyncMock()
        self.manager._save_metadata = Mock()
        self.manager._domain_xml = Mock(return_value='<domain/>')
        self.manager._guest_exec = AsyncMock(return_value=(0,b'',b''))
        self.hook = self.manager.conversation_lifecycle = SimpleNamespace(ready=AsyncMock(),stopping=AsyncMock(),starting=AsyncMock(),failed=AsyncMock(),stopped=AsyncMock())
        self.owner = 'qqo:conversation:TEST_GROUP'

    async def test_boot_and_already_running_call_ready_but_template_does_not(self):
        for running in (False,True):
            self.manager._running.return_value = running
            self.hook.ready.reset_mock()
            await self.manager.start_owned(self.owner,'s123456')
            self.hook.ready.assert_awaited_once_with(self.owner,'s123456')
        self.hook.ready.reset_mock()
        await self.manager.start_owned('qqo:template','s123456')
        self.hook.ready.assert_not_awaited()

    async def test_cleanup_honors_atomic_command_hold_before_guest_or_disk_io(self):
        self.hook.stopping.return_value = False
        self.manager._guest_exec.reset_mock(); self.manager._virsh.reset_mock()
        await self.manager.stop_owned(self.owner,'s123456',if_idle=True)
        await self.manager.destroy(self.owner,'s123456',if_unused=True)
        self.assertEqual(self.hook.stopping.await_count,2)
        self.manager._guest_exec.assert_not_awaited(); self.manager._virsh.assert_not_awaited()

    async def test_cleanup_rechecks_resident_before_revoking_or_touching_guest(self):
        original = self.manager._metadata.return_value
        self.manager._metadata.return_value = dict(original,programs=dict(retained=True,wanted=True))
        await self.manager.stop_owned(self.owner,'s123456',if_idle=True)
        await self.manager.destroy(self.owner,'s123456',if_unused=True)
        self.hook.stopping.assert_not_awaited()
        self.manager._virsh.assert_not_awaited()

    async def test_create_failure_revokes_before_destroying_failed_vm(self):
        self.manager.image = self.manager.root/'TEST_base.qcow2'
        self.manager.image.touch()
        self.manager._checked = AsyncMock()
        self.manager._run = AsyncMock(return_value=(0,b'',b''))
        self.hook.ready.side_effect = PermissionError('TEST_DENIED')
        with self.assertRaises(PermissionError):
            await self.manager.create(self.owner)
        self.hook.stopping.assert_awaited_once()
        self.assertEqual(list(self.manager.root.glob('s??????')),[])

    async def test_stop_revokes_before_hypervisor_and_keeps_io_lock(self):
        self.manager._running.return_value = True
        self.manager._agent = AsyncMock(return_value=1)
        seen = []
        async def stopping(owner,sid):
            self.assertTrue(self.manager._exec_locks[sid].locked())
            seen.append('revoke')
        async def virsh(*args,**kwargs):
            seen.append(args[0])
        self.hook.stopping.side_effect = stopping
        self.manager._virsh.side_effect = virsh
        await self.manager.stop_owned(self.owner,'s123456')
        self.assertEqual(seen,['revoke','destroy'])

    async def test_start_owned_restarts_vm_when_qga_is_broken(self):
        from src.plugins.ai_chat.sandbox import SandboxError, SandboxExecutionActivity
        from src.plugins.ai_chat.vm_sandbox import GuestAgentBroken

        self.manager._running.return_value = True
        calls = []
        broken = True

        async def virsh(*args, timeout=30):
            nonlocal broken
            calls.append(args)
            if args[0] == 'qemu-agent-command':
                if broken:
                    raise SandboxError('error: QEMU guest agent is not available due to an error')
                return '{"return":{}}'
            if args[0] == 'destroy':
                broken = False
                return ''
            if args[0] == 'start':
                return ''
            raise AssertionError(f'unexpected virsh call: {args}')

        self.manager._virsh.side_effect = virsh
        self.manager._wait_guest = self.manager.__class__._wait_guest.__get__(self.manager)
        self.manager._agent = self.manager.__class__._agent.__get__(self.manager)
        self.manager._defined = AsyncMock(return_value=True)
        await self.manager.start_owned(self.owner, 's123456')
        self.assertIn(('destroy', 'gaoji-vm-s123456'), calls)
        self.assertIn(('start', 'gaoji-vm-s123456'), calls)
        self.hook.ready.assert_awaited_once_with(self.owner, 's123456')

        # If an execution is active, do not force destroy on broken QGA.
        broken = True
        self.manager._active_execs['act1'] = SandboxExecutionActivity('act1', 's123456', self.owner, 'cmd', 1)
        try:
            with self.assertRaises(GuestAgentBroken):
                await self.manager.start_owned(self.owner, 's123456')
        finally:
            self.manager._active_execs.clear()

    async def test_stop_owned_forces_destroy_when_qga_is_broken(self):
        from src.plugins.ai_chat.vm_sandbox import GuestAgentBroken

        self.manager._running.return_value = True
        self.manager._guest_exec.side_effect = GuestAgentBroken('QEMU guest agent is not available due to an error')
        self.manager._virsh.reset_mock()
        await self.manager.stop_owned(self.owner, 's123456')
        self.manager._virsh.assert_awaited_once_with('destroy', 'gaoji-vm-s123456')
        self.hook.stopped.assert_awaited_once_with(self.owner, 's123456')
