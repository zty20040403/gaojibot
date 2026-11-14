"""Actual TxA/outbox/RestClient against loopback FakePlatform, not real QQ."""
from dataclasses import replace
from datetime import datetime,timezone
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.link_authority import LinkAuthority
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.adapters.qq_official.link_notices import LinkNotices
from src.plugins.ai_chat.adapters.qq_official.send_types import OutboxInvariantError
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests import test_qq_official_sender as fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class LinkNoticeTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.OfficialSenderTests.asyncTearDown
    ensure_current = fixture.OfficialSenderTests.ensure_current
    make_sender = fixture.OfficialSenderTests.make_sender
    fast_sleep = fixture.OfficialSenderTests.fast_sleep
    inbound = fixture.OfficialSenderTests.inbound

    async def asyncSetUp(self):
        await fixture.OfficialSenderTests.asyncSetUp(self)
        self.jobs = DurableJobStore(self.database); self.addCleanup(self.jobs.close)
        binding = BindingReducer(self.outbox.pepper,epoch_guard=assert_epoch,clock=lambda:self.now)
        self.input = OfficialInboxStore(self.database,jobs=self.jobs,binding=binding,clock=lambda:self.now); self.addCleanup(self.input.close)
        self.ingress = LinkIngress(LinkAuthority(self.outbox.pepper,clock=lambda:self.now,authority=binding.authority))
        self.input.link_ingress = self.ingress
        self.notices = LinkNotices(self.ingress,self.outbox); self.outbox.link_authority = self.notices
        self.session = 'TEST_LINK_SEND_SESSION'; self.seq = 0
        self.input.accept({'op':0,'s':0,'t':'READY','d':{'session_id':self.session}},token=self.token,session_id=self.session)

    def dispatch(self,text='/link',role='admin',endpoint=None):
        endpoint = endpoint or self.endpoint
        self.seq += 1; native = 'TEST_LINK_ORIGINAL_'+str(self.seq)
        group = endpoint.kind=='group'
        self.fake.add_window('group' if group else 'user',endpoint.native_conversation_id,native)
        frame = {'op':0,'s':self.seq,'t':'GROUP_AT_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE','id':'TEST_LINK_SEND_EVENT_'+str(self.seq),
            'd':{'id':native,'content':text,'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),
                'author':{'member_openid':self.actor.native_user_id,'member_role':role} if group else {'user_openid':endpoint.native_conversation_id}}}
        if group:frame['d']['group_openid'] = endpoint.native_conversation_id
        return self.input.accept(frame,token=self.token,session_id=self.session)

    def queue(self,endpoint=None):
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):source = self.dispatch(endpoint=endpoint)
        with self.tx() as cursor:ref = self.notices.prepare(cursor,self.token,source._link_input)
        self.source,self.ref = source,ref
        return ref.delivery_ids[0]

    def row(self):return self.query('SELECT * FROM qq_official_link_challenges WHERE notice_plan_id=?',(self.ref.plan_id,))[0]
    def enqueue(self):return self.queue()

    async def test_confirmed_code_is_one_ephemeral_original_endpoint_effect_without_durable_plaintext(self):
        delivery = self.queue(); self.assertEqual(self.row()['delivery_state'],'pending')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('issued','committed'))
        self.assertTrue(self.row()['delivery_receipt_hash']); self.assertIn('/link 723891',self.fake.received[0]['payload']['content'])
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed'); self.assertEqual(len(self.fake.received),1)
        for table in ('qq_official_inbound_events','delivery_plans','deliveries','delivery_attempts','messages'):
            self.assertNotIn('723891',repr([dict(row) for row in self.query('SELECT * FROM '+table)]))
        self.assertIsNone(self.query('SELECT wire_payload_json FROM delivery_attempts')[0]['wire_payload_json'])
        with self.assertRaises(OutboxInvariantError),self.tx() as cursor:self.notices.prepare(cursor,self.token,self.ingress.reducer.result(cursor,self.row()))

    async def test_new_human_window_is_never_borrowed_for_original_pending_code(self):
        delivery = self.queue(); original = self.query('SELECT reply_to_native_message_id FROM deliveries')[0]['reply_to_native_message_id']
        self.query("UPDATE qq_official_endpoints SET proactive_state='denied'")
        self.dispatch('TEST_LATER_ADMIN_MESSAGE')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.fake.received[0]['payload']['msg_id'],original)

    async def test_private_peer_cannot_issue_but_configured_owner_uses_own_original_window(self):
        private = EndpointKey(self.account,'private','TEST_LINK_PRIVATE'); self.provision(private)
        self.assertEqual(self.dispatch(endpoint=private)._link_input.status,'denied')
        self.input.apply_configuration(self.token,(private.native_conversation_id,))
        delivery = self.queue(private)
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.fake.received[0]['kind'],'user')
        self.assertEqual(self.fake.received[0]['payload']['msg_id'],self.outbox.get(self.token,delivery)['reply_to_native_message_id'])

    async def test_explicit_link_control_is_available_when_chat_is_stopped(self):
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
        self.assertEqual((await self.sender.send_delivery(self.queue())).status,'committed')
        self.assertTrue(self.query('SELECT stopped FROM conversation_settings')[0]['stopped'])

    async def test_known_auth_and_rate_rejections_get_one_attempt_and_no_replay(self):
        for status in (401,429):
            delivery = self.queue(); self.fake.next_errors.append((status,50002))
            self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
            self.assertEqual((self.row()['state'],self.row()['delivery_state']),('cancelled','failed'))
            self.assertEqual(len(self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery,))),1)

    async def test_missing_body_or_unknown_receipt_never_replays_after_sender_restart(self):
        delivery = self.queue(); self.outbox.forget_ephemeral(delivery)
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed'); self.assertEqual(self.fake.received,[])
        delivery = self.queue(); self.fake.drop_next_response = True
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('cancelled','unknown'))
        await self.sender.stop(); self.sender = self.make_sender()
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous'); self.assertEqual(len(self.fake.received),1)

    async def test_mixed_or_modified_code_and_other_original_source_are_rejected(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):source = self.dispatch()
        with self.assertRaisesRegex(OutboxInvariantError,'code_invalid'),self.tx() as cursor:
            self.notices.prepare(cursor,self.token,replace(source._link_input,code='000000'))
        self.source = source
        with self.tx() as cursor:self.ref = self.notices.prepare(cursor,self.token,source._link_input)
        delivery = self.ref.delivery_ids[0]
        with self.assertRaisesRegex(OutboxInvariantError,'link_notice_source_unavailable'),self.tx() as cursor:
            self.outbox.enqueue(self.token,rendered=self.outbox._ephemeral[delivery],requester=self.actor,
                logical_key='link:'+self.row()['challenge_id'],source_scope_key=self.endpoint.scope.key,purpose='link_otp',
                link_challenge_id=self.row()['challenge_id'],expires_at=self.now+30,source_native_message_id='TEST_NEW_SOURCE',transaction_cursor=cursor)
        denied = self.outbox.prepare(self.token,delivery,send_mode='passive',msg_id='TEST_NEW_SOURCE',msg_seq=1)
        self.assertEqual((denied.status,denied.reason),('failed','delivery_not_authorized')); self.assertEqual(self.fake.received,[])

    async def test_fast_bilateral_confirmation_waits_for_real_ledger_receipt(self):
        delivery = self.queue(); target = EndpointKey(self.account,'group','TEST_LINK_TARGET'); self.provision(target)
        self.assertEqual(self.dispatch('/link 723891',endpoint=target)._link_input.status,'confirming')
        self.assertEqual(self.query('SELECT * FROM qq_official_conversation_links'),[])
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        edge = self.query('SELECT * FROM qq_official_conversation_links')[0]
        self.assertEqual((edge['source_endpoint_key'],edge['target_endpoint_key'],edge['state']),(self.endpoint.key,target.key,'active'))

    async def test_expired_queue_and_lower_level_retry_request_cannot_leave_live_code(self):
        delivery = self.queue(); self.now += 31
        self.assertNotIn(delivery,self.outbox.list_due(self.token)); self.assertEqual(self.row()['code_hmac'],'')
        self.assertEqual(self.row()['state'],'cancelled'); self.assertEqual(self.fake.received,[])
        delivery = self.queue()
        self.assertEqual(self.outbox.fail(self.token,delivery,'authentication',retry=True,plain=True).status,'failed')
        self.assertEqual(self.row()['code_hmac'],''); self.assertEqual(self.outbox.get(self.token,delivery)['variant'],'primary')

    async def test_restart_with_captured_but_unapplied_receipt_keeps_fact_not_authorization(self):
        delivery = self.queue(); await self.sender.recover()
        reservation = self.outbox.prepare(self.token,delivery,send_mode='proactive')
        response = await self.rest.send_message(self.endpoint.kind,self.endpoint.native_conversation_id,reservation.payload())
        receipt = self.outbox.capture_receipt(reservation,response)
        self.assertEqual(self.row()['delivery_state'],'pending')
        self.assertIn(receipt,self.outbox.recover(self.token)); self.assertEqual(self.row()['delivery_state'],'unknown')
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('cancelled','committed'))
        self.assertEqual(self.row()['code_hmac'],''); self.assertEqual(len(self.fake.received),1)

    async def test_repeated_cancel_drains_actual_receipt_and_link_fact(self):
        await fixture.OfficialSenderTests.test_repeated_cancellation_drains_accepted_receipt_through_finalization(self)
        self.assertEqual(self.row()['delivery_state'],'committed'); self.assertTrue(self.row()['delivery_receipt_hash'])

    async def test_role_withdrawal_after_reservation_prevents_http(self):
        delivery = self.queue(); original = self.outbox.prepare
        def prepare(*args,**kwargs):
            reservation = original(*args,**kwargs); self.now += 1; self.dispatch('TEST_WITHDRAW_ROLE','member'); return reservation
        with patch.object(self.outbox,'prepare',side_effect=prepare):
            self.assertEqual((await self.sender.send_delivery(delivery)).reason,'link_notice_source_unavailable')
        self.assertEqual(self.fake.received,[]); self.assertEqual(self.row()['state'],'cancelled')

    async def test_accepted_receipt_after_cancellation_remains_true_but_not_authorized(self):
        delivery = self.queue(); original = self.rest.send_message
        async def send(*args,**kwargs):
            response = await original(*args,**kwargs)
            with self.tx() as cursor:self.ingress.reducer.close(cursor,self.row())
            return response
        with patch.object(self.rest,'send_message',side_effect=send):self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('cancelled','committed'))
        self.assertTrue(self.row()['delivery_receipt_hash']); self.assertEqual(len(self.fake.received),1)
