"""Production agent/gateway composition; real DB, files and HTTP, fake guest hop."""
from dataclasses import replace
from functools import partial
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
import httpx2

from src.bot_storage.async_calls import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.background_tools import workspace_owner
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.host_apis import HostAPI
from src.plugins.ai_chat.host_api_transport import PinnedAPITransport,forward
from src.plugins.ai_chat.model_catalog import ModelCatalog,ModelProfile
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests import test_host_github as github_fixture, test_vm_credentials as credential_fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.test_codex_gateway import free_port
from tests.test_host_api_transport import Stream


class VMLifecycleTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await credential_fixture.VMCredentialTests.asyncSetUp(self)
        self.owner = workspace_owner(self.endpoint)
        self.manager.backend = 'vm'
        self.manager._all_metadata = AsyncMock(return_value=[dict(id='s123456',owner_ref=self.owner)])
        key = self.root/'TEST_UPSTREAM'; key.write_text('TEST_HOST_SECRET'); key.chmod(0o600)
        api = HostAPI.parse('test',dict(base_url='https://api.example.test',secret_file=str(key),
            headers={'Authorization':'Bearer {secret}'},per_hour=10))
        jobs = DurableJobStore(self.database); self.addCleanup(jobs.close)
        calls = DatabaseCalls(); self.addAsyncCleanup(calls.close)
        runtime = SimpleNamespace(store=SimpleNamespace(transaction=self.tx,admitted=lambda *_:True,clock=lambda:self.now),
            jobs=jobs,_calls=calls,turn_locks={})
        profile = ModelProfile('TEST_MODEL','test','openai-chat','TEST_MODEL','https://example.invalid',api_key_required=False)
        app = SimpleNamespace(database=self.database,settings=replace(settings,background_gateway_port=free_port()),
            sandbox_manager=self.manager,logger=logging.getLogger(__name__),
            model_preferences=SimpleNamespace(get_explicit=lambda _:None),model_catalog=ModelCatalog({'TEST_MODEL':profile},default_profile='TEST_MODEL'),
            official_host=SimpleNamespace(config=HostConfiguration(self.account,apis={'test':api})))
        self.service = OfficialBackgroundAgents(runtime,self.token,app)
        self.addAsyncCleanup(self.service.close)
        self.seen = []
        def wire(request):
            self.seen.append(request)
            return httpx2.Response(200,stream=Stream(b'TEST_OK'))
        self.service.api_proxy.transport = partial(forward,transport_factory=lambda url:PinnedAPITransport(url,
            resolver=AsyncMock(return_value=('8.8.8.8',)),transport=httpx2.MockTransport(wire)))
        await self.service.start()
        self.lifecycle = self.service.vm_lifecycle
        await self.lifecycle.restoring
        self.token_value = (self.guest/'token').read_text()
        self.client = httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+self.token_value})
        self.addAsyncCleanup(self.client.aclose)
        self.url = f'http://127.0.0.1:{self.service.gateway.port}/api/test/value'

    async def test_restart_restores_running_vm_and_api_works_without_any_job(self):
        self.assertIs(self.manager.conversation_lifecycle,self.lifecycle)
        self.assertEqual(self.service.gateway.channels,{})
        response = await self.client.get(self.url)
        self.assertEqual((response.status_code,response.content),(200,b'TEST_OK'))
        row = self.query('SELECT * FROM host_api_calls')[0]
        self.assertEqual(row['scope_key'],self.endpoint.scope.key)
        self.assertEqual(row['caller_ref'],'vm:s123456:1')
        self.assertEqual(self.seen[0].headers['Authorization'],'Bearer TEST_HOST_SECRET')
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',
            (self.endpoint.scope.key,))
        self.assertEqual((await self.client.get(self.url)).status_code,200)

    async def test_stopping_revokes_persisted_and_inflight_authority(self):
        scope = await self.lifecycle.api.authorize(self.token_value)
        await self.lifecycle.stopping(self.owner,'s123456')
        self.assertTrue(scope.revoked)
        self.assertEqual((await self.client.get(self.url)).status_code,401)
        self.assertEqual(self.seen,[])
        # An unregistered/removed VM must still be stoppable.
        await self.lifecycle.stopping('qqo:conversation:TEST_RETIRED','s654321')

    async def test_boot_rotation_closes_old_scope_without_revalidating_old_token(self):
        scope = await self.lifecycle.api.authorize(self.token_value)
        self.boot.write_text('00000000-0000-4000-8000-000000000002')
        current = await self.lifecycle.ready(self.owner,'s123456')
        self.assertTrue(scope.revoked)
        self.assertEqual(current.token_generation,2)
        self.assertEqual((await self.client.get(self.url)).status_code,401)
        self.client.headers['Authorization'] = 'Bearer '+(self.guest/'token').read_text()
        self.assertEqual((await self.client.get(self.url)).status_code,200)

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
        await github_fixture.GitHubVMTests.asyncSetUp(self)
        self.hook = self.manager.conversation_lifecycle = SimpleNamespace(ready=AsyncMock(),stopping=AsyncMock())
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
