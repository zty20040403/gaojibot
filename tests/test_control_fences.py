"""Real storage fences and loopback HTTP; no production QQ or model calls."""
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import conversation_control as controls, turn_source
from src.plugins.ai_chat.background_jobs import BackgroundJobs
from tests import test_qq_official_sender as sender_base
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.qq_official_turn_fixture import ExecutionFixture


def change(fixture, action):
    with fixture.tx() as cursor:
        return controls.change(cursor,fixture.endpoint,action,now=fixture.now,actor='TEST_ADMIN',authorized=True)


class SourceFenceTests(unittest.TestCase):
    def fixture(self):
        fixture = ExecutionFixture('runTest'); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_late_result_and_old_input_remain_invalid_after_start(self):
        f = self.fixture()
        handle = f.claim().handle
        f.begin(handle)
        change(f,'stop'); change(f,'start')
        with f.tx() as cursor, self.assertRaises(turn_source.ExecutionFenceLost):
            turn_source.assert_execution_current(cursor,**f.args(handle))
        self.assertEqual(f.claim().state,'cancelled')

    def test_commands_still_run_while_stopped(self):
        f = self.fixture(); change(f,'stop')
        f.query("UPDATE qq_official_inbound_events SET turn_text='/status' WHERE inbox_id=?",(f.inbox,))
        self.assertEqual(f.claim(kind='command').state,'prepared')

    def test_cancel_wins_both_start_and_finish_races(self):
        f = self.fixture(); store = BackgroundJobs(f.jobs,clock=lambda:f.now)
        source = dict(account_key=f.account.key,endpoint_key=f.endpoint.key,actor_key=f.actor.key,
            scope_key=f.endpoint.scope.key,source_execution_id=None,canonical_message_id=f.message.canonical_message_id,
            native_message_id='TEST_INPUT',endpoint_generation=1,route_epoch=1)
        with f.tx() as cursor:
            row = store.submit(cursor,kind='TEST_JOB',key='TEST_RACE',source=source,task='TEST',title='',profile='TEST')
        change(f,'stop')
        self.assertIsNone(store.start(row['job_id']))
        store.finish(row['job_id'],state='completed',error='',report='TEST late success')
        self.assertEqual(store.get(row['job_id'])['state'],'cancelled')
        self.assertEqual(store.pending_results(f.account.key),[])


class ControlSenderTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    asyncSetUp = sender_base.OfficialSenderTests.asyncSetUp
    asyncTearDown = sender_base.OfficialSenderTests.asyncTearDown
    ensure_current = sender_base.OfficialSenderTests.ensure_current
    make_sender = sender_base.OfficialSenderTests.make_sender
    enqueue = sender_base.OfficialSenderTests.enqueue
    inbound = sender_base.OfficialSenderTests.inbound
    fast_sleep = sender_base.OfficialSenderTests.fast_sleep
    attempts = sender_base.OfficialSenderTests.attempts

    async def test_restart_and_start_never_release_an_old_pending_plan(self):
        delivery = self.enqueue()
        change(self,'stop')
        await self.sender.stop()
        self.sender = self.make_sender()
        change(self,'start')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'cancelled')
        self.assertEqual(self.fake.received,[])
        self.assertEqual((await self.sender.send_delivery(self.enqueue('TEST_NEW'))).status,'committed')

    async def test_stop_between_claim_and_http_does_not_send(self):
        original = self.outbox.prepare
        def prepare(*args,**kwargs):
            result = original(*args,**kwargs)
            change(self,'stop')
            return result
        delivery = self.enqueue()
        with patch.object(self.outbox,'prepare',side_effect=prepare):
            self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
        self.assertEqual(self.fake.received,[])
        self.assertEqual(self.attempts()[0]['acceptance'],'confirmed_no')

    async def test_stop_after_http_acceptance_keeps_the_real_receipt(self):
        original = self.rest.send_message
        async def send(*args,**kwargs):
            response = await original(*args,**kwargs)
            change(self,'stop')
            return response
        with patch.object(self.rest,'send_message',side_effect=send):
            result = await self.sender.send_delivery(self.enqueue())
        self.assertEqual(result.status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(len(self.query("SELECT * FROM messages WHERE direction='outbound'")),1)
