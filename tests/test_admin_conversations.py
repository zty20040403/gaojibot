"""Real login, CSRF, optimistic version and atomic conversation mutations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest

import httpx
from fastapi import FastAPI

from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.admin_api import conversations
from tests.admin_session_fixture import ApprovedClient,register_admin
from tests import test_qq_official_ingest as ingress


class AdminConversationTests(unittest.IsolatedAsyncioTestCase):
    setUp = ingress.QQOfficialIngestTests.setUp
    tx = ingress.QQOfficialIngestTests.tx
    query = ingress.QQOfficialIngestTests.query
    provision = ingress.QQOfficialIngestTests.provision
    path = '/bot-admin/api/v1/conversations'

    async def asyncSetUp(self):
        self.app = FastAPI()
        self.notify = Mock()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            official_runtime=lambda:SimpleNamespace(control_changed=self.notify),
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.client = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.client.aclose)

    async def login(self):
        response = await self.client.get(self.path,headers={'X-Test-Login':'yes'})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def send(self, action='stop', version=0, **extra):
        return await self.client.post(self.path+'/control',headers={'If-Match':str(version)},
            json={'endpoint_key':self.endpoint.key,'action':action,**extra})

    async def test_state_version_and_audit_commit_before_runtime_notification(self):
        initial = await self.login()
        self.assertFalse(initial['items'][0]['stopped'])
        def notified(endpoint,receipt):
            self.assertEqual(endpoint,self.endpoint.key)
            self.assertTrue(self.query('SELECT stopped FROM conversation_settings')[0]['stopped'])
        self.notify.side_effect = notified
        response = await self.send()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['resource_version'],1)
        self.notify.assert_called_once()
        self.assertEqual((await self.send()).status_code,409)
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)
        self.assertTrue((await self.client.get(self.path)).json()['items'][0]['stopped'])
        self.notify.side_effect = None
        self.assertEqual((await self.send('start',1)).status_code,200)
        self.assertFalse((await self.client.get(self.path)).json()['items'][0]['stopped'])
        self.assertEqual(self.app.state.auth_messages,[])  # No old global QQ OTP flow.

    async def test_parallel_same_version_has_exactly_one_winner(self):
        await self.login()
        responses = await asyncio.gather(self.send('stop'),self.send('start'))
        self.assertEqual(sorted(response.status_code for response in responses),[200,409])
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)
        self.notify.assert_called_once()

    async def test_operation_failure_rolls_back_state_version_and_audit(self):
        await self.login()
        original = conversations.change
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('TEST rollback')
        with patch.object(conversations,'change',side_effect=fail):
            response = await self.send()
        self.assertEqual(response.status_code,503,response.text)
        for table in ('conversation_settings','admin_resource_versions','admin_audit_log','admin_security_audit'):
            self.assertFalse(self.query('SELECT * FROM '+table),table)
        self.notify.assert_not_called()

    async def test_login_csrf_version_and_real_endpoint_are_required(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test') as anonymous:
            response = await anonymous.post(self.path+'/control',headers={'Origin':'https://test'},
                json={'endpoint_key':self.endpoint.key,'action':'stop'})
            self.assertEqual(response.status_code,401,response.text)
        await self.login()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',cookies=self.client.cookies) as no_csrf:
            response = await no_csrf.post(self.path+'/control',headers={'Origin':'https://test','If-Match':'0'},
                json={'endpoint_key':self.endpoint.key,'action':'stop'})
            self.assertEqual(response.status_code,403,response.text)
        response = await self.client.post(self.path+'/control',json={'endpoint_key':self.endpoint.key,'action':'stop'})
        self.assertEqual(response.status_code,428,response.text)
        response = await self.client.post(self.path+'/control',headers={'If-Match':'0'},
            json={'endpoint_key':'TEST_FOREIGN','action':'stop'})
        self.assertEqual(response.status_code,404,response.text)
        self.assertEqual((await self.send(authorized=True)).status_code,422)
        self.assertFalse(self.query('SELECT * FROM conversation_settings'))
