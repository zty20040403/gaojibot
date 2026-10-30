"""Real isolated PG and fake loopback M3, using unapplied source overlay."""
import asyncio
from dataclasses import replace
import json
import unittest

from src.bot_security.store import SecurityStore, SecurityError
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.approval_transport import (
 Recipient, OfficialApprovalTransport)
from src.plugins.ai_chat.adapters.qq_official.execution import ExecutionFenceLost
from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.qq_official.events import parse_dispatch
from src.plugins.ai_chat.adapters.qq_official.approval_control import prepare_approval_input,apply_control
from src.plugins.ai_chat.adapters.qq_official.approval_transport import task_effect_scope

from tests.qq_official_approval_fixture import ApprovalFixture, ApprovalLoopbackFixture, SourcePort, SECRET

class ApprovalStoreTests(ApprovalFixture):
 def test_creation_uses_real_admin_source_and_same_queue(self):
  request,code=self.propose()
  self.assertEqual(len(code),6)
  self.assertEqual((request['quorum'],request['qq_id'],request['bot_id']),(2,None,None))
  rows=self.query('SELECT * FROM qq_official_admin_otp_sources')
  self.assertEqual((len(rows),rows[0]['expires_at']-self.now),(1,30))
  self.assertEqual(rows[0]['window_id'],self.window_a)
  self.assertNotIn(code,json.dumps([dict(row) for row in rows]))
  self.assertNotIn(code,json.dumps([dict(row) for row in self.query('SELECT * FROM admin_approvals')]))
  self.assertEqual(self.propose()[1],None)
 def test_default_source_port_denies(self):
  self.transport.source_port=OfficialApprovalTransport(token=self.token,secret=SECRET).source_port
  with self.assertRaises(SecurityError): self.propose()
  self.assertFalse(self.query('SELECT * FROM admin_approvals'))
 def test_wrong_source_actor_endpoint_scope_or_epoch_does_not_authorize(self):
  variants=[replace(self.source,actor=self.other_actor,endpoint=self.other),replace(self.source,route_epoch=1),
    replace(self.source,endpoint_route_epoch=2),replace(self.source,revision=2),replace(self.source,scope=self.other.scope)]
  for source in variants:
   with self.subTest(source=source),self.assertRaises(SecurityError):
    context=replace(self.context,source=source);self.propose(payload=context.payload(),transport_context=context)
 def test_source_loss_and_owner_revoke_fail_closed(self):
  self.source_live=False
  with self.assertRaises(SecurityError): self.propose()
  self.source_live=True
  with self.tx() as c: self.owners.apply_trusted_config(c,self.account,('TEST_ADMIN_B',),'TEST_REVOKED')
  with self.assertRaises(Denied): self.propose()
 def test_no_window_cancels_before_returning_code(self):
  self.query("UPDATE qq_official_reply_windows SET state='expired'")
  request,code=self.propose()
  self.assertIsNone(code);self.assertEqual(request['status'],'cancelled')
  self.assertFalse(self.query('SELECT * FROM qq_official_admin_otp_sources'))
 def test_delivery_callback_cannot_claim_success_without_real_receipt(self):
  request,_=self.propose();self.store.delivered(request['approval_id'],1,True)
  row=self.query('SELECT * FROM admin_approvals')[0]
  self.assertEqual((row['status'],row['code_hash']),('cancelled',''))
 def test_restart_cancels_unsent_code_without_resigning(self):
  request,_=self.propose()
  with self.tx() as c:self.transport.recover(c)
  row=self.query('SELECT * FROM admin_approvals')[0]
  self.assertEqual((row['status'],row['code_hash']),('cancelled',''))
 def test_legacy_store_never_claims_official_queue(self):
  self.propose()
  old=SecurityStore(self.database,SECRET,clock=lambda:self.now);self.addCleanup(old.close)
  self.assertIsNone(old.claim('TEST_LEGACY_WORKER'))
  with self.assertRaises(SecurityError):old.confirm(None,'12345678','87654321','123456')
 def test_real_job_fence_loss_denies_creation(self):
  self.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?',(self.job.job_id,))
  with self.assertRaises(ExecutionFenceLost):self.propose()
 def test_policy_exact_operation_target_and_arguments(self):
  self.assertTrue(self.policy.permits(operation='fleet.job.create',target_id='TEST_HOST',arguments=self.arguments))
  for changes in ({'target_id':'TEST_OTHER'},{'arguments':{}},{'operation':'arbitrary.shell'}):
   self.assertFalse(self.policy.permits(**(dict(operation='fleet.job.create',target_id='TEST_HOST',arguments=self.arguments)|changes)))

