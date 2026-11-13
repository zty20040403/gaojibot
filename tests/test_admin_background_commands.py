"""Admin login/CSRF, account-serialized intent, and no guest RPC before commit."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import httpx
from fastapi import FastAPI

from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.admin_api import background_commands as routes
from src.plugins.ai_chat.platform_types import EndpointKey
from tests.admin_session_fixture import ApprovedClient,register_admin
from tests import test_command_runner as fixture
from tests.test_command_execution import report
from tests.qq_official_outbox_fixture import OutboxFixture


class AdminCommandTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    path = '/bot-admin/api/v1/background-commands'
    reserve = fixture.CommandRunnerTests.reserve
    stored = fixture.CommandRunnerTests.stored

    async def asyncSetUp(self):
        await fixture.CommandRunnerTests.asyncSetUp(self)
        self.service.runtime.agent_service = self.service
        self.app = FastAPI()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            official_runtime=lambda:self.service.runtime,
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.admin = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.admin.aclose)
        self.notify = Mock()
        patcher = patch.object(self.runner.changed,'set',self.notify)
        patcher.start(); self.addCleanup(patcher.stop)

    async def login(self):
        result = await self.admin.get(self.path,headers={'X-Test-Login':'yes'})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    async def settings(self, version=0, **changes):
        return await self.admin.put(self.path+'/settings',headers={'If-Match':str(version)},
            json=dict(endpoint_key=self.endpoint.key,hours=2)|changes)

    async def cancel(self, key, version=0, **changes):
        return await self.admin.post(self.path+'/'+key+'/cancel',headers={'If-Match':str(version)},
            json=dict(endpoint_key=self.endpoint.key)|changes)

    async def test_physical_setting_preserves_controls_and_rejects_stale_version(self):
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation,program_say_limit) VALUES (?,TRUE,7,3)',(self.endpoint.scope.key,))
        initial = await self.login()
        self.assertEqual(initial['endpoints'][0]['hours'],6)
        self.manager._guest_exec.reset_mock()
        response = await self.settings()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['applies_to'],'new_jobs')
        self.assertEqual(self.query('SELECT stopped,stop_generation,program_say_limit,agent_timeout_hours FROM conversation_settings'),
            [dict(stopped=True,stop_generation=7,program_say_limit=3,agent_timeout_hours=2)])
        self.assertEqual((await self.settings(hours=3)).status_code,409)
        self.manager._guest_exec.assert_not_awaited()
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)
        self.assertEqual(self.app.state.auth_messages,[])
        self.assertEqual((await self.admin.get(self.path)).json()['endpoints'][0]['hours'],2)

    async def test_cancel_only_selected_command_keeps_running_slot_until_guest_confirms(self):
        await self.login()
        key,other = self.reserve(),self.reserve('TEST_OTHER')
        await self.runner.visit(key)
        self.manager._guest_exec.reset_mock()
        self.notify.side_effect = lambda:self.assertTrue(self.stored(key)['cancel_requested'])
        response = await self.cancel(key)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'stopping')
        self.assertIsNone(self.stored(key)['finished_at'])
        self.assertFalse(self.stored(other)['cancel_requested'])
        self.manager._guest_exec.assert_not_awaited(); self.notify.assert_called_once()
        await self.runner.visit(key)
        self.assertEqual(self.stored(key)['state'],'cancelled')
        self.assertIsNotNone(self.stored(key)['finished_at'])
        self.assertEqual(self.requests[-1]['op'],'stop')

    async def test_scope_mismatch_and_invalid_inputs_have_no_effect(self):
        await self.login(); key = self.reserve()
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        self.assertEqual((await self.cancel(key,endpoint_key=other.key)).status_code,404)
        self.assertEqual((await self.cancel('TEST_MISSING')).status_code,404)
        for hours in (0,7,True,1.5,'3'):
            self.assertEqual((await self.settings(hours=hours)).status_code,422)
        self.assertEqual((await self.settings(scope_key='TEST_INJECT')).status_code,422)
        self.assertEqual((await self.settings(endpoint_key='TEST_BAD')).status_code,404)
        self.assertFalse(self.stored(key)['cancel_requested'])
        self.assertFalse(self.query('SELECT * FROM admin_audit_log'))
        self.assertEqual((await self.admin.get(self.path,params={'endpoint_key':other.key})).json()['items'],[])

    async def test_concurrent_version_and_transaction_failure_do_not_partially_commit(self):
        await self.login()
        results = await asyncio.gather(self.settings(hours=1),self.settings(hours=4))
        self.assertEqual(sorted(row.status_code for row in results),[200,409])
        key = self.reserve()
        original = routes.cancel_commands
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('TEST fail after SQL')
        self.notify.reset_mock()
        with patch.object(routes,'cancel_commands',side_effect=fail):
            self.assertEqual((await self.cancel(key,1)).status_code,503)
        self.assertFalse(self.stored(key)['cancel_requested']); self.notify.assert_not_called()
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)
        self.assertEqual((await self.admin.get(self.path)).json()['resource_version'],1)

    async def test_login_csrf_and_version_required_without_qq_otp(self):
        self.assertEqual((await self.admin.get(self.path)).status_code,401)
        await self.login()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',cookies=self.admin.cookies) as client:
            response = await client.put(self.path+'/settings',headers={'If-Match':'0','Origin':'https://test'},
                json=dict(endpoint_key=self.endpoint.key,hours=1))
            self.assertEqual(response.status_code,403)
        response = await self.admin.put(self.path+'/settings',json=dict(endpoint_key=self.endpoint.key,hours=1))
        self.assertEqual(response.status_code,428)
        self.assertFalse(self.query('SELECT * FROM admin_audit_log'))

    async def test_read_after_runtime_close_keeps_completion_and_notification_separate(self):
        await self.login(); key = self.reserve(); await self.runner.visit(key)
        self.guest_reports[key] = report(state='completed',stopped=True,exit_code=0,elapsed_seconds=2)
        await self.runner.visit(key)
        self.query("UPDATE qq_official_program_actions SET state='failed',error_code='TEST_DELIVERY_DENIED'")
        await self.lifecycle.close()
        self.manager._guest_exec.reset_mock()
        self.assertEqual((await self.admin.get(self.path)).json()['items'],[])
        response = await self.admin.get(self.path,params={'active':'false'})
        item = response.json()['items'][0]
        self.assertEqual((item['state'],item['notification']['state']),('completed','failed'))
        self.assertEqual(item['notification']['error_code'],'TEST_DELIVERY_DENIED')
        for hidden in ('argv_json','token_hash','boot_id','intent_digest'):
            self.assertNotIn(hidden,item)
        self.manager._guest_exec.assert_not_awaited()
