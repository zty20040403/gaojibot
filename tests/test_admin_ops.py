"""Read-only admin audit over actual consent/receipt records; no alternate approval."""
import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from src.plugins.ai_chat.admin import AdminServices
from tests.admin_session_fixture import ApprovedClient,register_admin
from tests.ops_action_fixture import OpsFixture
from tests import test_ops_execution as execution_fixture
from tests import test_admin_host_apis as auth_fixture


class AdminOpsTests(OpsFixture,unittest.IsolatedAsyncioTestCase):
    inbound = execution_fixture.OpsExecutionTests.inbound
    make_sender = execution_fixture.OpsExecutionTests.make_sender
    ensure_current = execution_fixture.OpsExecutionTests.ensure_current
    fast_sleep = execution_fixture.OpsExecutionTests.fast_sleep
    prepare = execution_fixture.OpsExecutionTests.prepare
    settle = execution_fixture.OpsExecutionTests.settle
    offer = execution_fixture.OpsExecutionTests.offer
    accept = execution_fixture.OpsExecutionTests.accept
    action = execution_fixture.OpsExecutionTests.action
    make_client = execution_fixture.OpsExecutionTests.make_client
    asyncTearDown = execution_fixture.OpsExecutionTests.asyncTearDown
    test_anonymous_and_member_cannot_read_any_scope = auth_fixture.AdminHostAPITests.test_anonymous_and_member_cannot_read_any_scope

    async def asyncSetUp(self):
        await execution_fixture.OpsExecutionTests.asyncSetUp(self)
        await self.offer(); self.accept()
        await self.execution.execute(self.proposal.action['action_id'])
        for index in range(2):
            proposal = self.request(request_id=f'TEST_CANCEL_{index}')
            with self.tx() as cursor:
                self.ops.close(cursor,proposal.action['action_id'],state='cancelled',error='ops_request_interrupted')
        self.app = FastAPI()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.client = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.client.aclose)
        self.path = '/bot-admin/api/v1/ops-actions'

    async def test_cursor_is_stable_at_equal_timestamps_and_fields_are_explicit(self):
        first = await self.client.get(self.path,headers={'X-Test-Login':'yes'},params={'limit':2,'scope_key':self.endpoint.scope.key})
        self.assertEqual(first.status_code,200,first.text)
        payload = first.json(); self.assertEqual(payload['counts'],{'succeeded':1,'cancelled':2})
        second = (await self.client.get(self.path,params={'limit':2,'before':payload['next_cursor']})).json()
        self.assertIsNone(second['next_cursor'])
        items = payload['items']+second['items']
        self.assertEqual(len({row['action_id'] for row in items}),3)
        success = next(row for row in items if row['state']=='succeeded')
        self.assertEqual(success['requester']['display'],'TEST_OWNER')
        self.assertEqual(success['approver'],success['requester']); self.assertEqual(success['notice_status'],'committed')
        self.assertEqual(success['parameters'],{'unit':'TEST_A.service'}); self.assertEqual(success['job'],f'job#{self.job.job_id}')
        for row in items:
            self.assertFalse({'actor_key','approved_actor_key','owner_grant_id','owner_revision','code_hash','binding_hash',
                'intent_hash','job_lease_owner','remote_contract','remote_version','parameters_json'} & row.keys())
        text = first.text+str(second)
        for secret in ('TEST_SECRET','control.example.test','TEST_PRIVATE_TOKEN','741852'):
            self.assertNotIn(secret,text)
        self.assertEqual(self.app.state.auth_messages,[])

    async def test_filters_and_validation_do_not_mutate_audit_or_offer_approval(self):
        await self.client.get(self.path,headers={'X-Test-Login':'yes'})
        for params,total in (({'state':'succeeded','plan':'restart-test'},1),({'scope_key':'TEST_OTHER'},0),({'plan':'TEST_OTHER'},0)):
            response = await self.client.get(self.path,params=params)
            self.assertEqual(response.status_code,200,response.text); self.assertEqual(len(response.json()['items']),total)
        for params in ({'limit':101},{'state':'TEST_BAD'},{'before':'0 OR TRUE'},{'before':'9'*19+':'+'a'*32}):
            self.assertEqual((await self.client.get(self.path,params=params)).status_code,422)
        before = self.query('SELECT * FROM ops_actions ORDER BY action_id')
        self.assertEqual((await self.client.post(self.path,json={'action':'approve'})).status_code,405)
        await self.client.get(self.path)
        self.assertEqual(before,self.query('SELECT * FROM ops_actions ORDER BY action_id'))
