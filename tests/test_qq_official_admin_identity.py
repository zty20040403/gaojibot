import hashlib,json,secrets,unittest
from types import SimpleNamespace
from unittest.mock import patch
from psycopg.errors import LockNotAvailable
from src.bot_storage.database import DatabaseError
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from src.plugins.ai_chat.adapters.qq_official.authority import ConsoleAuthorization
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease
from src.bot_security.store import lock_admin_security
from src.plugins.ai_chat.adapters.qq_official import admin_identity
from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore
from src.plugins.ai_chat.adapters.qq_official.admin_identity import AdminIdentityBinding,IdentityBindingDenied
from src.plugins.ai_chat.adapters.qq_official.validation import Unavailable

PASSWORD='TEST-bind-password-only-123456'
from tests.qq_official_admin_identity_fixture import IdentityFixture

class IdentityTests(IdentityFixture,unittest.TestCase):
 def test_actual_c2c_receipt_and_same_session_bind_exact_owner_once_without_payload_or_job(self):
  value=self.challenge();receipt=self.receive(value['command']);self.assertEqual(receipt.disposition,'control')
  self.assertTrue(self.service.status(value['challenge_id'],self.auth)['can_confirm'])
  self.assertEqual(self.service.confirm(value['challenge_id'],self.auth)['state'],'consumed')
  binding=self.rows('SELECT * FROM qq_official_admin_bindings')[0]
  self.assertEqual(binding['admin_account_id'],self.admin['account_id']);self.assertIn(self.owner,binding['c2c_actor_key'])
  self.assertEqual(self.rows('SELECT * FROM durable_jobs'),[])
  self.assertEqual(self.rows('SELECT * FROM messages'),[])
  tables=('qq_official_inbound_events','qq_official_admin_identity_challenges','admin_security_audit','qq_official_gateway_receipts')
  dump=json.dumps({t:self.rows('SELECT * FROM '+t) for t in tables},default=str)
  self.assertNotIn(value['command'],dump);self.assertNotIn(value['command'].split('.')[-1],dump)
  with self.assertRaises(IdentityBindingDenied):self.service.confirm(value['challenge_id'],self.auth)
 def test_password_and_recent_presence_without_challenge_cannot_claim_owner(self):
  value=self.challenge();self.receive('hello owner')
  with self.assertRaises(IdentityBindingDenied):self.service.confirm(value['challenge_id'],self.auth)
  self.assertFalse(self.rows('SELECT * FROM qq_official_admin_bindings'))
 def test_nonowner_group_malformed_and_no_original_time_are_never_retained(self):
  value=self.challenge()
  for kwargs in ({'actor':'TEST_STRANGER'},{'group':True},{'timestamp':None}):self.receive(value['command'],**kwargs)
  self.receive(value['command']+' extra')
  self.assertEqual(self.service.status(value['challenge_id'],self.auth)['state'],'pending')
  self.assertFalse(self.rows('SELECT * FROM durable_jobs'));self.assertFalse(self.rows('SELECT * FROM messages'))
  self.assertTrue(all(r['payload_json'] is None for r in self.rows('SELECT * FROM qq_official_inbound_events')))
 def test_owner_revoke_endpoint_generation_and_expiry_reject_confirmation(self):
  value=self.challenge();self.receive(value['command'])
  with self.tx() as c:c.execute("UPDATE qq_official_owner_grants SET state='revoked' WHERE account_key=?",(self.account.key,))
  with self.assertRaises(IdentityBindingDenied):self.service.confirm(value['challenge_id'],self.auth)
  with self.tx() as c:
   c.execute("UPDATE qq_official_owner_grants SET state='active' WHERE account_key=?",(self.account.key,))
   c.execute('UPDATE qq_official_endpoints SET generation=generation+1 WHERE account_key=?',(self.account.key,))
  with self.assertRaises(IdentityBindingDenied):self.service.confirm(value['challenge_id'],self.auth)
  self.now+=181
  self.assertEqual(self.service.status(value['challenge_id'],self.auth)['state'],'expired')
 def test_new_console_session_logout_and_admin_revision_reject(self):
  value=self.challenge();self.receive(value['command'])
  login=self.security.login('test_admin',PASSWORD,'127.0.0.1')
  other=ConsoleAuthorization(self.auth.account_id,self.auth.account_version,hashlib.sha256(login['session'].encode()).hexdigest())
  with self.assertRaises(IdentityBindingDenied):self.service.confirm(value['challenge_id'],other)
  self.security.logout(self.login['session'])
  with self.assertRaises(IdentityBindingDenied):self.service.confirm(value['challenge_id'],self.auth)
 def test_same_cursor_receipt_or_checkpoint_failure_rolls_back_observation(self):
  value=self.challenge();original=self.service.observe
  def fail(c,**kw):original(c,**kw);raise RuntimeError('TEST_ROLLBACK')
  with patch.object(self.service,'observe',side_effect=fail):
   with self.assertRaises(RuntimeError):self.receive(value['command'])
  self.assertEqual(self.service.status(value['challenge_id'],self.auth)['state'],'pending')
  self.assertEqual(self.rows('SELECT durable_seq FROM qq_official_accounts')[0]['durable_seq'],0)
  self.assertFalse(self.rows('SELECT * FROM qq_official_inbound_events'))
 def test_tampered_immutable_identity_and_replacement_code_rejected(self):
  first=self.challenge();second=self.challenge();self.receive(first['command'])
  self.assertEqual(self.service.status(first['challenge_id'],self.auth)['state'],'cancelled')
  with self.assertRaises(Exception):
   with self.tx() as c:c.execute('UPDATE qq_official_admin_identity_challenges SET expires_at=expires_at+1 WHERE challenge_id=?',(second['challenge_id'],))
  self.receive(second['command']);self.assertEqual(self.service.confirm(second['challenge_id'],self.auth)['state'],'consumed')


 def test_simultaneous_confirmation_exactly_one_binding_and_same_transaction_rollback(self):
  import concurrent.futures,threading
  value=self.challenge();self.receive(value['command'])
  original=AdminBindingStore._insert
  def fail(store,*args,**kwargs):original(store,*args,**kwargs);raise RuntimeError('TEST_AFTER_BIND_INSERT')
  with patch.object(AdminBindingStore,'_insert',fail):
   with self.assertRaises(Exception):self.service.confirm(value['challenge_id'],self.auth)
  self.assertFalse(self.rows('SELECT * FROM qq_official_admin_bindings'))
  self.assertEqual(self.service.status(value['challenge_id'],self.auth)['state'],'observed')
  second_store=OfficialInboxStore(self.database,jobs=self.jobs,binding=self.store.binding,clock=lambda:self.now)
  self.addCleanup(second_store.close);second_store.source_authority=self.store.source_authority
  runtime=SimpleNamespace(**{**vars(self.runtime),'store':second_store})
  second=AdminIdentityBinding(runtime,self.token);barrier=threading.Barrier(2)
  def execute(service):
   barrier.wait(timeout=3)
   try:return service.confirm(value['challenge_id'],self.auth)['state']
   except IdentityBindingDenied:return 'denied'
  with concurrent.futures.ThreadPoolExecutor(2) as pool:
   results=[pool.submit(execute,service) for service in (self.service,second)]
   self.assertEqual(sorted(work.result(timeout=5) for work in results),['consumed','denied'])
  self.assertEqual(len(self.rows('SELECT * FROM qq_official_admin_bindings')),1)
 def test_current_admin_revision_and_missing_service_never_accept_old_possession(self):
  value=self.challenge();self.store.admin_identity_service=None
  result=self.receive(value['command'])
  self.assertEqual(result.disposition,'control');self.assertEqual(self.service.status(value['challenge_id'],self.auth)['state'],'pending')
  self.assertTrue(all(r['payload_json'] is None for r in self.rows('SELECT * FROM qq_official_inbound_events')))
  self.store.admin_identity_service=self.service;self.receive(value['command'])
  with self.tx() as c:c.execute('UPDATE admin_accounts SET version=version+1 WHERE account_id=?',(self.auth.account_id,))
  with self.assertRaises(IdentityBindingDenied):self.service.confirm(value['challenge_id'],self.auth)


 def test_console_and_both_official_paths_share_the_transaction_lock(self):
  value=self.challenge();self.receive(value['command'])
  def bounded_lock(c):
   c.execute("SET LOCAL lock_timeout = '150ms'")
   lock_admin_security(c)
  with self.security.transaction():
   with patch.object(admin_identity,'lock_admin_security',bounded_lock),self.assertRaises(DatabaseError) as error:
    self.service.confirm(value['challenge_id'],self.auth)
   self.assertIsInstance(error.exception.__cause__,LockNotAvailable)
  self.service.confirm(value['challenge_id'],self.auth)
  endpoint=EndpointKey(self.account,'private',self.owner)
  actor=ActorKey(self.account,'c2c_user','',self.owner)
  with self.security.transaction(),self.assertRaises(Unavailable),self.tx() as c:
   c.execute("SET LOCAL lock_timeout = '150ms'")
   AdminBindingStore(owners=self.owners).resolve_admin(c,endpoint=endpoint,actor=actor)
  with self.tx() as c:
   resolved=AdminBindingStore(owners=self.owners).resolve_admin(c,endpoint=endpoint,actor=actor)
   self.assertEqual(resolved.admin_account_id,self.auth.account_id)


 def test_issued_c2c_actor_receipt_cannot_be_reused_with_a_forged_code_dto(self):
  from src.plugins.ai_chat.adapters.qq_official.admin_identity import IdentityInput
  value=self.challenge();identity,secret=value['command'].split(' ')[1].split('.')
  receipt=self.receive('/admin-bind '+identity+'.'+'0'*32)
  with self.tx() as c:
   self.assertEqual(self.service.observe(c,inbox_id=receipt.inbox_id,command=IdentityInput('control',identity,secret)),'denied')
  self.assertEqual(self.service.status(identity,self.auth)['state'],'pending')

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
  self.assertFalse(self.rows('SELECT * FROM qq_official_admin_identity_challenges'))
  self.assertFalse(self.rows('SELECT * FROM qq_official_admin_bindings'))

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

