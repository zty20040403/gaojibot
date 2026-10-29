import json,unittest
from types import SimpleNamespace
from prometheus_client import CollectorRegistry
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.platform_types import EndpointKey,AccountKey
from src.plugins.ai_chat.adapters.qq_official.observability import OfficialObservability
from src.plugins.ai_chat.adapters.qq_official.official_details import OfficialDetails,DetailsUnavailable
from tests.qq_official_admin_identity_fixture import IdentityFixture
from tests import test_qq_official_admin_identity as identity_support
from tests import test_qq_official_metrics as sender_support

class DetailTests(IdentityFixture,unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.calls=DatabaseCalls();self.runtime._calls=self.calls
  self.context=SimpleNamespace(official_runtime=self.runtime)
  self.observer=OfficialObservability(self.context,registry=CollectorRegistry(),clock=lambda:self.now)
  self.reader=OfficialDetails(self.runtime,clock=lambda:self.now)
 async def asyncTearDown(self):await self.observer.close();await self.calls.close()
 async def test_actual_checkpoint_and_current_binding_without_raw_identity_or_code(self):
  challenge=self.challenge();self.receive(challenge['command']);self.service.confirm(challenge['challenge_id'],self.auth)
  value=await self.observer.details(section='bindings')
  self.assertEqual(value['checkpoint']['durable_seq'],1);self.assertTrue(value['checkpoint']['session_present'])
  self.assertEqual(value['checkpoint']['fencing_epoch'],self.token.epoch)
  item=value['items'][0];self.assertTrue(item['admin_binding']['current']);self.assertTrue(item['admin_binding']['owner_current'])
  with self.tx() as c:c.execute("UPDATE qq_official_owner_grants SET state='revoked' WHERE account_key=?",(self.account.key,))
  self.assertFalse((await self.observer.details(section='bindings'))['items'][0]['admin_binding']['current'])
  encoded=json.dumps(value)
  for secret in (self.account.key,self.owner,self.session,self.auth.account_id,self.auth.session_hash,challenge['command'],'TEST_NATIVE_1'):
   self.assertNotIn(secret,encoded)
 async def test_bound_pagination_tamper_account_and_expiry(self):
  for i in range(3):
   with self.tx() as c:self.store._endpoint(c,EndpointKey(self.account,'group',f'TEST_GROUP_{i}'),self.now)
  first=self.reader.read(section='bindings',limit=1);self.assertEqual(len(first['items']),1)
  second=self.reader.read(section='bindings',limit=1,cursor=first['next_cursor']);self.assertNotEqual(first['items'][0]['ref'],second['items'][0]['ref'])
  for kwargs in ({'limit':51},{'limit':True},{'cursor':'SECRET_URL'},{'section':'deliveries','cursor':first['next_cursor']}):
   with self.assertRaises(DetailsUnavailable):self.reader.read(**{'section':'bindings',**kwargs})
  other_runtime=SimpleNamespace(**vars(self.runtime));other_runtime.loaded=SimpleNamespace(account=AccountKey('qq-official','TEST_OTHER','test'),binding_pepper=self.pepper)
  with self.assertRaises(DetailsUnavailable):OfficialDetails(other_runtime,clock=lambda:self.now).read(section='bindings',cursor=first['next_cursor'])
  self.now+=301
  with self.assertRaises(DetailsUnavailable):self.reader.read(section='bindings',cursor=first['next_cursor'])
 async def test_read_only_does_not_call_current_authority_or_change_rows(self):
  value=self.challenge();self.receive(value['command']);self.service.confirm(value['challenge_id'],self.auth)
  before=self.rows('SELECT revision FROM admin_security_lock')
  self.reader.read(section='bindings');self.reader.read(section='executions');self.reader.read(section='deliveries')
  self.assertEqual(before,self.rows('SELECT revision FROM admin_security_lock'))
  await self.calls(self.lease.close)
  self.assertTrue(self.reader.read(section='bindings')['checkpoint']['session_present'])
 async def test_real_ingest_execution_deadline_and_safe_reason(self):
  from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
  from src.plugins.ai_chat.adapters.qq_official.execution import claim_execution
  from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
  from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
  from src.plugins.ai_chat.ledger import MessageLedger
  ledger=MessageLedger(self.database);self.addCleanup(ledger.close)
  receipt=self.receive('SECRET_MODEL_BODY https://private/?token=TEST_SECRET')
  with self.tx() as c:process_inbox(c,inbox_id=receipt.inbox_id,lease_token=self.token,ledger=ledger,jobs=self.jobs,epoch_guard=assert_epoch,admission=lambda *args:True,now=self.now)
  job=self.jobs.claim_due('TEST_DETAIL_WORKER',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
  with self.tx() as c:
   result=claim_execution(c,token=self.token,job=job,now=self.now,deadline_at=self.now+600,admission=lambda *args:True)
   c.execute("UPDATE qq_official_turn_executions SET reason_code='private_sentinel' WHERE execution_id=?",(result.handle.execution_id,))
  value=self.reader.read(section='executions');item=value['items'][0]
  self.assertEqual(item['state'],'prepared');self.assertEqual(item['deadline_at'],self.now+600);self.assertEqual(item['reason'],'other')
  for private in ('SECRET_MODEL_BODY','https://private','private_sentinel',result.handle.execution_id,self.owner):self.assertNotIn(private,json.dumps(value))
 async def test_actual_background_task_has_own_execution_deadline_and_no_fabricated_result_deadline(self):
  from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
  from src.plugins.ai_chat.adapters.qq_official import execution
  from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
  from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
  from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import submit
  from src.plugins.ai_chat.ledger import MessageLedger
  from src.plugins.ai_chat.subagents import SubAgentStore
  from src.plugins.ai_chat.agent.contracts import ContextPacket
  ledger=MessageLedger(self.database);store=SubAgentStore(self.database)
  self.addCleanup(ledger.close);self.addCleanup(store.close)
  receipt=self.receive('SECRET_SUBTASK_OBJECTIVE')
  with self.tx() as c:process_inbox(c,inbox_id=receipt.inbox_id,lease_token=self.token,ledger=ledger,jobs=self.jobs,epoch_guard=assert_epoch,admission=lambda *args:True,now=self.now)
  job=self.jobs.claim_due('TEST_DETAIL_TASK_WORKER',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
  with self.tx() as c:
   handle=execution.claim_execution(c,token=self.token,job=job,now=self.now,deadline_at=self.now+600,admission=lambda *args:True,execution_kind='command').handle
   execution.begin_command_execution(c,token=self.token,handle=handle,job=job,now=self.now,admission=lambda *args:True)
   packet=ContextPacket(handle.scope_key,'TEST_CONVERSATION',None,handle.canonical_message_id,'SECRET_SUBTASK_OBJECTIVE',endpoint=handle.endpoint,requester_actor=handle.actor)
   submit(c,token=self.token,source_api=execution,source_handle=handle,source_job=job,tool_call_id='TEST_CALL',tool_name='run_subagents',packet=packet,decision=None,profile='test',now=self.now,execution_deadline=self.now+1200,result_wait_seconds=600,admission=lambda *args:True,coordinator=SimpleNamespace(store=store,max_parallelism=1,max_steps=4),jobs=self.jobs)
  rows=self.reader.read(section='executions')['items']
  self.assertEqual(len(rows),2)
  task=next(r for r in rows if r['kind']=='subagent')
  self.assertEqual(task['state'],'queued');self.assertEqual(task['deadline_at'],self.now+1200);self.assertIsNone(task['result_deadline_at']);self.assertIsNone(task['plan_ref'])
  self.assertNotIn('SECRET_SUBTASK_OBJECTIVE',json.dumps(rows))
 async def test_download_status_four_strict_bools_only(self):
  self.context.official_host=SimpleNamespace(download_status=lambda:dict(configured=True,listening=1,proxy_reachable=True,evidence_configured=True,url='https://SECRET'))
  self.assertEqual(self.observer.snapshot()['download'],dict(configured=True,listening=False,proxy_reachable=True,evidence_configured=True))
  self.context.official_host=SimpleNamespace(download_status=lambda:(_ for _ in ()).throw(RuntimeError('SECRET')))
  self.assertFalse(any(self.observer.snapshot()['download'].values()))

class DetailHTTPTests(IdentityFixture,unittest.IsolatedAsyncioTestCase):
 asyncSetUp=identity_support.IdentityHTTPTests.asyncSetUp
 asyncTearDown=identity_support.IdentityHTTPTests.asyncTearDown
 login_http=identity_support.IdentityHTTPTests.login_http
 async def test_cookie_protected_bounded_query_and_safe_errors(self):
  path='/bot-admin/api/qq-official/details'
  self.assertEqual((await self.client.get(path)).status_code,401)
  await self.login_http()
  for query in ('?account=SECRET','?section=bindings&limit=500','?section=bindings&section=executions','?section=bindings&cursor=SECRET'):
   response=await self.client.get(path+query);self.assertEqual(response.status_code,400,response.text);self.assertNotIn('SECRET',response.text)
  response=await self.client.get(path+'?section=bindings&limit=1')
  self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.headers['cache-control'],'no-store')

class DetailReceiptTests(IdentityFixture,unittest.IsolatedAsyncioTestCase):
 asyncSetUp=sender_support.SenderMetricsTests.asyncSetUp
 asyncTearDown=sender_support.SenderMetricsTests.asyncTearDown
 make_sender=sender_support.SenderMetricsTests.make_sender
 ensure_current=sender_support.SenderMetricsTests.ensure_current
 enqueue=sender_support.SenderMetricsTests.enqueue
 window=sender_support.SenderMetricsTests.window
 query=IdentityFixture.rows
 async def test_actual_parts_receipts_and_unknown_are_separate_from_execution(self):
  from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
  from src.plugins.ai_chat.message_ir import MessageBody,TextNode
  rendered=render_plan(MessageBody((TextNode(0,'SECRET_PAYLOAD '*30),)),self.endpoint,max_chars=250)
  self.assertGreater(len(rendered.parts),1)
  plan=self.outbox.enqueue(self.token,rendered=rendered,requester=self.actor,logical_key='TEST_DETAIL_PARTS',source_scope_key=self.endpoint.scope.key,expires_at=self.now+200,source_native_message_id='TEST_SOURCE')
  self.assertEqual((await self.sender.send_delivery(plan.delivery_ids[0])).status,'committed')
  self.fake.drop_next_response=True
  self.assertEqual((await self.sender.send_delivery(plan.delivery_ids[1])).status,'ambiguous')
  reader=OfficialDetails(self.runtime,clock=lambda:self.now);page=reader.read(section='deliveries')
  by_part={r['part_index']:r for r in page['items']}
  self.assertEqual(by_part[0]['state'],'committed');self.assertEqual(by_part[0]['attempt']['acceptance'],'confirmed_yes')
  self.assertEqual(len(by_part[0]['receipts']),1);self.assertIsNotNone(by_part[0]['receipts'][0]['applied_at'])
  self.assertEqual(by_part[1]['state'],'ambiguous');self.assertEqual(by_part[1]['attempt']['acceptance'],'unknown');self.assertFalse(by_part[1]['receipts'])
  encoded=json.dumps(page)
  for private in ('SECRET_PAYLOAD','TEST_SOURCE',self.account.key,self.actor.native_user_id,self.fake.received[0]['receipt']['id']):self.assertNotIn(private,encoded)
  self.assertFalse(reader.read(section='executions')['items'])

class DetailsSSETests(unittest.TestCase):
 def test_existing_sse_database_counters_invalidate_details_without_body_or_ids(self):
  from src.plugins.ai_chat.admin_api.realtime import _changed_database_resources
  for table in ('delivery_receipts','delivery_attempts','qq_official_admin_bindings','qq_official_endpoints','qq_official_owner_grants','admin_accounts','qq_official_subagent_dispatches','subagent_tasks'):
   before={table:(0,0,0)};after={table:(1,0,0)}
   self.assertIn('qq-official',_changed_database_resources(before,after))
