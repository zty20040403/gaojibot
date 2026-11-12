"""Persistent program admission and quota using real PostgreSQL fences."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

from tests.qq_official_outbox_fixture import OutboxFixture
from tests.test_vm_sessions import BOOT
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions,ProgramDenied
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from src.plugins.ai_chat.platform_types import EndpointKey


class ProgramActionTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.sessions = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.credential = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1))
        self.current = self.credential.source
        self.actions = ProgramActions(self.sessions)

    def reserve(self, key='TEST_REQUEST', **changes):
        return self.actions.reserve(self.current,**(dict(generation=0,kind='say',program='TEST_PROGRAM',
            request_id=key,payload={'text':'TEST_MESSAGE'})|changes))

    def test_hash_only_intent_is_idempotent_without_human_or_alias_authority(self):
        first,created = self.reserve()
        self.assertTrue(created)
        self.actions = ProgramActions(self.sessions)
        second,created = self.reserve()
        self.assertEqual(first,second); self.assertFalse(created)
        self.assertEqual(first['scope_key'],self.endpoint.scope.key)
        for name in ('actor_key','requester_actor_key','owner'):
            self.assertNotIn(name,first)
        self.assertNotIn(self.credential.token,json.dumps(first))
        self.assertNotIn('token_hash',self.actions.get(self.current,first['action_id']))
        with self.assertRaisesRegex(ProgramDenied,'program_request_conflict'):
            self.reserve(payload={'text':'TEST_DIFFERENT'})
        self.assertEqual(self.query('SELECT used FROM program_quota')[0]['used'],1)

    def test_concurrent_limit_is_shared_by_say_and_files_and_survives_reopen(self):
        self.query('INSERT INTO conversation_settings(scope_key,program_say_limit) VALUES (?,3)',(self.current.scope_key,))
        def attempt(number):
            try:
                return self.reserve('TEST_'+str(number),kind='say' if number%2 else 'send_file')[1]
            except ProgramDenied as error:
                self.assertEqual(error.status,429)
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt,range(12))),3)
        self.assertEqual(self.query('SELECT used FROM program_quota')[0]['used'],3)
        self.query("UPDATE qq_official_program_actions SET state='failed',error_code='TEST_SEND_FAILED'")
        with self.assertRaisesRegex(ProgramDenied,'program_rate_limited'):
            self.reserve('TEST_NO_REFUND')
        self.now += 3600
        self.assertTrue(self.reserve('TEST_NEXT_HOUR')[1])

    def test_defaults_are_sixty_say_and_six_wake_and_zero_disables(self):
        for number in range(60):
            self.reserve('TEST_SAY_'+str(number))
        with self.assertRaises(ProgramDenied):
            self.reserve('TEST_SAY_61')
        for number in range(6):
            self.reserve('TEST_WAKE_'+str(number),kind='wake')
        with self.assertRaises(ProgramDenied):
            self.reserve('TEST_WAKE_7',kind='wake')
        self.now += 3600
        self.query('INSERT INTO conversation_settings(scope_key,program_say_limit) VALUES (?,0)',(self.current.scope_key,))
        with self.assertRaises(ProgramDenied):
            self.reserve('TEST_DISABLED_QUOTA')

    def test_stop_start_cannot_admit_an_old_body_or_replay_an_old_request(self):
        generation = self.actions.capture(self.current)
        first,_ = self.reserve()
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.current.scope_key,))
        with self.assertRaisesRegex(ProgramDenied,'conversation_stopped_or_replaced'):
            self.actions.capture(self.current)
        self.assertEqual(self.actions.get(self.current,first['action_id'])['state'],'pending')
        self.query('UPDATE conversation_settings SET stopped=FALSE')
        with self.assertRaises(ProgramDenied):
            self.reserve('TEST_OLD_BODY',generation=generation)
        with self.assertRaises(ProgramDenied):
            self.reserve(generation=1)
        self.assertTrue(self.reserve('TEST_NEW',generation=self.actions.capture(self.current))[1])

    def test_program_disable_and_scope_disable_fence_already_reserved_actions(self):
        first,_ = self.reserve()
        self.query('INSERT INTO program_controls(scope_key,program_name,enabled,updated_at) VALUES (?,?,FALSE,?)',
            (self.current.scope_key,'TEST_PROGRAM',self.now))
        with self.tx() as cursor,self.assertRaisesRegex(ProgramDenied,'program_disabled'):
            self.actions.check(cursor,first)
        self.assertTrue(self.reserve('TEST_OTHER',program='TEST_OTHER')[1])
        self.query('INSERT INTO conversation_settings(scope_key,programs_enabled) VALUES (?,FALSE)',(self.current.scope_key,))
        with self.assertRaisesRegex(ProgramDenied,'program_disabled'):
            self.reserve('TEST_GLOBAL',program='TEST_OTHER')

    def test_rotation_cross_scope_and_expiry_never_reauthorize_old_intent(self):
        first,_ = self.reserve()
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        with self.assertRaises(PermissionError):
            self.actions.reserve(replace(self.current,endpoint=other),generation=0,kind='say',program='TEST_P',request_id='TEST_R',payload={})
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        with self.assertRaises(PermissionError):
            self.reserve()
        self.assertEqual(self.actions.get(self.current,first['action_id'])['action_id'],first['action_id'])
        fresh,_ = self.reserve('TEST_FRESH')
        self.now += 300
        with self.tx() as cursor,self.assertRaisesRegex(ProgramDenied,'program_action_expired'):
            self.actions.check(cursor,fresh)

    def test_outer_transaction_rollback_includes_quota_and_action(self):
        with self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'):
            with self.tx() as cursor:
                self.reserve(transaction_cursor=cursor)
                raise RuntimeError('TEST_ROLLBACK')
        self.assertEqual(self.query('SELECT * FROM program_quota'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_program_actions'),[])
        for payload in ({'text':'X'*16384},{'nan':float('nan')},{'bad':object()}):
            with self.assertRaisesRegex(ValueError,'invalid_program_action'):
                self.reserve(payload=payload)
