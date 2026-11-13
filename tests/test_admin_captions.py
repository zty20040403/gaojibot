"""Caption controls use the existing authenticated, CSRF-checked audited route."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
        register_admin(self.app, AdminServices(version='TEST',started_at=1,database=self.database,
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
        response = await self.client.put(path+'/captions/settings',headers={'If-Match':str(response.json()['caption_resource_version'])},json={
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

    async def test_native_change_invalidates_caption_snapshot_not_unrelated_media_version(self):
        from src.plugins.ai_chat.conversation_config import change
        path = '/bot-admin/api/v1/media'
        response = await self.client.get(path,headers={'X-Test-Login':'yes'})
        original = response.json()
        body = {'scope_key':self.endpoint.scope.key,'enabled':True,'daily_limit':3}
        missing = await self.client.put(path+'/captions/settings',json=body)
        self.assertEqual(missing.status_code,428)
        with self.tx() as cursor:
            change(cursor,self.endpoint,'image_daily_limit',77,actor='TEST_NATIVE_ADMIN',now=ingress.NOW)
        stale = await self.client.put(path+'/captions/settings',json=body,headers={'If-Match':str(original['caption_resource_version'])})
        self.assertEqual(stale.status_code,409,stale.text)
        self.assertEqual(stale.json()['detail']['resource'],'captions')
        current = (await self.client.get(path)).json()
        self.assertEqual(current['resource_version'],original['resource_version'])
        self.assertEqual(current['caption_resource_version'],original['caption_resource_version']+1)
        self.assertEqual(current['caption_settings'][0]['image_daily_limit'],77)
        result = await self.client.put(path+'/captions/settings',json=body,headers={'If-Match':str(current['caption_resource_version'])})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['resource'],'captions')
        self.assertEqual(result.json()['resource_version'],current['caption_resource_version']+1)

    async def test_audit_failure_rolls_back_setting_and_version_in_the_same_transaction(self):
        path = '/bot-admin/api/v1/media'
        await self.client.get(path,headers={'X-Test-Login':'yes'})
        with patch('src.plugins.ai_chat.admin_control._encode_json',side_effect=ValueError('TEST audit unavailable')):
            response = await self.client.put(path+'/captions/settings',headers={'If-Match':'0'},
                json={'scope_key':self.endpoint.scope.key,'enabled':True,'daily_limit':3})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.query('SELECT * FROM conversation_settings'),[])
        self.assertEqual(self.query('SELECT * FROM admin_resource_versions'),[])
        self.assertEqual(self.query('SELECT * FROM admin_audit_log'),[])

    async def test_invalid_and_foreign_settings_do_not_change_state(self):
        path = '/bot-admin/api/v1/media/captions/settings'
        await self.client.get('/bot-admin/api/v1/media',headers={'X-Test-Login':'yes'})
        for limit in (-1,10001,True):
            response = await self.client.put(path,json={'scope_key':self.endpoint.scope.key,'enabled':True,'daily_limit':limit})
            self.assertEqual(response.status_code,422,response.text)
        response = await self.client.put(path,headers={'If-Match':'0'},json={'scope_key':'TEST_FOREIGN','enabled':True,'daily_limit':200})
        self.assertEqual(response.status_code,409,response.text)
        self.assertFalse(CaptionSettings(self.database).snapshot(ingress.NOW)[0]['image_captions'])