class ApprovalLoopbackTests(ApprovalLoopbackFixture):
 async def test_real_m3_single_send_receipt_then_two_admin_quorum_and_existing_worker(self):
  request=await self.approved()
  calls=[]
  async def execute(row):calls.append(row['approval_id']);return {'ok':True}
  self.mobile.executors['server_task']=execute
  self.assertTrue(await self.mobile.run_once());self.assertFalse(await self.mobile.run_once())
  self.assertEqual(calls,[request['approval_id']]);self.assertEqual(len(self.fake.received),2)
  self.assertTrue(all(r['payload_mode']=='ephemeral' and r['attempts']==1 for r in self.query('SELECT * FROM deliveries')))
  self.assertTrue(all(r['wire_payload_json'] is None for r in self.query('SELECT * FROM delivery_attempts')))
  self.assertEqual(self.query('SELECT count(*) AS n FROM messages WHERE direction=\'outbound\'')[0]['n'],2)
 async def test_response_loss_cancels_approval_and_never_replays(self):
  request,code=self.propose();self.fake.drop_next_response=True
  with self.assertRaises(SecurityError):await self.delivered(request,code)
  self.assertEqual(self.query('SELECT status FROM admin_approvals')[0]['status'],'cancelled')
  self.assertEqual(len(self.fake.received),1)
  self.assertEqual(await self.otp.send_code(request,code,self.mobile.code_text(request,code)),'unknown')
  self.assertEqual(len(self.fake.received),1)
 async def test_expired_source_does_not_send(self):
  request,code=self.propose();self.now+=31
  with self.assertRaises(SecurityError):await self.delivered(request,code)
  self.assertFalse(self.fake.received);self.assertFalse(self.query('SELECT * FROM delivery_attempts'))
 async def test_wrong_recipient_code_cannot_confirm(self):
  request,code=self.propose();await self.delivered(request,code)
  with self.assertRaises(SecurityError):self.store.confirm(request['approval_id'],None,None,code,recipient=self.recipient_b)
  self.assertEqual(self.store.confirm(request['approval_id'],None,None,code,recipient=self.recipient)['status'],'awaiting_quorum')
  with self.assertRaises(SecurityError):self.store.request_review(request['approval_id'],None,None,recipient=self.recipient)
 async def test_second_admin_revocation_invalidates_queued_authority(self):
  await self.approved()
  with self.tx() as c:self.owners.apply_trusted_config(c,self.account,('TEST_ADMIN_A',),'TEST_REVOKED_B')
  self.assertIsNone(self.store.claim('TEST_WORKER'))
 async def test_raw_code_not_in_any_persisted_delivery_evidence_or_context(self):
  request,code=self.propose();await self.delivered(request,code)
  for table in ('admin_approvals','qq_official_admin_otp_sources','delivery_plans','deliveries','delivery_attempts','messages'):
   with self.subTest(table=table):self.assertNotIn(code,json.dumps([dict(row) for row in self.query('SELECT * FROM '+table)],default=str))
  self.assertIn(code,self.fake.received[0]['payload']['content'])

