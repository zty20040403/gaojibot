import unittest
from prometheus_client import CollectorRegistry,generate_latest
from src.plugins.ai_chat.platform_types import EndpointKey,ActorKey
from src.plugins.ai_chat.adapters.qq_official.official_metrics import official_metrics
from tests import test_qq_official_sender as original
from tests import qq_official_outbox_fixture as fixtures
from tests.qq_official_admin_identity_fixture import IdentityFixture

class SenderMetricsTests(IdentityFixture,unittest.IsolatedAsyncioTestCase):
 make_sender=original.OfficialSenderTests.make_sender
 ensure_current=original.OfficialSenderTests.ensure_current
 enqueue=original.OfficialSenderTests.enqueue
 window=fixtures.OutboxFixture.window
 query=IdentityFixture.rows
 async def asyncSetUp(self):
  self.endpoint=EndpointKey(self.account,'group','TEST_GROUP');self.actor=ActorKey(self.account,'group_member','TEST_GROUP','TEST_MEMBER')
  with self.tx() as c:
   self.store._endpoint(c,self.endpoint,self.now)
   c.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(self.endpoint.key,))
  await original.OfficialSenderTests.asyncSetUp(self)
  self.registry=CollectorRegistry();self.metrics=official_metrics(self.registry);self.sender.observer=self.metrics
 async def asyncTearDown(self):await original.OfficialSenderTests.asyncTearDown(self)
 async def test_actual_physical_success_counted_once_not_poll_or_receipt_replay(self):
  delivery=self.enqueue();self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
  before=generate_latest(self.registry).decode()
  self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
  self.assertEqual(before,generate_latest(self.registry).decode());self.assertEqual(len(self.fake.received),1)
  self.assertIn('mode="passive",result="started"} 1.0',before)
  self.assertIn('mode="passive",result="accepted"} 1.0',before)
 async def test_actual_lost_response_is_unknown_and_never_counted_as_acceptance(self):
  delivery=self.enqueue();self.fake.drop_next_response=True
  self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
  self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
  output=generate_latest(self.registry).decode()
  self.assertIn('mode="passive",result="unknown"} 1.0',output)
  self.assertNotIn('result="accepted"',output);self.assertEqual(len(self.fake.received),1)
 async def test_metric_sink_failure_cannot_cancel_or_retry_the_real_send(self):
  class Broken:
   def observe(self,*args,**kwargs):raise RuntimeError('TEST_METRIC_FAILURE')
  self.sender.observer=Broken();delivery=self.enqueue()
  self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
  self.assertEqual(len(self.fake.received),1)
 async def test_lowest_physical_fence_denial_records_no_http_attempt_metric(self):
  from src.qq_official.http import scoped_request_guard
  from src.qq_official.errors import QQAPIError
  delivery=self.enqueue()
  async def deny(stage):
   if stage=='request':raise QQAPIError('source_denied',accepted=False,stage='send')
  # Prepared-token retrieval is permitted; the actual message request is denied.
  await self.rest.prepare_send(self.endpoint.kind,self.endpoint.native_conversation_id)
  with scoped_request_guard(deny):await self.sender.send_delivery(delivery)
  self.assertEqual(len(self.fake.received),0)
  self.assertNotIn('gaoji_qq_official_send_total{',generate_latest(self.registry).decode())
