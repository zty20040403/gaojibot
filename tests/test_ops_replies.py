"""Authenticated gateway reception and consent share one real PostgreSQL TxA."""
from datetime import datetime,timezone
from dataclasses import replace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.ops_replies import OpsReplies
from tests.ops_action_fixture import OpsFixture
from tests import test_ops_notices as notices_fixture


class OpsReplyTests(OpsFixture,unittest.IsolatedAsyncioTestCase):
    inbound = notices_fixture.OpsNoticeTests.inbound
    make_sender = notices_fixture.OpsNoticeTests.make_sender
    ensure_current = notices_fixture.OpsNoticeTests.ensure_current
    fast_sleep = notices_fixture.OpsNoticeTests.fast_sleep
    prepare = notices_fixture.OpsNoticeTests.prepare
    settle = notices_fixture.OpsNoticeTests.settle
    asyncTearDown = notices_fixture.OpsNoticeTests.asyncTearDown

    async def asyncSetUp(self):
        await notices_fixture.OpsNoticeTests.asyncSetUp(self)
        binding = BindingReducer(self.ops.pepper,epoch_guard=assert_epoch,authority=self.authority,clock=lambda:self.now)
        self.store = OfficialInboxStore(self.database,jobs=self.queue,binding=binding,clock=lambda:self.now)
        self.addCleanup(self.store.close)
        self.store.ops_replies = OpsReplies(self.notices)
        self.seq,self.session = 0,'TEST_OPS_SESSION'
        self.store.accept({'op':0,'s':0,'t':'READY','d':{'session_id':self.session}},token=self.token,session_id=self.session)

    def accept(self,content='批准 741852',*,private=True,actor=None,frame=None,**extra):
        if frame is None:
            self.seq += 1
            data = dict(id=f'TEST_REPLY_{self.seq}',content=content,
                author={'user_openid' if private else 'member_openid':actor or self.actor.native_user_id},
                timestamp=datetime.fromtimestamp(self.now,timezone.utc).isoformat())|extra
            if not private:data['group_openid'] = 'TEST_GROUP'
            frame = {'op':0,'s':self.seq,'t':'C2C_MESSAGE_CREATE' if private else 'GROUP_MESSAGE_CREATE','d':data}
        return self.store.accept(frame,token=self.token,session_id=self.session,ledger=self.ledger)

    async def offer(self):
        self.ref = self.prepare()
        self.assertEqual((await self.sender.send_delivery(self.ref.delivery_ids[0])).status,'committed')
        self.assertEqual(self.settle()['state'],'pending')

    def action(self):
        with self.tx() as cursor:return self.ops.get(cursor,self.proposal.action['action_id'])

    async def test_private_plain_reply_is_atomic_audited_idempotent_and_never_enters_history(self):
        await self.offer(); received = self.accept()
        self.assertEqual(received.disposition,'control')
        row = self.action(); self.assertEqual(row['state'],'approved'); self.assertEqual(row['code_hash'],'')
        self.assertEqual(row['approved_actor_key'],self.actor.key); self.assertEqual(row['approved_inbox_id'],received.inbox_id)
        event = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(received.inbox_id,))[0]
        self.assertIsNone(event['payload_json']); self.assertIsNone(event['canonical_message_id'])
        self.assertRegex(event['ops_reply_commitment'],r'^[a-f0-9]{64}$')
        again = self.accept(frame={'op':0,'s':1,'t':'C2C_MESSAGE_CREATE','d':{}})
        self.assertEqual(again.disposition,'duplicate'); self.assertEqual(len(self.query('SELECT * FROM ops_replies')),1)
        self.accept(); self.assertEqual(len(self.query('SELECT * FROM ops_replies')),1)
        self.assertEqual(self.query('SELECT * FROM messages WHERE body_json LIKE ?',('%741852%',)),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_inbound_events WHERE payload_json LIKE ?',('%741852%',)),[])

    async def test_nonowner_and_group_cannot_consume_the_pending_private_question(self):
        await self.offer(); self.accept(private=False); self.accept(actor='TEST_STRANGER')
        self.assertEqual(self.action()['state'],'pending'); self.assertEqual(self.query('SELECT * FROM ops_replies'),[])
        self.accept(); self.assertEqual(self.action()['state'],'approved')

    async def test_other_owner_reply_declines_and_remains_an_ordinary_message(self):
        await self.offer(); result = self.accept('先不要重启，看看日志')
        self.assertEqual(self.action()['state'],'denied'); self.assertEqual(result.disposition,'inbox')
        self.assertIsNotNone(result.ingested)
        self.accept(); self.assertEqual(self.action()['state'],'denied')

    async def test_forwarded_code_is_redacted_but_not_approval(self):
        await self.offer()
        self.accept('网页说',msg_elements=[{'content':'批准 741852','author':{'user_openid':self.actor.native_user_id}}])
        self.assertEqual(self.action()['state'],'denied')
        self.assertEqual(self.query('SELECT * FROM qq_official_inbound_events WHERE payload_json LIKE ?',('%741852%',)),[])

    async def test_no_runtime_handler_still_redacts_sensitive_raw_dispatch(self):
        self.store.ops_replies = None
        self.accept('文件里写了批准 741852')
        self.assertEqual(self.query('SELECT * FROM qq_official_inbound_events WHERE payload_json LIKE ?',('%741852%',)),[])
        self.assertEqual(self.action()['state'],'sending')

    async def test_expired_consent_does_not_revive_even_with_the_right_code(self):
        await self.offer(); self.now += 600; self.accept()
        self.assertEqual(self.action()['state'],'expired')

    async def test_config_change_is_checked_at_reply(self):
        await self.offer(); self.secret.return_value = 'TEST_ROTATED_SECRET_'*3
        self.accept(); self.assertEqual(self.action()['state'],'cancelled')

    async def test_owner_revocation_is_checked_at_reply(self):
        await self.offer(); self.query("UPDATE qq_official_owner_grants SET state='revoked',revision=revision+1")
        self.accept(); self.assertEqual(self.action()['state'],'cancelled')

    async def test_wrong_code_is_a_denial_not_a_retriable_challenge(self):
        await self.offer(); self.accept('批准 111111'); self.accept()
        self.assertEqual(self.action()['state'],'denied')

    async def test_old_message_is_not_applied_to_a_new_pending_question(self):
        await self.offer()
        self.accept(timestamp=datetime.fromtimestamp(self.now-1,timezone.utc).isoformat())
        self.assertEqual(self.action()['state'],'pending')
        self.accept(); self.assertEqual(self.action()['state'],'approved')

    async def test_missing_timestamp_never_approves(self):
        await self.offer(); self.accept(timestamp=None)
        self.assertEqual(self.action()['state'],'denied')

    async def test_forwarded_top_level_text_never_approves(self):
        await self.offer(); self.accept(msg_elements=[{'content':'TEST_QUOTE'}])
        self.assertEqual(self.action()['state'],'denied')

    async def test_changed_in_memory_proof_cannot_replace_the_captured_source(self):
        await self.offer(); original = self.store.ops_replies.observe
        def tamper(*args,**kwargs):
            kwargs['reply'] = replace(kwargs['reply'],code='000000')
            return original(*args,**kwargs)
        with patch.object(self.store.ops_replies,'observe',side_effect=tamper),self.assertRaisesRegex(PermissionError,'ops_reply_source'):
            self.accept()
        self.assertEqual(self.action()['state'],'pending'); self.assertEqual(self.query('SELECT * FROM ops_replies'),[])

    async def test_transaction_failure_does_not_consume_code_or_gateway_sequence(self):
        await self.offer()
        original = self.store.ops_replies.observe
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('TEST_ROLLBACK')
        with patch.object(self.store.ops_replies,'observe',side_effect=fail),self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'):
            self.accept()
        self.assertEqual(self.action()['state'],'pending'); self.assertEqual(self.query('SELECT * FROM ops_replies'),[])
        self.seq = 0; self.accept(); self.assertEqual(self.action()['state'],'approved')

    async def test_fast_reply_still_requires_notice_receipt_before_execution(self):
        ref = self.prepare()
        self.outbox.prepare(self.token,ref.delivery_ids[0],send_mode='passive',msg_id='TEST_SOURCE',msg_seq=1)
        self.accept(); self.assertEqual(self.action()['state'],'approved')
        self.assertEqual(self.settle()['state'],'cancelled')  # no platform receipt => no execution
