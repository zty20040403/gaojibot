"""Program speech through real PostgreSQL/outbox and loopback FakePlatform."""
import json
import unittest
from unittest.mock import Mock,patch

from tests import test_qq_official_sender as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.test_vm_sessions import BOOT
from src.plugins.ai_chat.adapters.qq_official.identity_store import snapshot
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions
from src.plugins.ai_chat.adapters.qq_official.program_delivery import say,rendered_say
from src.plugins.ai_chat.adapters.qq_official.send_types import OutboxInvariantError
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions


class ProgramDeliveryTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.OfficialSenderTests.asyncTearDown
    ensure_current = fixture.OfficialSenderTests.ensure_current
    make_sender = fixture.OfficialSenderTests.make_sender
    fast_sleep = fixture.OfficialSenderTests.fast_sleep
    inbound = fixture.OfficialSenderTests.inbound

    async def asyncSetUp(self):
        await fixture.OfficialSenderTests.asyncSetUp(self)
        self.outbox.admission = Mock(return_value=False)
        self.sessions = VMSessions(self.tx,self.token,private_admission=self.outbox.admission,clock=lambda:self.now)
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        self.actions = ProgramActions(self.sessions)

    async def submit(self, key='TEST_REQUEST'):
        return await self.calls(say,self.actions,self.current,generation=0,program='TEST_PROGRAM',request_id=key,text='TEST speech')

    async def test_program_speech_is_proactive_idempotent_and_recorded_without_human_grant(self):
        action,ref = await self.submit()
        result = await self.sender.send_delivery(ref.delivery_ids[0])
        self.assertEqual(result.status,'committed')
        again,duplicate = await self.submit()
        self.assertEqual((action,ref.delivery_ids),(again,duplicate.delivery_ids))
        self.assertFalse(duplicate.created)
        self.assertEqual((await self.sender.send_delivery(duplicate.delivery_ids[0])).status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertNotIn('msg_id',self.fake.received[0]['payload'])
        self.outbox.admission.assert_not_called()
        row = self.query("SELECT * FROM messages WHERE direction='outbound'")[0]
        self.assertEqual((row['sender_native_user_id'],row['sender_display'],row['message_kind']),
            ('TEST_REAL_BOT','程序 TEST_PROGRAM','chat'))
        raw = json.loads(row['raw_event_json'])
        self.assertEqual((raw['by'],raw['program'],raw['program_action_id']),('program','TEST_PROGRAM',action))
        self.assertEqual(self.actions.get(self.current,action)['state'],'completed')
        self.assertEqual(self.query('SELECT used FROM program_quota')[0]['used'],1)

    async def test_platform_denial_never_borrows_a_recent_human_reply_window(self):
        self.fake.proactive_allowed[('group',self.endpoint.native_conversation_id)] = False
        action,ref = await self.submit()
        self.assertEqual((await self.sender.send_delivery(ref.delivery_ids[0])).reason,'proactive_denied')
        self.assertEqual(self.query('SELECT send_mode FROM delivery_attempts'),[{'send_mode':'proactive'}])
        self.assertEqual(self.actions.get(self.current,action)['state'],'failed')
        self.assertEqual(self.query("SELECT * FROM messages WHERE direction='outbound'"),[])
        _,second = await self.submit('TEST_NEXT')
        self.assertEqual((await self.sender.send_delivery(second.delivery_ids[0])).status,'failed')
        self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')),1)

    async def test_lost_response_is_ambiguous_and_duplicate_does_not_resend(self):
        self.fake.drop_next_response = True
        action,ref = await self.submit()
        self.assertEqual((await self.sender.send_delivery(ref.delivery_ids[0])).status,'ambiguous')
        self.assertEqual(self.actions.get(self.current,action)['state'],'ambiguous')
        _,duplicate = await self.submit()
        self.assertEqual((await self.sender.send_delivery(duplicate.delivery_ids[0])).status,'ambiguous')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(self.query("SELECT * FROM messages WHERE direction='outbound'"),[])

    async def test_stop_start_fences_queued_speech(self):
        action,ref = await self.submit()
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,FALSE,1)',(self.endpoint.scope.key,))
        result = await self.sender.send_delivery(ref.delivery_ids[0])
        self.assertEqual((result.status,result.reason),('cancelled','conversation_stopped_or_replaced'))
        self.assertEqual(self.fake.received,[])
        self.assertEqual(self.actions.get(self.current,action)['state'],'cancelled')

    async def test_program_disable_after_claim_is_rechecked_before_http(self):
        _,ref = await self.submit()
        original = self.outbox.prepare
        def prepare(*args,**kwargs):
            result = original(*args,**kwargs)
            self.query('INSERT INTO program_controls(scope_key,program_name,enabled,updated_at) VALUES (?,?,FALSE,?)',
                (self.endpoint.scope.key,'TEST_PROGRAM',self.now))
            return result
        with patch.object(self.outbox,'prepare',side_effect=prepare):
            result = await self.sender.send_delivery(ref.delivery_ids[0])
        self.assertEqual((result.status,result.reason),('failed','program_disabled'))
        self.assertEqual(self.fake.received,[])

    async def test_rotation_rejects_old_program_plan_but_accepted_receipt_stays_true(self):
        action,ref = await self.submit()
        reservation = self.outbox.prepare(self.token,ref.delivery_ids[0],send_mode='proactive')
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        self.assertFalse(self.outbox.attempt_current(self.token,reservation))
        # Explicit local receipt double: accepted external effects are not erased
        # by revocation, just as for ordinary sends. No real QQ acceptance claim.
        receipt = self.outbox.capture_receipt(reservation,{'id':'TEST_ACCEPTED'})
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        self.assertEqual(self.actions.get(self.current,action)['state'],'completed')
        self.assertEqual(json.loads(self.query("SELECT raw_event_json FROM messages WHERE direction='outbound'")[0]['raw_event_json'])['by'],'program')

    async def test_bot_requester_without_proof_and_mutated_program_body_are_rejected(self):
        with self.tx() as cursor:
            bot = snapshot(cursor,self.account).bot_actor
            args = dict(token=self.token,rendered=rendered_say(self.endpoint,'TEST forged'),requester=bot,
                logical_key='TEST_FORGED',source_scope_key=self.endpoint.scope.key,expires_at=self.now+300,
                now=self.now,admission=lambda *_:True)
            with self.assertRaisesRegex(OutboxInvariantError,'invalid_delivery_plan'):
                enqueue_plan(cursor,**args)
        row,_ = self.actions.reserve(self.current,generation=0,kind='say',program='TEST_P',request_id='TEST_R',payload={'text':'TEST original'})
        args.update(logical_key='program:'+row['action_id'],program_action_id=row['action_id'],
            rendered=rendered_say(self.endpoint,'TEST original'),purpose='unsolicited',source_generation=0)
        for changes in ({'rendered':rendered_say(self.endpoint,'TEST forged')},{'requester':self.actor},
                {'source_scope_key':'TEST_HISTORY_ALIAS'}):
            with self.tx() as cursor,self.assertRaisesRegex(OutboxInvariantError,'program_source_mismatch'):
                enqueue_plan(cursor,**(args|changes))
        self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])
