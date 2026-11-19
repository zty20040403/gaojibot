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
 async def test_actual_checkpoint_and_endpoint_without_console_identity(self):
  self.receive('TEST_MESSAGE')
  value=await self.observer.details(section='bindings')
  self.assertEqual(value['checkpoint']['durable_seq'],1);self.assertTrue(value['checkpoint']['session_present'])
  self.assertEqual(value['checkpoint']['fencing_epoch'],self.token.epoch)
  item=value['items'][0];self.assertEqual(item['kind'],'private');self.assertNotIn('admin_binding',item)
  encoded=json.dumps(value)
  for secret in (self.account.key,self.owner,self.session,self.auth.account_id,self.auth.session_hash,'TEST_MESSAGE','TEST_NATIVE_1'):
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
 async def test_read_only_details_survive_loss_of_live_authority(self):
  await self.calls(self.lease.close)
  for section in ('bindings','executions','deliveries'):
   self.assertTrue(self.reader.read(section=section)['checkpoint']['session_present'])
 async def test_real_ingest_execution_deadline_and_safe_reason(self):
  from src.plugins.ai_chat.adapters.qq_official.inbox import record_pending_inbox
  from src.plugins.ai_chat.adapters.qq_official.turns import claim_execution
  from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
  from src.plugins.ai_chat.ledger import MessageLedger
  ledger=MessageLedger(self.database);self.addCleanup(ledger.close)
  receipt=self.receive('SECRET_MODEL_BODY https://private/?token=TEST_SECRET')
  with self.tx() as c:record_pending_inbox(c,inbox_id=receipt.inbox_id,lease_token=self.token,ledger=ledger,epoch_guard=assert_epoch,admission=lambda *args:True,now=self.now)
  with self.tx() as c:
   result=claim_execution(c,token=self.token,inbox_id=receipt.inbox_id,now=self.now,deadline_at=self.now+600,admission=lambda *args:True)
   c.execute("UPDATE qq_official_turn_executions SET reason_code='private_sentinel' WHERE execution_id=?",(result.handle.execution_id,))
  value=self.reader.read(section='executions');item=value['items'][0]
  self.assertEqual(item['state'],'prepared');self.assertEqual(item['deadline_at'],self.now+600);self.assertEqual(item['reason'],'other')
  for private in ('SECRET_MODEL_BODY','https://private','private_sentinel',result.handle.execution_id,self.owner):self.assertNotIn(private,json.dumps(value))
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
 fast_sleep=sender_support.SenderMetricsTests.fast_sleep
 inbound=sender_support.SenderMetricsTests.inbound
 make_sender=sender_support.SenderMetricsTests.make_sender
 ensure_current=sender_support.SenderMetricsTests.ensure_current
 enqueue=sender_support.SenderMetricsTests.enqueue
 window=sender_support.SenderMetricsTests.window
 query=sender_support.SenderMetricsTests.query
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
  for table in ('delivery_receipts','delivery_attempts','qq_official_endpoints','background_jobs'):
   before={table:(0,0,0)};after={table:(1,0,0)}
   self.assertIn('qq-official',_changed_database_resources(before,after))
