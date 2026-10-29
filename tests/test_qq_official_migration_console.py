"""Secure admin routes through real sessions, mapping store and Journal."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
import nonebot
nonebot.init()

from src.bot_security.service import MobileAuthorization
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.admin import AdminServices,register_admin
from src.plugins.ai_chat.admin_security import SESSION_COOKIE
from src.plugins.ai_chat.adapters.qq_official.migration_console import MigrationConsoleService
from src.plugins.ai_chat.adapters.qq_official.migration_mappings import mapping_request
from src.plugins.ai_chat.adapters.qq_official.migration_preview import Selection
from tests.qq_official_migration_journal_fixture import JournalFixture


class MigrationConsoleTests(JournalFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=DatabaseCalls()
        self.ports.control=self.mapping_authority.control
        self.service=MigrationConsoleService(authority=self.mapping_authority,mappings=self.mapping_store,
            transaction=self.tx,calls=self.calls,journal=self.migration,
            baseline_source=lambda c,s,n:self.baseline_source,clock=lambda:self.now)
        self.app=FastAPI()
        self.getter=lambda:self.service
        register_admin(self.app,AdminServices(version='test',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test'),
            mobile_authorization=MobileAuthorization(self.mapping_security,sender=None,bot_selector=lambda:None),
            official_migration_service=lambda:self.getter()))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',
            headers={'Origin':'https://test','x-csrf-token':self.mapping_csrf})
        self.client.cookies.set(SESSION_COOKIE,self.mapping_cookie)
        self.path='/bot-admin/api/v1/qq-official/migrations'

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.calls.close()

    def plan_request(self):
        return dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,target_endpoint=self.endpoint.key,
            items=[dict(kind='reminder',key='1')],deadline=self.now+500)

    async def create(self):
        response=await self.client.post(self.path,json=self.plan_request())
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def test_real_plan_review_digest_freeze_and_drain(self):
        plan=await self.create()
        key=plan['migration_id'];preview=plan['preview_digest']
        read=await self.client.get(self.path+'/'+key)
        self.assertEqual(read.status_code,200,read.text)
        self.assertEqual(read.json()['preview_digest'],preview)
        self.assertEqual(read.headers['cache-control'],'no-store')
        response=await self.client.post(self.path+'/'+key+'/actions',json=dict(action='freeze',preview_digest='0'*64))
        self.assertEqual(response.status_code,409)
        self.assertEqual('planned',self.query('SELECT phase FROM migration_routes')[0][0])
        for action in ('freeze','drain'):
            response=await self.client.post(self.path+'/'+key+'/actions',json=dict(action=action,preview_digest=preview))
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual('draining',self.query('SELECT phase FROM migration_routes')[0][0])
        actions=[r[0] for r in self.query('SELECT action FROM migration_control_audit ORDER BY audit_id')]
        self.assertEqual(['mapping_put','plan','freeze','drain'],actions)

    async def test_csrf_origin_member_and_missing_session_do_not_change_mapping(self):
        body=mapping_request(selection=Selection(self.source,self.source_scope,self.endpoint),
            source_actor=self.source_actor,target_actor=self.actor,evidence_inbox_id=self.mapping_inbox,
            expected_revision=1,expires_at=self.now+1000)
        for headers in ({'x-csrf-token':''},{'x-csrf-token':'WRONG'},{'Origin':'https://evil.test'}):
            response=await self.client.post(self.path+'/mappings',json=body,headers=headers)
            self.assertEqual(response.status_code,403,response.text)
        self.query("UPDATE admin_accounts SET role='member'")
        self.assertEqual((await self.client.post(self.path+'/mappings',json=body)).status_code,403)
        self.query("UPDATE admin_accounts SET role='admin'")
        self.client.cookies.clear()
        self.assertEqual((await self.client.post(self.path+'/mappings',json=body)).status_code,401)
        self.assertEqual(1,self.query('SELECT revision FROM migration_actor_mappings')[0][0])

    async def test_strict_payload_and_default_disabled_service(self):
        for body in ({**self.plan_request(),'baseline_source':'forged'}, {**self.plan_request(),'deadline':True}):
            self.assertEqual((await self.client.post(self.path,json=body)).status_code,422)
        self.getter=lambda:None
        self.assertEqual((await self.client.post(self.path,json=self.plan_request())).status_code,404)
        self.assertEqual(0,self.query('SELECT count(*) FROM migration_journals')[0][0])

    async def test_mapping_put_and_revoke_real_versions_no_query_overrides(self):
        body=mapping_request(selection=Selection(self.source,self.source_scope,self.endpoint),
            source_actor=self.source_actor,target_actor=self.actor,evidence_inbox_id=self.mapping_inbox,
            expected_revision=1,expires_at=self.now+1000)
        response=await self.client.post(self.path+'/mappings',json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['revision'],2)
        self.assertEqual((await self.client.post(self.path+'/mappings?actor=override',json=body)).status_code,400)
        key=response.json()['mapping_id']
        response=await self.client.post(self.path+'/mappings/'+key+'/revoke',json={'expected_revision':2})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((response.json()['revision'],response.json()['state']),(3,'revoked'))

    async def test_cannot_change_selection_or_skip_verified_receipts_via_actions(self):
        plan=await self.create();path=self.path+'/'+plan['migration_id']+'/actions'
        response=await self.client.post(path,json={'action':'complete','preview_digest':plan['preview_digest'],'target_endpoint':'replacement'})
        self.assertEqual(response.status_code,422)
        for action in ('complete','switch','verify'):
            response=await self.client.post(path,json=dict(action=action,preview_digest=plan['preview_digest']))
            self.assertEqual(response.status_code,409)
        self.assertEqual('planned',self.query('SELECT phase FROM migration_routes')[0][0])

    async def test_read_rechecks_revocation_after_snapshot_before_response(self):
        plan=await self.create()
        original=self.mapping_security.session
        calls=0
        def revoke_after_snapshot(*args,**kwargs):
            nonlocal calls
            calls+=1
            # secure_route authenticates once; service checks before and after its snapshot.
            if calls==3:self.mapping_security.logout(self.mapping_cookie)
            return original(*args,**kwargs)
        with patch.object(self.mapping_security,'session',side_effect=revoke_after_snapshot):
            response=await self.client.get(self.path+'/'+plan['migration_id'])
        self.assertEqual(response.status_code,404,response.text)
        self.assertNotIn(plan['preview_digest'],response.text)

    async def test_base_and_v1_routes_share_authentication(self):
        response=await self.client.post(self.path.replace('/api/v1/','/api/'),json=self.plan_request())
        self.assertEqual(response.status_code,200,response.text)
        self.mapping_security.logout(self.mapping_cookie)
        response=await self.client.get(self.path+'/'+response.json()['migration_id'])
        self.assertEqual(response.status_code,401)