class ApprovalAdditionalTests(ApprovalFixture,unittest.IsolatedAsyncioTestCase):
 asyncSetUp=ApprovalLoopbackTests.asyncSetUp
 asyncTearDown=ApprovalLoopbackTests.asyncTearDown
 delivered=ApprovalLoopbackTests.delivered
 approved=ApprovalLoopbackTests.approved
 async def test_same_native_person_other_environment_cannot_claim_or_confirm(self):
  from src.plugins.ai_chat.platform_types import AccountKey
  request=await self.approved()
  account=AccountKey('qq-official',self.account.app_id,'sandbox')
  endpoint=EndpointKey(account,'private',self.endpoint.native_conversation_id)
  actor=ActorKey(account,'c2c_user','',self.actor.native_user_id)
  self.provision(endpoint);token=self.lease(account).acquire()
  transport=OfficialApprovalTransport(token=token,secret=SECRET,clock=lambda:self.now,source_port=SourcePort(self))
  other=SecurityStore(self.database,SECRET,clock=lambda:self.now,transport=transport);self.addCleanup(other.close)
  self.assertIsNone(other.claim('TEST_FOREIGN_WORKER'))
  self.assertEqual(self.query('SELECT status FROM admin_approvals WHERE approval_id=?',(request['approval_id'],))[0]['status'],'queued')
  with self.assertRaises(SecurityError):other.confirm(request['approval_id'],None,None,'123456',recipient=Recipient(endpoint,actor))
 async def test_two_concurrent_emitters_make_only_one_physical_attempt(self):
  request,code=self.propose();text=self.mobile.code_text(request,code)
  outcomes=await asyncio.gather(self.otp.send_code(request,code,text),self.otp.send_code(request,code,text))
  self.assertEqual(sorted(outcomes),['failed','success'])
  self.assertEqual(len(self.fake.received),1)
  self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')),1)
 async def test_fence_lost_before_code_plan_does_not_send(self):
  request,code=self.propose()
  self.query('UPDATE qq_official_accounts SET fencing_epoch=fencing_epoch+1 WHERE account_key=?',(self.account.key,))
  with self.assertRaises(LeaseLost):await self.otp.send_code(request,code,self.mobile.code_text(request,code))
  self.assertFalse(self.fake.received);self.assertFalse(self.query('SELECT * FROM delivery_attempts'))

 async def test_approval_otp_cannot_persist_nonminimal_ephemeral_manifest(self):
  from src.bot_storage.database import DatabaseError
  request,code=self.propose()
  ref=self.otp._prepare(request,code,self.mobile.code_text(request,code))
  for encoded in ('{}','{"v":1,"ephemeral":false}','{"v":1,"ephemeral":true,"extra":1}'):
   with self.subTest(encoded=encoded),self.assertRaises(DatabaseError) as caught:
    self.query('UPDATE delivery_plans SET manifest_json=? WHERE plan_id=?',(encoded,ref.plan_id))
   self.assertEqual(caught.exception.__cause__.diag.constraint_name,'ck_approval_ephemeral_manifest')
  self.assertEqual(self.query('SELECT manifest_json FROM delivery_plans WHERE plan_id=?',(ref.plan_id,))[0]['manifest_json'],
                   '{"v":1,"ephemeral":true}')
  self.assertFalse(self.fake.received)

