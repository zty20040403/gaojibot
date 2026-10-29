"""Actual shared authorization + real PG Fleet store/HTTP contract, no host execution."""

from src.bot_security.store import SecurityError
from src.plugins.ai_chat.qq_action_authorization import AUTOMATIC_SANDBOX_TOOLS
from src.plugins.ai_chat.fleet_client import FleetControlError

from tests.qq_official_host_fixture import HostFixture

class HostTests(HostFixture):
 async def test_actual_shared_authorization_and_fleet_contract_queue_once(self):
  first=await self.prepare();record=first['operation']
  self.assertTrue(first['approval_required']);self.assertEqual(self.remote_store.get_operation(record['operation_id'])['status'],'awaiting_approval')
  self.assertEqual(len(self.fake.received),1)
  text=self.fake.received[0]['payload']['content']
  for key in ('testhost','test.service','service.restart','resource_version','contract_hash','deadline_at'):
   self.assertIn(key,text)
  await self.quorum()
  result=await self.approve(record)
  self.assertTrue(result['submitted']);self.assertFalse(result['executed'])
  self.assertEqual(self.remote_store.get_operation(record['operation_id'])['status'],'queued')
  self.assertTrue((await self.approve(record))['submitted'])
  self.assertEqual(len([r for r in self.http_calls if r[0]=='POST' and r[1].endswith('/approve')]),1)
  self.assertEqual(self.query("SELECT count(*) AS n FROM fleet_operation_events WHERE event_type='approved'")[0]['n'],1)
 async def test_unknown_contract_or_unbound_identity_never_posts(self):
  for body in (self.body|{'host_id':'other'},self.body|{'operation':'exec.run'},self.body|{'arguments':{'script':'TEST'}},
               self.body|{'url':'http://TEST.invalid'}):
   with self.assertRaises(FleetControlError):await self.prepare(body)
  self.assertFalse(self.http_calls)
  with self.assertRaises(FleetControlError):await self.client.prepare_operation(self.body,actor='official',origin=self.source.scope.key)
  self.assertFalse(self.http_calls)
 async def test_exact_contract_changed_after_quorum_never_approves(self):
  record=(await self.prepare())['operation'];await self.quorum()
  self.query('UPDATE fleet_operations SET resource_version=resource_version+1 WHERE operation_id=?',(record['operation_id'],))
  with self.assertRaises(FleetControlError):await self.approve(record)
  self.assertFalse([r for r in self.http_calls if r[0]=='POST' and r[1].endswith('/approve')])
 async def test_revoked_grant_after_quorum_denies_remote_approval(self):
  record=(await self.prepare())['operation'];await self.quorum()
  with self.tx() as c:self.owners.apply_trusted_config(c,self.account,('TEST_ADMIN_B',),'TEST_REVOKED')
  with self.assertRaises((FleetControlError,PermissionError)):await self.approve(record)
  self.assertEqual(self.remote_store.get_operation(record['operation_id'])['status'],'awaiting_approval')
 async def test_direct_unknown_fleet_mutation_cannot_use_task_objective(self):
  await self.prepare();await self.quorum()
  with self.host.bind(self.source):
   for path in ('/v1/ops/call','/v1/deployments/prepare','/v1/jobs','/v1/artifacts'):
    with self.subTest(path=path),self.assertRaises(FleetControlError):
     await self.client._authorized_request('POST',path,{},actor='official',origin=self.source.scope.key)
  self.assertFalse([r for r in self.http_calls if r[1] in {'/v1/ops/call','/v1/jobs','/v1/artifacts'}])
  self.assertIn('sandbox_exec',AUTOMATIC_SANDBOX_TOOLS)
 async def test_changed_caller_origin_or_actor_rejected(self):
  with self.host.bind(self.source):
   for actor,origin in (('qq:12345',self.source.scope.key),('official','other')):
    with self.assertRaises(FleetControlError):await self.client.prepare_operation(self.body,actor=actor,origin=origin)
  self.assertFalse(self.http_calls)

 async def test_invalid_contract_types_do_not_authorize_even_with_matching_hash(self):
  from src.cluster_control.execution_contracts import content_hash
  from src.plugins.ai_chat.adapters.qq_official.host_authority import _CONTRACT
  record=(await self.prepare())['operation']
  for changes in ({'backend_binding_version':True},{'arguments':None},
        {'resource_budget':{'wall_seconds':True}},{'resource_budget':{'wall_seconds':301}},
        {'task_ref':None},{'resource_ref':['test.service']}):
   altered=record|changes
   altered['contract_hash']=content_hash({key:altered[key] for key in _CONTRACT})
   with self.subTest(changes=changes),self.assertRaises(SecurityError):
    self.host.contract(altered,self.source,self.account_a)

 async def test_http_has_no_sql_locks_and_late_receipt_survives_revoke(self):
  import asyncio
  import json
  from unittest.mock import patch
  record=(await self.prepare())['operation'];await self.quorum()
  entered=asyncio.Event();release=asyncio.Event();original=self.client._raw_request
  async def paused(method,path,*args,**kwargs):
   if method=='POST' and path.endswith('/approve'):
    entered.set();await release.wait()
   return await original(method,path,*args,**kwargs)
  with patch.object(self.client,'_raw_request',new=paused):
   pending=asyncio.create_task(self.approve(record))
   try:
    await asyncio.wait_for(entered.wait(),3)
    def revoke():
     with self.tx() as cursor:
      cursor.execute('SET LOCAL lock_timeout=1000')
      # Every current-authority lock can be acquired while the HTTP is pending.
      cursor.execute('SELECT * FROM qq_official_accounts FOR UPDATE NOWAIT').fetchall()
      cursor.execute('SELECT * FROM qq_official_endpoints FOR UPDATE NOWAIT').fetchall()
      cursor.execute('SELECT * FROM admin_approvals FOR UPDATE NOWAIT').fetchall()
      cursor.execute('SELECT * FROM durable_jobs FOR UPDATE NOWAIT').fetchall()
      cursor.execute('SELECT * FROM qq_official_turn_executions FOR UPDATE NOWAIT').fetchall()
      cursor.execute('SELECT * FROM admin_security_lock FOR UPDATE NOWAIT').fetchall()
      self.owners.apply_trusted_config(cursor,self.account,('TEST_ADMIN_B',),'TEST_REVOKED')
    await asyncio.wait_for(asyncio.to_thread(revoke),3)
   finally:release.set()
   result=await asyncio.wait_for(pending,3)
  self.assertTrue(result['submitted'])
  effects=json.loads(self.query("SELECT result_json FROM admin_approvals WHERE kind='server_task'")[0]['result_json'])['official_effects']
  self.assertEqual(effects[record['operation_id']]['remote_status'],'queued')
  with self.assertRaises((FleetControlError,PermissionError)):await self.approve(record)
  self.assertEqual(len([r for r in self.http_calls if r[0]=='POST' and r[1].endswith('/approve')]),1)

 async def test_concurrent_approval_and_unknown_never_repost(self):
  import asyncio
  import json
  from unittest.mock import patch
  record=(await self.prepare())['operation'];await self.quorum()
  entered=asyncio.Event();release=asyncio.Event();original=self.client._raw_request;attempts=0
  async def lost(method,path,*args,**kwargs):
   nonlocal attempts
   if method=='POST' and path.endswith('/approve'):
    attempts+=1;entered.set();await release.wait();raise TimeoutError('TEST_UNKNOWN')
   return await original(method,path,*args,**kwargs)
  with patch.object(self.client,'_raw_request',new=lost):
   first=asyncio.create_task(self.approve(record))
   try:
    await asyncio.wait_for(entered.wait(),3)
    second=await asyncio.wait_for(self.approve(record),3)
    self.assertIsNone(second['executed']);self.assertEqual(second['status'],'needs_attention')
   finally:release.set()
   with self.assertRaises(TimeoutError):await first
  third=await self.approve(record)
  self.assertIsNone(third['executed']);self.assertEqual(attempts,1)
  row=self.query("SELECT * FROM admin_approvals WHERE kind='server_task'")[0]
  effects=json.loads(row['result_json'])['official_effects']
  self.assertEqual(effects[record['operation_id']]['state'],'unknown')
  # Even an old grant worker's delayed finish cannot erase attempt evidence.
  self.query("UPDATE admin_approvals SET status='executing',execution_owner='TEST_OLD' WHERE approval_id=?",(row['approval_id'],))
  self.store.finish(row['approval_id'],'TEST_OLD',{'ok':True,'official_effects':{}},status='succeeded')
  saved=json.loads(self.query('SELECT result_json FROM admin_approvals WHERE approval_id=?',(row['approval_id'],))[0]['result_json'])
  self.assertEqual(saved['official_effects'],effects)
  self.assertIsNone((await self.approve(record))['executed'])

 async def test_prepare_idempotency_is_bound_to_source_and_parameters(self):
  first=(await self.prepare())['operation']
  second=(await self.prepare(self.body|{'idempotency_key':'TEST_DIFFERENT_CALL'}))['operation']
  self.assertEqual(first['operation_id'],second['operation_id'])

 async def test_admitted_receipt_is_historical_after_account_fence_loss(self):
  import json
  from unittest.mock import patch
  from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
  record=(await self.prepare())['operation'];await self.quorum();original=self.client._raw_request
  async def lose(method,path,*args,**kwargs):
   if method=='POST' and path.endswith('/approve'):
    self.query('UPDATE qq_official_accounts SET fencing_epoch=fencing_epoch+1 WHERE account_key=?',(self.account.key,))
   return await original(method,path,*args,**kwargs)
  with patch.object(self.client,'_raw_request',new=lose):result=await self.approve(record)
  self.assertTrue(result['submitted'])
  evidence=json.loads(self.query("SELECT result_json FROM admin_approvals WHERE kind='server_task'")[0]['result_json'])
  self.assertEqual(evidence['official_effects'][record['operation_id']]['remote_status'],'queued')
  with self.assertRaises(LeaseLost):await self.approve(record)
  self.assertEqual(len([r for r in self.http_calls if r[0]=='POST' and r[1].endswith('/approve')]),1)
