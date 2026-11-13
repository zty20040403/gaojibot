"""Program wake execution fencing and real outbox results; no model or QQ API."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

from tests import test_program_delivery as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramDenied
from src.plugins.ai_chat.adapters.qq_official.program_execution import ProgramExecution
from src.plugins.ai_chat.adapters.qq_official.program_wakes import ProgramWakes


class ProgramExecutionTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.ProgramDeliveryTests.asyncTearDown
    ensure_current = fixture.ProgramDeliveryTests.ensure_current
    make_sender = fixture.ProgramDeliveryTests.make_sender
    fast_sleep = fixture.ProgramDeliveryTests.fast_sleep
    inbound = fixture.ProgramDeliveryTests.inbound

    async def asyncSetUp(self):
        await fixture.ProgramDeliveryTests.asyncSetUp(self)
        self.wakes = ProgramWakes(self.actions)
        self.execution = ProgramExecution(self.wakes)

    def submit(self, key='TEST_REQUEST', program='TEST_PROGRAM'):
        return self.wakes.submit(self.current,generation=0,program=program,request_id=key,text='TEST wake')[0]

    def delivery(self, action):
        return self.query('SELECT delivery_id FROM deliveries WHERE plan_id=(SELECT plan_id FROM qq_official_program_actions WHERE action_id=?)',
            (action,))[0]['delivery_id']

    def merged(self):
        self.query('INSERT INTO conversation_settings(scope_key,program_wake_limit) VALUES (?,1)',(self.current.scope_key,))
        self.submit()
        first,second = self.submit('TEST_A','TEST_A'),self.submit('TEST_B','TEST_B')
        self.now = (self.now//3600+1)*3600
        run = self.execution.claim(first['wake_id'])
        self.execution.answer(run,'TEST merged answer')
        reply = self.execution.publish(run)
        return first,second,run,reply

    async def test_command_notification_is_atomic_and_kill_blocks_its_already_saved_answer(self):
        from src.plugins.ai_chat.adapters.qq_official.background_commands import BackgroundCommands
        from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
        commands = BackgroundCommands(self.actions.sessions)
        command,_ = commands.reserve(self.current,generation=(0,0),program='TEST_PROGRAM',request_id='TEST_COMMAND',argv=['true'])
        options = dict(generation=0,program='TEST_PROGRAM',request_id='command:'+command['command_id'],
            text='TEST command completed',source_command_id=command['command_id'])
        with self.assertRaisesRegex(ProgramDenied,'notification_unavailable'):
            self.wakes.submit(self.current,**options)
        self.query("UPDATE qq_official_background_commands SET state='completed',finished_at=?,exit_code=0",(self.now,))
        with self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'):
            with self.tx() as cursor:
                self.wakes.submit(self.current,transaction_cursor=cursor,**options)
                raise RuntimeError('TEST_ROLLBACK')
        self.assertEqual(self.query('SELECT * FROM program_quota'),[])
        self.assertIsNone(self.query('SELECT notification_action_id FROM qq_official_background_commands')[0]['notification_action_id'])
        action,created = self.wakes.submit(self.current,**options)
        self.assertTrue(created)
        self.assertFalse(self.wakes.submit(self.current,**options)[1])
        self.assertEqual(self.query('SELECT notification_action_id FROM qq_official_background_commands')[0]['notification_action_id'],action['action_id'])
        run = self.execution.claim(action['wake_id'])
        self.execution.answer(run,'TEST command answer')
        reply = self.execution.publish(run)
        with self.tx() as cursor:
            change(cursor,self.endpoint,'kill',now=self.now,actor='TEST_ADMIN',authorized=True)
        result = await self.sender.send_delivery(self.delivery(reply))
        self.assertEqual((result.status,result.reason),('failed','background_command_cancelled_or_replaced'))
        self.assertEqual(self.fake.received,[])
        # Ordinary program wakes are not background commands and remain available.
        ordinary = self.submit('TEST_ORDINARY')
        self.assertIsNotNone(self.execution.claim(ordinary['wake_id']))

    async def test_claim_is_single_winner_and_silence_does_not_fabricate_a_qq_message(self):
        action = self.submit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _:self.execution.claim(action['wake_id']),range(2)))
        self.assertEqual(sum(run is not None for run in claimed),1)
        run = next(run for run in claimed if run is not None)
        self.assertEqual(self.actions.get(self.current,action['action_id'])['state'],'running')
        self.execution.answer(run,'[silence]')
        self.assertIsNone(self.execution.publish(run))
        result = self.actions.get(self.current,action['action_id'])
        self.assertEqual((result['state'],result['silent'],result['plan_id']),('completed',True,None))
        self.assertEqual(self.query('SELECT * FROM deliveries'),[])
        self.assertEqual(self.query('SELECT * FROM messages'),[])

    async def test_saved_answer_survives_worker_restart_but_started_model_is_not_replayed(self):
        saved,interrupted = self.submit('TEST_SAVED'),self.submit('TEST_INTERRUPTED')
        first = self.execution.claim(saved['wake_id'])
        second = self.execution.claim(interrupted['wake_id'])
        self.execution.answer(first,'TEST saved answer')
        self.execution = ProgramExecution(self.wakes)
        self.execution.recover()
        self.assertEqual(self.actions.get(self.current,interrupted['action_id'])['state'],'ambiguous')
        self.assertIsNone(self.execution.claim(interrupted['wake_id']))
        with self.assertRaisesRegex(ProgramDenied,'program_wake_replaced'):
            self.execution.answer(second,'TEST late answer')
        reply = self.execution.publish(first)
        self.assertEqual(self.execution.publish(first),reply)
        self.assertEqual(self.query("SELECT used FROM program_quota WHERE kind='say'"),[{'used':1}])
        self.assertEqual((await self.sender.send_delivery(self.delivery(reply))).status,'committed')
        self.assertTrue(self.execution.settle_delivery(first))
        self.assertEqual(self.actions.get(self.current,saved['action_id'])['state'],'completed')
        self.outbox.admission.assert_not_called()

    async def test_merged_answer_records_all_programs_and_shared_delivery_truth(self):
        first,second,run,reply = self.merged()
        self.assertEqual((await self.sender.send_delivery(self.delivery(reply))).status,'committed')
        for action in (first,second):
            self.assertEqual(self.actions.get(self.current,action['action_id'])['state'],'completed')
        message = self.query("SELECT * FROM messages WHERE direction='outbound'")[0]
        raw = json.loads(message['raw_event_json'])
        self.assertEqual(raw['programs'],['TEST_A','TEST_B'])
        self.assertEqual(raw['program_wake_id'],run['wake_id'])
        self.assertEqual(message['sender_display'],'程序合并唤醒')
        self.assertEqual(message['message_kind'],'chat')

    async def test_disabling_any_member_after_enqueue_blocks_the_whole_merged_answer(self):
        first,second,run,reply = self.merged()
        self.query('INSERT INTO program_controls VALUES (?,?,FALSE,?)',(self.current.scope_key,'TEST_B',self.now))
        result = await self.sender.send_delivery(self.delivery(reply))
        self.assertEqual((result.status,result.reason),('failed','program_disabled'))
        self.assertEqual(self.fake.received,[])
        self.assertTrue(self.execution.settle_delivery(run))
        for action in (first,second):
            self.assertEqual(self.actions.get(self.current,action['action_id'])['state'],'failed')

    async def test_wake_answer_cannot_evade_say_quota_and_old_generation_cannot_publish(self):
        action = self.submit()
        run = self.execution.claim(action['wake_id'])
        self.execution.answer(run,'TEST answer')
        self.query('INSERT INTO conversation_settings(scope_key,program_say_limit) VALUES (?,0)',(self.current.scope_key,))
        with self.assertRaisesRegex(ProgramDenied,'program_rate_limited'):
            self.execution.publish(run)
        self.query('UPDATE conversation_settings SET program_say_limit=60,stop_generation=1')
        with self.assertRaisesRegex(ProgramDenied,'conversation_stopped_or_replaced'):
            self.execution.publish(run)
        self.assertEqual(self.query('SELECT * FROM deliveries'),[])

    async def test_zero_after_admission_cancels_before_model_claim_and_lost_reply_stays_ambiguous(self):
        action = self.submit()
        self.query('INSERT INTO conversation_settings(scope_key,program_wake_limit) VALUES (?,0)',(self.current.scope_key,))
        self.assertIsNone(self.execution.claim(action['wake_id']))
        self.assertEqual(self.actions.get(self.current,action['action_id'])['error_code'],'program_action_disabled')
        self.query('UPDATE conversation_settings SET program_wake_limit=6')
        fresh = self.submit('TEST_FRESH'); run = self.execution.claim(fresh['wake_id'])
        self.execution.answer(run,'TEST accepted but response lost')
        reply = self.execution.publish(run)
        self.fake.drop_next_response = True
        self.assertEqual((await self.sender.send_delivery(self.delivery(reply))).status,'ambiguous')
        self.assertTrue(self.execution.settle_delivery(run))
        self.assertEqual(self.actions.get(self.current,fresh['action_id'])['state'],'ambiguous')
        self.assertIsNone(self.execution.claim(fresh['wake_id']))
        self.assertEqual(len(self.fake.received),1)
