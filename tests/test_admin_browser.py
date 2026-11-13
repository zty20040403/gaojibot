"""Real login/CSRF, SQL intents and VM protocol doubles; no host browser or cookie values."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.admin_control import AdminControlStore
from tests.admin_session_fixture import ApprovedClient,register_admin
from tests import test_browser_mutations as fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class AdminBrowserTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    path = '/bot-admin/api/v1/conversations/browser'

    async def asyncSetUp(self):
        await fixture.BrowserMutationTests.asyncSetUp(self)
        self.lifecycle = SimpleNamespace(browser=self.browser,closed=False)
        self.runtime = SimpleNamespace(agent_service=SimpleNamespace(vm_lifecycle=self.lifecycle))
        self.app = FastAPI()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            official_runtime=lambda:self.runtime,
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.admin = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.admin.aclose)

    async def guest(self,sid,path,args,**kwargs):
        if 'scope=' in args[-1]:
            return (0,json.dumps(dict(domains=['test.example'],truncated=False,boot=fixture.BOOT)).encode(),b'')
        return await fixture.BrowserMutationTests.guest(self,sid,path,args,**kwargs)

    async def login(self):
        response = await self.admin.get(self.path,params={'endpoint_key':self.endpoint.key},headers={'X-Test-Login':'yes'})
        self.assertEqual(response.status_code,200,response.text)
        self.snapshot = response.json()['snapshot']
        return response.json()

    async def clear(self,version=0,**changes):
        return await self.admin.post(self.path+'/clear',headers={'If-Match':str(version)},
            json=dict(endpoint_key=self.endpoint.key,fence=self.snapshot['fence'],confirmation='clear')|changes)

    async def test_domains_and_clear_commit_intent_before_guest_without_qq_otp(self):
        response = await self.login()
        self.assertEqual(response['snapshot']['domains'],['test.example'])
        self.assertNotIn(fixture.BOOT,json.dumps(response))
        self.assertNotIn('cookie',json.dumps(response))
        async def perform(value):
            self.assertEqual(value['action'],'clear')
            rows = self.query("SELECT action,after_json FROM admin_audit_log WHERE resource_key='browser'")
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['action'],'browser.clear.requested')
            self.assertEqual(json.loads(rows[0]['after_json'])['state'],'unknown')
            return {'cleared':True}
        self.perform.side_effect = perform
        response = await self.clear()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'cleared')
        self.assertEqual((await self.clear()).status_code,409)
        self.perform.assert_awaited_once()
        recent = (await self.admin.get(self.path,params={'endpoint_key':self.endpoint.key})).json()['recent']
        self.assertEqual(recent[0]['state'],'cleared')
        self.assertEqual(recent[0]['operation_id'],response.json()['operation_id'])
        self.assertEqual(self.app.state.auth_messages,[])

    async def test_login_csrf_version_and_explicit_confirmation_are_required(self):
        self.assertEqual((await self.admin.get(self.path)).status_code,401)
        await self.login()
        body = dict(endpoint_key=self.endpoint.key,fence=self.snapshot['fence'],confirmation='clear')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',cookies=self.admin.cookies) as client:
            response = await client.post(self.path+'/clear',headers={'Origin':'https://test','If-Match':'0'},json=body)
            self.assertEqual(response.status_code,403)
        self.assertEqual((await self.admin.post(self.path+'/clear',json=body)).status_code,428)
        for changes in ({'confirmation':'TEST_NO'},{'scope_key':'TEST_INJECT'},{'fence':'TEST_BAD'}):
            self.assertEqual((await self.clear(**changes)).status_code,422)
        self.perform.assert_not_awaited(); self.assertFalse(self.query('SELECT * FROM admin_audit_log'))

    async def test_changed_snapshot_and_concurrent_versions_cannot_clear_twice(self):
        await self.login()
        result = await self.clear(fence='0'*64)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'not_applied')
        self.assertEqual(result.json()['error_code'],'browser_snapshot_changed')
        self.perform.assert_not_awaited()
        results = await asyncio.gather(self.clear(1),self.clear(1))
        self.assertEqual(sorted(row.status_code for row in results),[200,409])
        self.perform.assert_awaited_once()

    async def test_audit_failure_rolls_back_version_and_never_calls_guest(self):
        await self.login()
        original = AdminControlStore.mutate_transaction
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('TEST after audit SQL')
        self.manager._guest_exec.reset_mock()
        with patch.object(AdminControlStore,'mutate_transaction',side_effect=fail,autospec=True):
            self.assertEqual((await self.clear()).status_code,503)
        self.assertFalse(self.query('SELECT * FROM admin_audit_log'))
        self.assertFalse(self.query("SELECT * FROM admin_resource_versions WHERE resource_key='browser'"))
        self.manager._guest_exec.assert_not_awaited()

    async def test_revoked_session_cancels_inflight_wait_and_preserves_unknown_result(self):
        await self.login()
        entered,ended = asyncio.Event(),asyncio.Event()
        async def waiting(value):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()
        self.perform.side_effect = waiting
        pending = asyncio.create_task(self.clear())
        await asyncio.wait_for(entered.wait(),3)
        self.app.state.mobile.store.logout(self.admin.cookies['gaoji_session'])
        result = await asyncio.wait_for(pending,3)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'unknown')
        self.assertTrue(ended.is_set()); self.perform.assert_awaited_once()
        self.assertEqual((await self.admin.get(self.path)).status_code,401)

    async def test_http_cancellation_never_replays_clear_and_offline_history_is_retained(self):
        await self.login()
        entered = asyncio.Event()
        async def waiting(value):
            entered.set(); await asyncio.Future()
        self.perform.side_effect = waiting
        pending = asyncio.create_task(self.clear())
        await asyncio.wait_for(entered.wait(),3)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual((await self.clear()).status_code,409)
        self.lifecycle.closed = True
        result = (await self.admin.get(self.path,params={'endpoint_key':self.endpoint.key})).json()
        self.assertFalse(result['available']); self.assertIsNone(result['snapshot'])
        self.assertEqual(result['recent'][0]['state'],'unknown')
        self.perform.assert_awaited_once()
