"""Real PG and loopback FakePlatform: notices are bot effects, not program speech."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from src.plugins.ai_chat import daily_budgets
from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.adapters.qq_official.budget_notice_worker import BudgetNoticeWorker
from tests import test_qq_official_sender as fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class BudgetNoticeDeliveryTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.OfficialSenderTests.asyncTearDown
    ensure_current = fixture.OfficialSenderTests.ensure_current
    make_sender = fixture.OfficialSenderTests.make_sender
    fast_sleep = fixture.OfficialSenderTests.fast_sleep
    inbound = fixture.OfficialSenderTests.inbound

    async def asyncSetUp(self):
        await fixture.OfficialSenderTests.asyncSetUp(self)
        self.outbox.admission = Mock(return_value=False)
        self.runtime = SimpleNamespace(store=SimpleNamespace(clock=lambda:self.now,transaction=self.tx),outbox=self.outbox,
            _calls=self.calls,sender=self.sender,logger=Mock())
        self.worker = BudgetNoticeWorker(self.runtime,self.token)
        self.configure('0')

    def configure(self,value):
        with self.tx() as cursor:change(cursor,self.endpoint,'daily_budget_usd',value,actor='TEST_ADMIN',now=self.now)

    def day(self):
        return dict(self.query('SELECT * FROM conversation_budget_days WHERE scope_key=? ORDER BY day_start DESC',
            (self.endpoint.scope.key,))[0])

    def delivery(self):
        return self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(self.day()['notice_plan_id'],))[0]['delivery_id']

    def queue(self):
        self.assertTrue(self.worker.reconcile())
        return self.delivery()

    async def test_once_only_proactive_receipt_is_a_bot_notice_without_human_or_program_grant(self):
        delivery = self.queue()
        self.assertEqual(self.day()['notice_state'],'sending')  # Queued, not confirmed.
        self.assertEqual(self.fake.received,[])
        self.assertFalse(self.worker.reconcile())
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.day()['notice_state'],'committed')
        self.assertFalse(self.worker.reconcile())
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertNotIn('msg_id',self.fake.received[0]['payload'])
        self.outbox.admission.assert_not_called()
        message = self.query("SELECT * FROM messages WHERE direction='outbound'")[0]
        self.assertEqual((message['sender_native_user_id'],message['sender_display']),('TEST_REAL_BOT','Bot'))
        raw = json.loads(message['raw_event_json'])
        self.assertEqual(raw['system_notice'],'daily_budget'); self.assertNotEqual(raw.get('by'),'program')
        self.assertEqual(self.query('SELECT * FROM program_quota'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_program_actions'),[])

    async def test_platform_denial_cannot_fall_back_to_fresh_human_message_or_try_twice(self):
        self.fake.proactive_allowed[('group',self.endpoint.native_conversation_id)] = False
        delivery = self.queue()
        self.assertEqual((await self.sender.send_delivery(delivery)).reason,'proactive_denied')
        self.assertEqual(self.day()['notice_state'],'failed')
        self.assertFalse(self.worker.reconcile())
        self.assertEqual(self.query('SELECT send_mode FROM delivery_attempts'),[dict(send_mode='proactive')])
        self.assertEqual(self.query("SELECT * FROM messages WHERE direction='outbound'"),[])

    async def test_known_auth_and_rate_rejection_still_get_exactly_one_attempt_per_day(self):
        for status in (401,429):
            self.fake.next_errors.append((status,50002))
            delivery = self.queue()
            self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
            self.assertEqual(self.day()['notice_state'],'failed')
            self.assertFalse(self.worker.reconcile())
            self.assertEqual(len(self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery,))),1)
            self.now = daily_budgets.interval(self.now)[1]; self.configure('0')

    async def test_lost_receipt_stays_unknown_after_sender_restart_and_budget_restore(self):
        self.fake.drop_next_response = True
        delivery = self.queue()
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
        self.assertEqual(self.day()['notice_state'],'unknown')
        await self.sender.stop(); self.sender = self.make_sender(); self.runtime.sender = self.sender
        self.configure('off'); self.configure('0')
        self.assertFalse(self.worker.reconcile())
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(self.query("SELECT * FROM messages WHERE direction='outbound'"),[])

    async def test_stop_and_start_before_claim_never_reauthorize_original_pending_notice(self):
        self.query('UPDATE conversation_settings SET stop_generation=2 WHERE scope_key=?',(self.endpoint.scope.key,))
        self.assertFalse(self.worker.reconcile())
        self.assertEqual(self.day()['notice_state'],'cancelled'); self.assertIsNone(self.day()['notice_plan_id'])
        self.assertEqual(self.fake.received,[])

    async def test_queued_notice_does_not_survive_stop_start_or_host_epoch_rotation(self):
        delivery = self.queue()
        await self.sender.stop(); self.account_lease.close()
        self.token = self.lease().acquire()
        self.sender = self.make_sender(); self.runtime.sender = self.sender
        self.worker = BudgetNoticeWorker(self.runtime,self.token)
        self.assertFalse(self.worker.reconcile())
        self.assertEqual(self.day()['notice_state'],'cancelled')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'cancelled')
        self.assertEqual(self.fake.received,[])

    async def test_budget_restore_after_claim_is_checked_before_http(self):
        delivery = self.queue()
        original = self.outbox.prepare
        def prepare(*args,**kwargs):
            result = original(*args,**kwargs)
            self.configure('off'); self.configure('0')
            return result
        with patch.object(self.outbox,'prepare',side_effect=prepare):
            result = await self.sender.send_delivery(delivery)
        self.assertEqual((result.status,result.reason),('failed','budget_notice_source_unavailable'))
        self.assertEqual(self.day()['notice_state'],'failed')
        self.assertFalse(self.worker.reconcile()); self.assertEqual(self.fake.received,[])

    async def test_platform_accepted_receipt_remains_true_even_when_budget_changes_before_finalize(self):
        delivery = self.queue()
        original = self.rest.send_message
        async def send(*args,**kwargs):
            result = await original(*args,**kwargs)
            self.configure('off'); self.configure('0')
            return result
        with patch.object(self.rest,'send_message',side_effect=send):
            self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.day()['notice_state'],'committed')
        self.assertEqual(len(self.fake.received),1); self.assertFalse(self.worker.reconcile())
        self.assertIsNotNone(self.query('SELECT applied_at FROM delivery_receipts')[0]['applied_at'])

    def enqueue(self):return self.queue()

    async def test_repeated_cancel_still_finalizes_real_notice_receipt_and_day_marker(self):
        await fixture.OfficialSenderTests.test_repeated_cancellation_drains_accepted_receipt_through_finalization(self)
        self.assertEqual(self.day()['notice_state'],'committed')

    async def test_next_beijing_day_can_issue_a_new_notice_without_replaying_yesterday(self):
        first = self.queue()
        self.assertEqual((await self.sender.send_delivery(first)).status,'committed')
        self.now = daily_budgets.interval(self.now)[1]; self.configure('0')
        second = self.queue(); self.assertNotEqual(first,second)
        self.assertEqual((await self.sender.send_delivery(second)).status,'committed')
        self.assertEqual(len(self.fake.received),2)
        self.assertEqual(len(self.query('SELECT * FROM conversation_budget_days')),2)
        self.assertFalse(self.worker.reconcile())


class PrivateBudgetNoticeTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    endpoint_kind = 'private'
    asyncTearDown = fixture.OfficialSenderTests.asyncTearDown
    ensure_current = fixture.OfficialSenderTests.ensure_current
    make_sender = fixture.OfficialSenderTests.make_sender
    fast_sleep = fixture.OfficialSenderTests.fast_sleep
    inbound = fixture.OfficialSenderTests.inbound
    configure = BudgetNoticeDeliveryTests.configure
    day = BudgetNoticeDeliveryTests.day
    delivery = BudgetNoticeDeliveryTests.delivery
    queue = BudgetNoticeDeliveryTests.queue

    async def asyncSetUp(self):
        await BudgetNoticeDeliveryTests.asyncSetUp(self)
        self.fake.proactive_allowed[('user',self.endpoint.native_conversation_id)] = True
        self.outbox.admission.return_value = True

    async def test_private_access_is_checked_but_message_author_is_still_bot(self):
        delivery = self.queue()
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertIn('仍可直接私聊提问',self.fake.received[0]['payload']['content'])
        self.assertTrue(self.outbox.admission.called)
        self.assertTrue(all(call.args[2].identity_kind=='c2c_user' for call in self.outbox.admission.call_args_list))
        self.assertEqual(self.query('SELECT requester_actor_key FROM delivery_plans')[0]['requester_actor_key'],
            self.query('SELECT bot_actor_key FROM delivery_attempts')[0]['bot_actor_key'])

    async def test_private_access_revocation_blocks_already_queued_notice(self):
        delivery = self.queue()
        self.outbox.admission.return_value = False
        self.assertEqual((await self.sender.send_delivery(delivery)).reason,'budget_notice_source_unavailable')
        self.assertEqual(self.fake.received,[])
