"""Caption controls use the existing authenticated, CSRF-checked audited route."""
from types import SimpleNamespace
import unittest

import httpx
from fastapi import FastAPI

from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.image_captions import CaptionSettings
from tests.admin_session_fixture import ApprovedClient, register_admin
from tests import test_qq_official_ingest as ingress


class AdminCaptionTests(unittest.IsolatedAsyncioTestCase):
    setUp = ingress.QQOfficialIngestTests.setUp
    tx = ingress.QQOfficialIngestTests.tx
    query = ingress.QQOfficialIngestTests.query
    provision = ingress.QQOfficialIngestTests.provision

    async def asyncSetUp(self):
        self.app = FastAPI()
        register_admin(self.app, AdminServices(version='TEST',started_at=1,
            media_library=SimpleNamespace(database=self.database,admin_snapshot=lambda **kwargs: {'items':[], 'jobs':[], 'counts':{}}),
            vision_worker=SimpleNamespace(admin_snapshot=lambda **kwargs: {'items':[], 'counts':{}}),
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.client = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_authenticated_settings_are_persistent_visible_and_audited(self):
        path = '/bot-admin/api/v1/media'
        response = await self.client.get(path,headers={'X-Test-Login':'yes'})
        self.assertEqual(response.status_code,200,response.text)
        row = response.json()['caption_settings'][0]
        self.assertFalse(row['image_captions'])
        self.assertEqual(row['image_daily_limit'],200)
        response = await self.client.put(path+'/captions/settings',json={
            'scope_key':self.endpoint.scope.key,'enabled':True,'daily_limit':3})
        self.assertEqual(response.status_code,200,response.text)
        audit = await self.client.get('/bot-admin/api/v1/audit')
        self.assertEqual(audit.status_code,200,audit.text)
        self.assertIn('media.captions.set',audit.text)
        saved = CaptionSettings(self.database).snapshot(ingress.NOW)[0]
        self.assertTrue(saved['image_captions'])
        self.assertEqual(saved['image_daily_limit'],3)
        response = await self.client.get(path)
        self.assertEqual(response.json()['caption_settings'][0]['image_daily_limit'],3)

    async def test_invalid_and_foreign_settings_do_not_change_state(self):
        path = '/bot-admin/api/v1/media/captions/settings'
        await self.client.get('/bot-admin/api/v1/media',headers={'X-Test-Login':'yes'})
        for limit in (-1,10001,True):
            response = await self.client.put(path,json={'scope_key':self.endpoint.scope.key,'enabled':True,'daily_limit':limit})
            self.assertEqual(response.status_code,422,response.text)
        response = await self.client.put(path,json={'scope_key':'TEST_FOREIGN','enabled':True,'daily_limit':200})
        self.assertEqual(response.status_code,409,response.text)
        self.assertFalse(CaptionSettings(self.database).snapshot(ingress.NOW)[0]['image_captions'])
