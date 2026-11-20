import unittest
from types import SimpleNamespace

PASSWORD='TEST-bind-password-only-123456'
from tests.qq_official_admin_identity_fixture import IdentityFixture

class IdentityHTTPTests(IdentityFixture,unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  import httpx
  from fastapi import FastAPI
  from src.bot_storage import DatabaseCalls
  from src.plugins.ai_chat.admin import AdminServices,register_admin
  self.calls=DatabaseCalls();self.runtime._calls=self.calls
  from src.plugins.ai_chat.adapters.qq_official.observability import OfficialObservability
  from prometheus_client import CollectorRegistry
  self.runtime.config=SimpleNamespace(enabled=True)
  self.runtime._gateway=None;self.runtime.sender=None
  self.diagnostics=OfficialObservability(SimpleNamespace(official_runtime=self.runtime),registry=CollectorRegistry(),clock=lambda:self.now)
  self.app=FastAPI();register_admin(self.app,AdminServices(version='test',started_at=1,database=self.database,
   settings=SimpleNamespace(admin_origin='https://test'),console_store=self.security,
   official_observability=lambda:self.diagnostics))
  self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',headers={'Origin':'https://test'})
 async def asyncTearDown(self):await self.client.aclose();await self.diagnostics.close();await self.calls.close()
 async def login_http(self):
  response=await self.client.post('/bot-admin/api/auth/login',json={'username':'test_admin','password':PASSWORD})
  self.assertEqual(response.status_code,200,response.text)
  self.client.headers['x-csrf-token']=self.client.cookies['gaoji_csrf']
 async def test_console_cannot_create_poll_or_confirm_qq_identity(self):
  await self.login_http()
  for prefix in ('/bot-admin/api','/bot-admin/api/v1'):
   path=prefix+'/qq-official/admin-binding/challenges'
   self.assertEqual((await self.client.post(path,json={})).status_code,404)
   self.assertEqual((await self.client.get(path+'/TEST_ID')).status_code,404)
   self.assertEqual((await self.client.post(path+'/TEST_ID/confirm',json={})).status_code,404)

 async def test_protected_status_rejects_anonymous_member_and_account_queries(self):
  path='/bot-admin/api/qq-official/status'
  self.assertEqual((await self.client.get(path)).status_code,401)
  await self.login_http();await self.diagnostics.sample()
  response=await self.client.get(path);self.assertEqual(response.status_code,200,response.text)
  self.assertEqual(response.headers['cache-control'],'no-store')
  self.assertNotIn(self.account.key,response.text);self.assertNotIn(self.owner,response.text)
  self.assertEqual((await self.client.get(path+'?account=TEST_OTHER')).status_code,400)
  from src.bot_security.passwords import hash_password
  self.security.apply_account_change(self.admin,{'action':'create','username':'test_member','password_hash':hash_password(PASSWORD),'role':'member','qq_id':None})
  self.client.cookies.clear()
  result=await self.client.post('/bot-admin/api/auth/login',json={'username':'test_member','password':PASSWORD})
  self.assertEqual(result.status_code,200,result.text)
  self.assertEqual((await self.client.get(path)).status_code,403)
