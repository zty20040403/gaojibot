"""Logged-in admin changes commit controls, versions and audit atomically."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest

import httpx
from fastapi import FastAPI

from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.admin_api import programs
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramDenied
from tests.admin_session_fixture import ApprovedClient,register_admin
from tests import test_vm_units as fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class AdminProgramTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    path = '/bot-admin/api/v1/programs'

    async def asyncSetUp(self):
        await fixture.VMUnitHTTPTests.asyncSetUp(self)
        self.assertEqual((await self.client.post(self.url,json={})).status_code,200)
        self.service.runtime.agent_service = self.service
        self.notify = Mock()
        patcher = patch.object(self.lifecycle.keeper,'wake',self.notify)
        patcher.start(); self.addCleanup(patcher.stop)
        self.app = FastAPI()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            sandbox_manager=self.manager,official_runtime=lambda:self.service.runtime,
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.admin = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.admin.aclose)
        self.current = await self.lifecycle.credentials.authenticate(self.token_value)

    async def login(self):
        response = await self.admin.get(self.path,headers={'X-Test-Login':'yes'})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def settings(self, version=0, **change):
        return await self.admin.post(self.path+'/settings',headers={'If-Match':str(version)},json=
            dict(endpoint_key=self.endpoint.key,enabled=False,say_limit=3,wake_limit=2)|change)

    async def test_snapshot_and_control_commit_before_notify_without_guest_work_in_transaction(self):
        self.assertEqual((await self.login())['resource_version'],0)
        self.manager._guest_exec.reset_mock()
        snapshot = await self.admin.get(self.path,params={'endpoint_key':self.endpoint.key})
        self.assertEqual(snapshot.status_code,200,snapshot.text)
        self.assertEqual(snapshot.json()['snapshot']['vms'][0]['units'][0]['name'],'TEST.service')
        def notified():
            self.assertFalse(self.query('SELECT programs_enabled FROM conversation_settings')[0]['programs_enabled'])
        self.notify.side_effect = notified
        response = await self.settings()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['resource_version'],1)
        self.assertEqual(response.json()['synchronization'],'pending')
        self.notify.assert_called_once()
        self.manager._guest_exec.assert_not_awaited()
        with self.assertRaisesRegex(ProgramDenied,'program_disabled'):
            self.lifecycle.actions.reserve(self.current,generation=0,kind='say',program='TEST',request_id='TEST_DENIED',payload={'text':'TEST'})
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)
        self.assertEqual(self.app.state.auth_messages,[])

    async def test_per_program_disable_and_zero_limits_do_not_refund_existing_quota(self):
        await self.login()
        self.lifecycle.actions.reserve(self.current,generation=0,kind='say',program='TEST',request_id='TEST_1',payload={'text':'TEST'})
        response = await self.admin.post(self.path+'/control',headers={'If-Match':'0'},json=
            dict(endpoint_key=self.endpoint.key,program_name='TEST',enabled=False))
        self.assertEqual(response.status_code,200,response.text)
        with self.assertRaises(ProgramDenied):
            self.lifecycle.actions.reserve(self.current,generation=0,kind='say',program='TEST',request_id='TEST_2',payload={'text':'TEST'})
        self.lifecycle.actions.reserve(self.current,generation=0,kind='say',program='TEST_OTHER',request_id='TEST_3',payload={'text':'TEST'})
        self.assertEqual((await self.settings(1,enabled=True,say_limit=0,wake_limit=0)).status_code,200)
        self.assertEqual(self.query("SELECT used FROM program_quota WHERE kind='say'")[0]['used'],2)
        with self.assertRaisesRegex(ProgramDenied,'program_rate_limited'):
            self.lifecycle.actions.reserve(self.current,generation=0,kind='say',program='TEST_OTHER',request_id='TEST_4',payload={'text':'TEST'})

    async def test_parallel_version_has_one_winner_and_failure_rolls_everything_back(self):
        await self.login()
        results = await asyncio.gather(self.settings(enabled=True),self.settings(enabled=False))
        self.assertEqual(sorted(item.status_code for item in results),[200,409])
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)
        self.notify.assert_called_once()
        original = programs.policy
        calls = 0
        def fail(cursor,endpoint):
            nonlocal calls
            calls += 1
            if calls==2:
                raise RuntimeError('TEST after SQL mutation')
            return original(cursor,endpoint)
        before = self.query('SELECT * FROM conversation_settings')
        with patch.object(programs,'policy',side_effect=fail):
            self.assertEqual((await self.settings(1,say_limit=999)).status_code,503)
        self.assertEqual(self.query('SELECT * FROM conversation_settings'),before)
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)
        self.assertEqual(self.query("SELECT version FROM admin_resource_versions WHERE resource_key='programs'")[0]['version'],1)

    async def test_reenable_removes_override_and_offline_status_remains_readable(self):
        await self.login()
        for version,enabled in ((0,False),(1,True)):
            response = await self.admin.post(self.path+'/control',headers={'If-Match':str(version)},json=
                dict(endpoint_key=self.endpoint.key,program_name='TEST',enabled=enabled))
            self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(self.query('SELECT * FROM program_controls'))
        await self.lifecycle.close()
        self.manager._guest_exec.reset_mock()
        response = await self.admin.get(self.path,params={'endpoint_key':self.endpoint.key})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['snapshot']['vms'][0]['units'][0]['name'],'TEST.service')
        self.manager._guest_exec.assert_not_awaited()

    async def resume(self, *, version=0, fence=None):
        metadata = await self.manager._metadata(self.owner,'s123456')
        return await self.admin.post(self.path+'/resume',headers={'If-Match':str(version)},json=dict(
            endpoint_key=self.endpoint.key,sandbox_id='s123456',
            recovery_fence=fence or self.manager.program_state.recovery_fence(metadata)))

    def claim_resume(self):
        return self.service.runtime.jobs.claim_due('TEST_ADMIN_WORKER',kinds=(self.lifecycle.admin.kind,),now=self.now)[0]

    async def test_resume_is_audited_durable_and_retry_does_not_clear_a_new_pause(self):
        await self.login()
        await self.manager.program_state.stopping(self.owner,'s123456')
        await self.manager.program_state.stopped(self.owner,'s123456')
        response = await self.resume()
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue((await self.manager._metadata(self.owner,'s123456'))['programs']['paused'])
        self.assertEqual(self.query('SELECT action FROM admin_audit_log')[0]['action'],'program.resume')
        job = self.claim_resume()
        self.assertEqual(job.job_id,response.json()['job_id'])
        result = await self.lifecycle.admin.execute(job)
        self.assertTrue(result['reset_applied'])
        self.assertFalse((await self.manager._metadata(self.owner,'s123456'))['programs']['paused'])
        await self.manager.program_state.stopping(self.owner,'s123456')
        await self.manager.program_state.stopped(self.owner,'s123456')
        from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
        worker = DurableJobWorker(self.service.runtime.jobs,logger=self.lifecycle.logger,worker_id=job.lease_owner)
        worker.register(self.lifecycle.admin.kind,self.lifecycle.admin.execute)
        await worker._execute(job)  # Lost success ACK, same durable intent.
        self.assertTrue((await self.manager._metadata(self.owner,'s123456'))['programs']['paused'])
        snapshot = await self.admin.get(self.path,params={'endpoint_key':self.endpoint.key})
        self.assertEqual(snapshot.json()['snapshot']['recovery_jobs'][0]['status'],'succeeded')

    async def test_resume_rejects_stale_snapshot_cancelled_job_and_changed_route(self):
        await self.login()
        metadata = await self.manager._metadata(self.owner,'s123456')
        old = self.manager.program_state.recovery_fence(metadata)
        await self.manager.program_state.stopping(self.owner,'s123456')
        await self.manager.program_state.stopped(self.owner,'s123456')
        self.assertEqual((await self.resume(fence=old)).status_code,409)
        self.assertEqual((await self.resume()).status_code,200)
        job = self.claim_resume()
        self.service.runtime.jobs.cancel(job.job_id)
        with self.assertRaisesRegex(PermissionError,'program_admin_job_expired_or_replaced'):
            await self.lifecycle.admin.execute(job)
        self.assertTrue((await self.manager._metadata(self.owner,'s123456'))['programs']['paused'])
        self.assertEqual((await self.resume(version=1)).status_code,200)
        job = self.claim_resume()
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
        with self.assertRaisesRegex(PermissionError,'program_admin_route_replaced'):
            await self.lifecycle.admin.execute(job)
        self.assertTrue((await self.manager._metadata(self.owner,'s123456'))['programs']['paused'])

    async def test_resume_enqueue_failure_rolls_back_intent_version_and_audit(self):
        await self.login()
        original = self.lifecycle.admin.enqueue
        def fail(*args):
            original(*args)
            raise RuntimeError('TEST_AFTER_ENQUEUE')
        with patch.object(self.lifecycle.admin,'enqueue',side_effect=fail):
            self.assertEqual((await self.resume()).status_code,503)
        self.assertFalse(self.query('SELECT * FROM durable_jobs WHERE kind=?',(self.lifecycle.admin.kind,)))
        self.assertFalse(self.query('SELECT * FROM admin_audit_log'))
        self.assertFalse(self.query('SELECT * FROM admin_resource_versions'))

    async def test_auth_csrf_versions_and_input_are_required(self):
        body = dict(endpoint_key=self.endpoint.key,enabled=False,say_limit=1,wake_limit=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test') as client:
            self.assertEqual((await client.get(self.path)).status_code,401)
        await self.login()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',cookies=self.admin.cookies) as client:
            self.assertEqual((await client.post(self.path+'/settings',json=body,headers={'Origin':'https://test','If-Match':'0'})).status_code,403)
        self.assertEqual((await self.admin.post(self.path+'/settings',json=body)).status_code,428)
        self.assertEqual((await self.settings(endpoint_key='TEST_FOREIGN')).status_code,404)
        for change in ({'authorized':True},{'enabled':'true'},{'say_limit':-1},{'wake_limit':1001}):
            self.assertEqual((await self.settings(**change)).status_code,422)
        for name in ('../TEST','TEST\n','TEST'*30):
            response = await self.admin.post(self.path+'/control',headers={'If-Match':'0'},json=
                dict(endpoint_key=self.endpoint.key,program_name=name,enabled=False))
            self.assertEqual(response.status_code,422,response.text)
        self.assertFalse(self.query('SELECT * FROM admin_audit_log'))
