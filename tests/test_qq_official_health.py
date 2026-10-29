import asyncio,json,unittest
from types import SimpleNamespace
from unittest.mock import patch
from prometheus_client import CollectorRegistry,generate_latest
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.observability import OfficialObservability
from tests.qq_official_admin_identity_fixture import IdentityFixture

class HealthTests(IdentityFixture,unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.calls=DatabaseCalls();self.runtime._calls=self.calls;self.runtime.config=SimpleNamespace(enabled=True)
  self.runtime._gateway=SimpleNamespace(health={'state':'ready','message_gap':False,'dispatch_queue_depth':2,'dispatch_queue_limit':64,'rest_error':'TEST_SECRET_URL'})
  self.runtime.sender=SimpleNamespace(_running=True,_stopping=False,reason='ready',_active=set(),_rate_waiters=set(),_unrecorded={})
  self.context=SimpleNamespace(official_runtime=self.runtime);self.registry=CollectorRegistry()
  self.service=OfficialObservability(self.context,registry=self.registry,clock=lambda:self.now)
 async def asyncTearDown(self):await self.service.close();await self.calls.close()
 async def test_real_lease_required_ready_sender_not_process_state(self):
  self.runtime.state='active'
  self.assertTrue((await self.service.sample())['healthy'])
  self.runtime.sender._running=False
  self.assertEqual((await self.service.sample())['reason'],'sender_unavailable')
  self.runtime.sender._running=True;self.runtime._gateway.health['state']='connecting'
  self.assertEqual((await self.service.sample())['reason'],'gateway_unavailable')
  self.runtime._gateway.health['state']='ready';await self.calls(self.lease.close)
  self.assertEqual((await self.service.sample())['reason'],'lease_unavailable')
 async def test_sample_failure_stale_and_private_values_never_reported(self):
  snapshot=await self.service.sample();encoded=json.dumps(snapshot)+generate_latest(self.registry).decode()
  for private in (self.account.key,self.owner,'TEST_SECRET_URL',self.token.holder_id):self.assertNotIn(private,encoded)
  with patch.object(self.service,'_read',side_effect=RuntimeError('TEST_CREDENTIAL_URL')):
   snapshot=await self.service.sample()
  self.assertFalse(snapshot['healthy']);self.assertEqual(snapshot['reason'],'diagnostics_unavailable')
  self.assertNotIn('TEST_CREDENTIAL_URL',json.dumps(snapshot))
  self.service._observed_monotonic=0
  self.assertEqual(self.service.snapshot()['reason'],'health_sample_stale')
 async def test_sampling_never_increments_events_and_marks_true_unknown_delivery(self):
  self.service.metrics.observe('send','unknown',mode='passive')
  before=generate_latest(self.registry).decode()
  await self.service.sample();await self.service.sample()
  after=generate_latest(self.registry).decode()
  eventlines=lambda x:[line for line in x.splitlines() if '_total{' in line or '_count{' in line]
  self.assertEqual(eventlines(before),eventlines(after))
 async def test_disabled_does_no_database_io_and_shutdown_invalidates_health(self):
  self.context.official_runtime=None
  with patch.object(self.service,'_read',side_effect=AssertionError('no I/O')):
   self.assertFalse((await self.service.sample())['enabled'])
  self.context.official_runtime=self.runtime;self.assertTrue((await self.service.sample())['healthy'])
  await self.service.close();self.assertFalse(self.service.snapshot()['healthy'])
 async def test_stale_holder_row_after_real_lock_backend_dies_is_unhealthy(self):
  self.assertTrue((await self.service.sample())['healthy'])
  with self.tx() as c:c.execute('SELECT pg_terminate_backend(?)',(self.token.backend_pid,))
  async with asyncio.timeout(2):
   while (await self.service.sample())['healthy']:await asyncio.sleep(.01)
  self.assertEqual(self.service.snapshot()['reason'],'lease_unavailable')
  self.assertIsNotNone(self.rows('SELECT holder_id FROM qq_official_accounts')[0]['holder_id'])
