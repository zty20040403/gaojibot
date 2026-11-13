"""API audit is read-only and available only to logged-in administrators."""
from types import SimpleNamespace
import unittest

import httpx
from fastapi import FastAPI

from src.bot_security.passwords import hash_password
from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.host_api_calls import HostAPICalls
from tests.admin_session_fixture import ApprovedClient,PASSWORD,register_admin
from tests.postgres_fixture import isolated_postgres_for_test


class AdminHostAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = isolated_postgres_for_test(self)
        store = HostAPICalls(self.database)
        for scope in ('TEST_A','TEST_B','TEST_A'):
            store.begin(scope_key=scope,api_name='test',caller_ref='job#1',method='POST',
                path='/TEST?key=TEST_PRIVATE',limit=0,rejection=(400,'invalid_api_path'))
        self.app = FastAPI()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.client = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.client.aclose)
        self.path = '/bot-admin/api/v1/host-apis'

    async def test_authorized_filters_pagination_and_redaction(self):
        response = await self.client.get(self.path,headers={'X-Test-Login':'yes'},
            params={'scope_key':'TEST_A','api_name':'test','limit':1})
        self.assertEqual(response.status_code,200,response.text)
        payload = response.json()
        self.assertEqual(len(payload['items']),1)
        self.assertEqual(payload['counts'],{'denied':2})
        self.assertNotIn('TEST_PRIVATE',response.text)
        self.assertEqual(payload['items'][0]['path'],'<invalid>')
        second = await self.client.get(self.path,params={'scope_key':'TEST_A','before':payload['next_cursor']})
        self.assertEqual(len(second.json()['items']),1)
        self.assertIsNone(second.json()['next_cursor'])
        self.assertEqual((await self.client.get(self.path,params={'limit':501})).status_code,422)
        self.assertEqual((await self.client.post(self.path,json={})).status_code,405)
        self.assertEqual(self.app.state.auth_messages,[])

    async def test_anonymous_and_member_cannot_read_any_scope(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test') as client:
            self.assertEqual((await client.get(self.path)).status_code,401)
            store = self.app.state.mobile.store
            store.apply_account_change(store.account_for_qq('123456789'),{'action':'create','username':'test_member',
                'password_hash':hash_password(PASSWORD),'role':'member','qq_id':None})
            response = await client.post('/bot-admin/api/v1/auth/login',headers={'Origin':'https://test'},
                json={'username':'test_member','password':PASSWORD})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual((await client.get(self.path)).status_code,403)
