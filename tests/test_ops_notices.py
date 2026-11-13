"""Local FakePlatform receipts, ephemeral storage and live owner revocation."""
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.ops_notices import OpsNotices
from src.plugins.ai_chat.adapters.qq_official.send_types import OutboxInvariantError
from src.plugins.ai_chat.adapters.qq_official.plans import EPHEMERAL_BODY,EPHEMERAL_MANIFEST
from tests.ops_action_fixture import OpsFixture
from tests import test_qq_official_sender as sender_fixture


class OpsNoticeTests(OpsFixture,unittest.IsolatedAsyncioTestCase):
    inbound = sender_fixture.OfficialSenderTests.inbound
    make_sender = sender_fixture.OfficialSenderTests.make_sender
    ensure_current = sender_fixture.OfficialSenderTests.ensure_current
    fast_sleep = sender_fixture.OfficialSenderTests.fast_sleep
    asyncTearDown = sender_fixture.OfficialSenderTests.asyncTearDown

    async def asyncSetUp(self):
        await sender_fixture.OfficialSenderTests.asyncSetUp(self)
        self.fake.add_window('user',self.endpoint.native_conversation_id,'TEST_SOURCE')
        self.fake.proactive_allowed[('user',self.endpoint.native_conversation_id)] = True
        secret = patch.object(type(self.config),'secret',return_value='TEST_SECRET_'*4)
        self.secret = secret.start(); self.addCleanup(secret.stop)
        self.notices = OpsNotices(self.ops,self.outbox,configuration=lambda:self.config)
        self.outbox.ops_authority = self.notices
        with patch('src.plugins.ai_chat.adapters.qq_official.ops_actions.secrets.randbelow',return_value=741852):
            self.proposal = self.request()

    def prepare(self):
        with self.tx() as cursor:
            return self.notices.prepare(cursor,self.proposal)

    def settle(self):
        with self.tx() as cursor:
            return self.notices.settled(cursor,self.proposal.action['action_id'])

    async def test_confirmed_notice_is_private_and_code_never_enters_durable_bodies(self):
        ref = self.prepare()
        outcome = await self.sender.send_delivery(ref.delivery_ids[0])
        self.assertEqual(outcome.status,'committed')
        self.assertEqual(self.fake.received[0]['kind'],'user')
        self.assertIn('批准 741852',self.fake.received[0]['payload']['content'])
        self.assertIn('restart-test',self.fake.received[0]['payload']['content'])
        self.assertEqual(self.settle()['state'],'pending')
        self.assertEqual(self.query('SELECT manifest_json FROM delivery_plans')[0]['manifest_json'],EPHEMERAL_MANIFEST)
        self.assertEqual(self.query('SELECT body_json FROM deliveries')[0]['body_json'],EPHEMERAL_BODY)
        self.assertIsNone(self.query('SELECT wire_payload_json FROM delivery_attempts')[0]['wire_payload_json'])
        for message in self.query('SELECT body_json,message_kind FROM messages WHERE native_message_id<>?',('TEST_SOURCE',)):
            self.assertNotIn('741852',message['body_json']); self.assertEqual(message['message_kind'],'command')
        with self.tx() as cursor,self.assertRaises(OutboxInvariantError):
            self.notices.prepare(cursor,self.request())

    async def test_lost_receipt_is_not_success_and_same_notice_never_sends_twice(self):
        ref = self.prepare(); self.fake.drop_next_response = True
        result = await self.sender.send_delivery(ref.delivery_ids[0])
        self.assertEqual(result.status,'ambiguous')
        self.assertEqual(self.settle()['state'],'cancelled')
        self.assertEqual(self.settle()['error_code'],'ops_notice_unconfirmed')
        await self.sender.send_delivery(ref.delivery_ids[0])
        self.assertEqual(len(self.fake.received),1)

    async def test_new_outbox_cannot_recover_or_resend_a_lost_ephemeral_code(self):
        ref = self.prepare(); self.outbox.forget_ephemeral(ref.delivery_ids[0])
        self.assertEqual((await self.sender.send_delivery(ref.delivery_ids[0])).status,'failed')
        self.assertEqual(self.settle()['state'],'cancelled'); self.assertEqual(self.fake.received,[])

    async def test_stopping_cannot_be_bypassed_by_the_ephemeral_notice(self):
        ref = self.prepare()
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
        self.assertEqual((await self.sender.send_delivery(ref.delivery_ids[0])).status,'cancelled')
        self.assertEqual(self.settle()['state'],'cancelled'); self.assertEqual(self.fake.received,[])

    async def test_revocation_after_reservation_blocks_network(self):
        ref = self.prepare()
        reservation = self.outbox.prepare(self.token,ref.delivery_ids[0],send_mode='passive',msg_id='TEST_SOURCE',msg_seq=1)
        self.query("UPDATE qq_official_owner_grants SET state='revoked',revision=revision+1")
        self.assertEqual(self.outbox.attempt_denial(self.token,reservation),'ops_notice_source_unavailable')
        self.assertEqual(self.settle()['state'],'cancelled')
        self.assertEqual(self.fake.received,[])

    async def test_credential_rotation_invalidates_waiting_notice(self):
        ref = self.prepare(); self.secret.return_value = 'TEST_ROTATED_SECRET_'*3
        self.assertEqual((await self.sender.send_delivery(ref.delivery_ids[0])).status,'failed')
        self.assertEqual(self.settle()['state'],'cancelled'); self.assertEqual(self.fake.received,[])

    async def test_modified_question_or_source_is_rejected_before_enqueue(self):
        self.proposal.action['parameters_json'] = '{"unit":"TEST_OTHER.service"}'
        with self.assertRaisesRegex(PermissionError,'ops_intent_source_changed'):
            self.prepare()
        self.assertEqual(self.query('SELECT plan_id FROM delivery_plans'),[])