class IdentitySchemaTests(IdentityFixture,unittest.TestCase):
 def test_observed_source_union_requires_all_fields_and_exact_actor_fk(self):
  value=self.challenge();self.receive(value['command'])
  row=self.rows('SELECT * FROM qq_official_admin_identity_challenges')[0]
  other=self.challenge()
  for mutate,args in (
   ("UPDATE qq_official_admin_identity_challenges SET state='observed',source_inbox_id=?,endpoint_key=?,actor_key=?,owner_grant_id=?,owner_revision=?,observed_at=? WHERE challenge_id=?",
    (row['source_inbox_id'],row['endpoint_key'],row['actor_key'],row['owner_grant_id'],row['owner_revision'],self.now,other['challenge_id'])),
   ("UPDATE qq_official_admin_identity_challenges SET state='consumed',consumed_at=? WHERE challenge_id=?",(self.now,other['challenge_id']))):
   with self.assertRaises(Exception):
    with self.tx() as c:c.execute(mutate,args)
  self.assertEqual(self.service.status(other['challenge_id'],self.auth)['state'],'pending')
 def test_pending_code_does_not_match_another_current_account(self):
  value=self.challenge()
  account=AccountKey('qq-official','TEST_OTHER_'+secrets.token_hex(4),'test')
  self.store.ensure_account(account)
  lease=DedicatedAccountLease(self.dsn,account,schema=self.schema,clock=lambda:self.now);self.addCleanup(lease.close)
  token=lease.acquire();self.store.apply_configuration(token,(self.owner,))
  runtime=SimpleNamespace(**{**vars(self.runtime),'_token':token,'loaded':SimpleNamespace(account=account,binding_pepper=self.pepper)})
  other=AdminIdentityBinding(runtime,token)
  with self.assertRaises(IdentityBindingDenied):other.status(value['challenge_id'],self.auth)
  self.assertFalse(self.rows('SELECT * FROM qq_official_admin_bindings'))
