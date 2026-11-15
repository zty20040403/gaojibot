"""Actual TxA and one feedback send against loopback FakePlatform, not real QQ."""
import json
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.link_feedback import LinkFeedback,feedback_rendered
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.send_types import OutboxInvariantError
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_link_notices as fixture,test_qq_official_sender as sender_fixture


class LinkFeedbackTests(fixture.LinkNoticeTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ingress.replies.admission = self.input.admitted; self.outbox.admission = self.input.admitted
        self.feedback = LinkFeedback(self.ingress,self.outbox); self.outbox.link_feedback_authority = self.feedback

    def queue(self,text='/link list',role='admin',endpoint=None):
        self.source = self.dispatch(text,role,endpoint)
        with self.tx() as cursor:self.ref = self.feedback.prepare(cursor,self.token,self.source.inbox_id)
        return self.ref.delivery_ids[0]

    def enqueue(self):return self.queue()
    def again(self):
        with self.tx() as cursor:return self.feedback.prepare(cursor,self.token,self.source.inbox_id)

    async def test_single_actual_feedback_has_only_original_outcome_and_no_durable_text(self):
        delivery = self.queue(); self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertIn('本会话涉及的连接',self.fake.received[0]['payload']['content'])
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed'); self.assertIsNone(self.again())
        self.assertEqual(len(self.fake.received),1)
        for table in ('qq_official_inbound_events','delivery_plans','deliveries','delivery_attempts','messages'):
            self.assertNotIn('/link list',repr(self.query('SELECT * FROM '+table)))
        self.assertIsNone(self.query('SELECT wire_payload_json FROM delivery_attempts')[0]['wire_payload_json'])
        self.assertEqual(json.loads(self.query('SELECT body_json FROM messages')[0]['body_json'])['nodes'],[])

    async def test_later_admin_window_never_replaces_original_passive_message(self):
        delivery = self.queue(); original = self.outbox.get(self.token,delivery)['reply_to_native_message_id']
        self.query("UPDATE qq_official_endpoints SET proactive_state='denied'")
        self.dispatch('TEST_LATER_ADMIN_MESSAGE')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.fake.received[0]['payload']['msg_id'],original)

    async def test_generic_member_denial_is_sent_without_link_identifiers(self):
        delivery = self.queue(role='member')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        text = self.fake.received[0]['payload']['content']
        self.assertIn('没有原认证',text); self.assertNotIn('LC-',text)
        self.assertIsNone(self.query('SELECT authority_hash FROM qq_official_link_replies')[0]['authority_hash'])

    async def test_auth_rate_and_proactive_rejection_are_once_without_fallback(self):
        for status,code in ((401,50002),(429,50002),(400,40034105)):
            delivery = self.queue(); self.fake.next_errors.append((status,code))
            self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
            self.assertEqual(len(self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery,))),1)
            self.assertIsNone(self.again())

    async def test_changed_body_or_other_original_source_cannot_send(self):
        self.source = self.dispatch('/link list'); wrong = render_plan(MessageBody((TextNode(0,'TEST_CHANGED_BODY'),)),self.endpoint)
        with self.assertRaisesRegex(OutboxInvariantError,'source_mismatch'),self.tx() as cursor:
            self.outbox.enqueue(self.token,rendered=wrong,requester=self.actor,logical_key='link-feedback:'+self.source.inbox_id,
                source_scope_key=self.endpoint.scope.key,expires_at=self.now+30,purpose='link_feedback',link_reply_inbox_id=self.source.inbox_id,
                source_native_message_id='TEST_OTHER_SOURCE',transaction_cursor=cursor)
        with self.tx() as cursor:self.ref = self.feedback.prepare(cursor,self.token,self.source.inbox_id)
        delivery = self.ref.delivery_ids[0]; self.outbox._ephemeral[delivery] = wrong
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed'); self.assertEqual(self.fake.received,[])

    async def test_correct_body_cannot_rebind_original_message_or_borrow_passive_window(self):
        accepted = self.dispatch('/link list')
        row = self.query('SELECT * FROM qq_official_link_replies WHERE source_inbox_id=?',(accepted.inbox_id,))[0]
        with self.assertRaisesRegex(OutboxInvariantError,'source_mismatch'),self.tx() as cursor:
            self.outbox.enqueue(self.token,rendered=feedback_rendered(row),requester=self.actor,logical_key='link-feedback:'+accepted.inbox_id,
                source_scope_key=self.endpoint.scope.key,expires_at=self.now+30,purpose='link_feedback',link_reply_inbox_id=accepted.inbox_id,
                source_native_message_id='TEST_OTHER_SOURCE',transaction_cursor=cursor)
        with self.tx() as cursor:ref = self.feedback.prepare(cursor,self.token,accepted.inbox_id)
        result = self.outbox.prepare(self.token,ref.delivery_ids[0],send_mode='passive',msg_id='TEST_OTHER_SOURCE',msg_seq=1)
        self.assertEqual((result.status,result.reason),('failed','delivery_not_authorized')); self.assertEqual(self.fake.received,[])

    async def test_other_full_account_cannot_read_or_enqueue_original_feedback(self):
        accepted = self.dispatch('/link list'); account = AccountKey(self.account.platform,self.account.app_id,'sandbox')
        self.provision(EndpointKey(account,'group',self.endpoint.native_conversation_id)); token = self.lease(account).acquire()
        with self.assertRaises(PermissionError),self.tx() as cursor:self.feedback.prepare(cursor,token,accepted.inbox_id)
        self.assertEqual(self.query("SELECT * FROM delivery_plans WHERE purpose='link_feedback'"),[])

    async def test_role_withdrawal_between_claim_and_http_stops_feedback(self):
        delivery = self.queue(); original = self.outbox.prepare
        def prepare(*args,**kwargs):
            result = original(*args,**kwargs); self.now += 1; self.dispatch('TEST_DEMOTION','member'); return result
        with patch.object(self.outbox,'prepare',side_effect=prepare):
            self.assertEqual((await self.sender.send_delivery(delivery)).reason,'link_feedback_source_unavailable')
        self.assertEqual(self.fake.received,[])

    async def test_lost_body_and_unknown_platform_receipt_do_not_replay(self):
        delivery = self.queue(); self.outbox.forget_ephemeral(delivery)
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed'); self.assertIsNone(self.again())
        self.assertEqual(self.fake.received,[])
        delivery = self.queue(); self.fake.drop_next_response = True
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous'); self.assertIsNone(self.again())
        await self.sender.stop(); self.sender = self.make_sender()
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous'); self.assertEqual(len(self.fake.received),1)

    async def test_queue_expiry_and_lower_level_retry_or_plain_request_cannot_replay(self):
        delivery = self.queue(); self.now += 31
        self.assertNotIn(delivery,self.outbox.list_due(self.token)); self.assertIsNone(self.again())
        delivery = self.queue()
        self.assertEqual(self.outbox.fail(self.token,delivery,'authentication',retry=True,plain=True).status,'failed')
        self.assertEqual(self.outbox.get(self.token,delivery)['variant'],'primary'); self.assertIsNone(self.again())
        self.assertEqual(self.fake.received,[])

    async def test_repeated_cancel_drains_real_accepted_receipt(self):
        await sender_fixture.OfficialSenderTests.test_repeated_cancellation_drains_accepted_receipt_through_finalization(self)

    async def test_explicit_feedback_does_not_clear_or_obey_chat_stop(self):
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
        self.assertEqual((await self.sender.send_delivery(self.queue())).status,'committed')
        self.assertTrue(self.query('SELECT stopped FROM conversation_settings')[0]['stopped'])

    async def test_default_private_owner_feedback_uses_own_window_and_regrant_does_not_revive(self):
        private = EndpointKey(self.account,'private','TEST_FEEDBACK_OWNER')
        self.input.apply_configuration(self.token,(private.native_conversation_id,))
        delivery = self.queue(endpoint=private)
        self.dispatch('TEST_LATER_PRIVATE_MESSAGE',endpoint=private)
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.fake.received[0]['payload']['msg_id'],self.outbox.get(self.token,delivery)['reply_to_native_message_id'])
        delivery = self.queue(endpoint=private)
        self.input.apply_configuration(self.token,()); self.input.apply_configuration(self.token,(private.native_conversation_id,))
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed'); self.assertEqual(len(self.fake.received),1)

    async def test_replacement_lease_cannot_restore_original_pending_feedback(self):
        delivery = self.queue(); await self.sender.stop()
        self.account_lease.close(); self.token = self.lease().acquire(); self.sender = self.make_sender()
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed'); self.assertEqual(self.fake.received,[])

    async def test_actual_receipt_after_role_revocation_still_records_accepted_fact(self):
        delivery = self.queue(); original = self.rest.send_message
        async def send(*args,**kwargs):
            response = await original(*args,**kwargs); self.now += 1; self.dispatch('TEST_LATE_DEMOTION','member'); return response
        with patch.object(self.rest,'send_message',side_effect=send):
            self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertIsNotNone(self.query('SELECT applied_at FROM delivery_receipts')[0]['applied_at']); self.assertEqual(len(self.fake.received),1)

    async def test_large_authorized_result_is_bounded_and_explicit_about_hidden_rows(self):
        accepted = self.dispatch('/link list')
        row = dict(self.query('SELECT * FROM qq_official_link_replies WHERE source_inbox_id=?',(accepted.inbox_id,))[0])
        entries = [dict(link_id=f'{i:032x}',direction='outgoing',peer='群会话 '+f'{i:012x}',state='active',generation=9223372036854775807) for i in range(32)]
        row['entries_json'] = json.dumps(entries); plan = feedback_rendered(row)
        self.assertEqual((plan.status,len(plan.parts)),('ready',1)); self.assertLessEqual(len(plan.parts[0].canonical_text),2000)
        self.assertIn('未显示',plan.parts[0].canonical_text)


for name in dir(fixture.LinkNoticeTests):
    if name.startswith('test_') and name not in LinkFeedbackTests.__dict__:setattr(LinkFeedbackTests,name,None)