class ApprovalControlTests(ApprovalFixture,unittest.IsolatedAsyncioTestCase):
 asyncSetUp=ApprovalLoopbackTests.asyncSetUp
 asyncTearDown=ApprovalLoopbackTests.asyncTearDown
 delivered=ApprovalLoopbackTests.delivered
 approved=ApprovalLoopbackTests.approved
 async def test_real_receipt_confirmation_and_replay_never_votes_twice(self):
  request,code=self.propose();await self.delivered(request,code)
  command=self.control_command(code);source=self.source_receipt(command)
  result=self.apply_control(command,source)
  self.assertEqual(result.decision,'accepted')
  self.assertTrue(self.apply_control(command,source).duplicate)
  self.assertEqual(self.query('SELECT status FROM admin_approvals')[0]['status'],'awaiting_quorum')
  self.assertEqual(len(self.query('SELECT * FROM qq_official_admin_control_receipts')),1)
  self.assertFalse(self.query('SELECT * FROM qq_official_inbound_events WHERE payload_json IS NOT NULL OR turn_text LIKE ?',('%'+code+'%',)))
 async def test_fake_source_or_modified_command_cannot_confirm(self):
  request,code=self.propose();await self.delivered(request,code)
  command=self.control_command(code)
  with self.assertRaises(SecurityError):self.apply_control(command,'TEST_NOT_RECEIVED')
  wrong=self.control_command('999999' if code!='999999' else '888888');source=self.source_receipt(wrong)
  with self.assertRaises(SecurityError):self.apply_control(command,source)
  self.assertEqual(self.apply_control(wrong,source).decision,'denied')
  self.assertEqual(self.query('SELECT attempts FROM admin_approvals')[0]['attempts'],1)
 async def test_tx_a_rollback_does_not_consume_vote(self):
  request,code=self.propose();await self.delivered(request,code)
  command=self.control_command(code);source=self.source_receipt(command)
  with self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'):
   with self.tx() as c:
    apply_control(c,store=self.store,inbox_id=source,session_id='TEST_CONTROL_SESSION',seq=10,command=command,now=self.now)
    raise RuntimeError('TEST_ROLLBACK')
  self.assertEqual(self.query('SELECT status FROM admin_approvals')[0]['status'],'pending')
  self.assertFalse(self.query('SELECT * FROM qq_official_admin_control_receipts'))
 async def test_same_shared_review_state_machine_issues_ram_only_code(self):
  request,code=self.propose();await self.delivered(request,code)
  self.store.confirm(request['approval_id'],None,None,code,recipient=self.recipient)
  command=self.control_command('review '+request['approval_id'],self.recipient_b)
  source=self.source_receipt(command,self.recipient_b)
  result=self.apply_control(command,source)
  self.assertEqual(result.decision,'sending');self.assertIsNotNone(result.code)
  await self.delivered(result.request,result.code)
  self.assertIsNone(self.apply_control(command,source).code)
 async def test_task_effect_scope_exact_policy_and_revocation(self):
  await self.approved()
  async def execute(row):return {'ok':True}
  self.mobile.executors['server_task']=execute;await self.mobile.run_once()
  kwargs=dict(context=self.context,operation='fleet.job.create',target_id='TEST_HOST',arguments=self.arguments)
  with task_effect_scope(self.store,**kwargs) as c:
   self.assertIsNotNone(c.execute('SELECT 1').fetchone())
  with self.assertRaises(SecurityError):
   with task_effect_scope(self.store,**(kwargs|{'target_id':'TEST_OTHER'})):pass
  self.route_live=False
  with self.assertRaises(SecurityError):
   with task_effect_scope(self.store,**kwargs):pass

class InputPrivacyTests(unittest.TestCase):
 def test_trusted_mentions_and_embedded_approval_privacy(self):
  from src.plugins.ai_chat.platform_types import AccountKey
  account=AccountKey('qq-official','TEST_APP','test')
  endpoint=EndpointKey(account,'private','TEST_OWNER')
  cases=[('123456',[],'control'),('<@!TEST_FAKE> 123456',[],'quarantine'),('/confirm AP-ABCDEF123456 123456',[],'control'),
   ('<@!TEST_BOT> /confirm AP-ABCDEF123456 123456',[{'id':'TEST_BOT','is_you':True}],'control'),
   ('<@!TEST_BOT> /confirm AP-ABCDEF123456 123456',[],'quarantine'),
   ('quoted /confirm AP-ABCDEF123456 123456',[],'quarantine'),('ordinary chat',[],'none')]
  for text,mentions,kind in cases:
   with self.subTest(kind=kind):
    dispatch=parse_dispatch({'op':0,'t':'C2C_MESSAGE_CREATE','s':1,'d':{'id':'TEST_MESSAGE','content':text,
       'author':{'user_openid':'TEST_OWNER'},'mentions':mentions}})
    result=prepare_approval_input(dispatch,endpoint);self.assertEqual(result.kind,kind)
    self.assertNotIn('123456',repr(result))
  group=EndpointKey(account,'group','TEST_GROUP')
  dispatch=parse_dispatch({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','s':1,'d':{'id':'TEST_MESSAGE',
    'group_openid':'TEST_GROUP','content':'/confirm AP-ABCDEF123456 123456','author':{'member_openid':'TEST_MEMBER'}}})
  self.assertEqual(prepare_approval_input(dispatch,group).kind,'quarantine')
