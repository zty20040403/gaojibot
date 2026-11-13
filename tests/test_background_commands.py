"""Real PostgreSQL command intents and kill/stop fencing, without a guest shell."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

from src.plugins.ai_chat.adapters.qq_official.background_commands import BackgroundCommands
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramDenied
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from tests.test_vm_sessions import BOOT
from tests.qq_official_outbox_fixture import OutboxFixture


class BackgroundCommandTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.sessions = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.credential = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1))
        self.current = self.credential.source
        self.commands = BackgroundCommands(self.sessions)

    def reserve(self, request='TEST_REQUEST', **changes):
        return self.commands.reserve(self.current,**(dict(generation=(0,0),program='TEST_PROGRAM',
            request_id=request,argv=['printf','TEST_MESSAGE'])|changes))

    def control(self, action):
        with self.tx() as cursor:
            return change(cursor,self.endpoint,action,now=self.now,actor='TEST_ADMIN',authorized=True)

    def test_idempotent_hash_only_intent_has_no_actor_and_safe_status(self):
        first,created = self.reserve()
        self.assertTrue(created)
        self.commands = BackgroundCommands(self.sessions)
        second,created = self.reserve()
        self.assertFalse(created); self.assertEqual(second,first)
        self.assertEqual(first['deadline_at']-first['created_at'],21600)
        self.assertEqual(self.commands.authenticate(self.credential.token),(self.current,(0,0)))
        status = self.commands.get(self.current,first['command_id'])
        self.assertEqual(status['output_path'],'/work/.gaoji/out/'+first['command_id']+'.txt')
        for key in ('actor_key','owner','token_hash','argv_json'):
            self.assertNotIn(key,status)
        self.assertNotIn(self.credential.token,json.dumps(first))
        self.assertFalse(self.query('SELECT * FROM background_jobs'))
        self.assertFalse(self.query('SELECT * FROM program_quota'))
        with self.assertRaisesRegex(ProgramDenied,'request_conflict'):
            self.reserve(argv=['printf','TEST_CHANGED'])

    def test_concurrent_eight_limit_retains_uncertain_commands_until_confirmed_stop(self):
        def attempt(number):
            try:
                return self.reserve('TEST_'+str(number))[1]
            except ProgramDenied as error:
                self.assertEqual(error.status,429)
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt,range(16))),8)
        self.query("UPDATE qq_official_background_commands SET state='ambiguous'")
        self.assertEqual(self.control('kill')['cancelled_commands'],8)
        self.assertTrue(all(row['finished_at'] is None for row in self.query('SELECT * FROM qq_official_background_commands')))
        with self.assertRaisesRegex(ProgramDenied,'command_limit'):
            self.reserve('TEST_BLOCKED',generation=(0,1))
        self.query("UPDATE qq_official_background_commands SET state='cancelled',finished_at=?",(self.now,))
        self.assertTrue(self.reserve('TEST_FRESH',generation=(0,1))[1])

    def test_kill_fences_slow_old_start_without_changing_conversation_stop(self):
        captured = self.commands.capture(self.current)
        row,_ = self.reserve()
        receipt = self.control('kill')
        self.assertEqual(receipt['cancelled_command_ids'],[row['command_id']])
        settings = self.query('SELECT stopped,stop_generation,command_generation FROM conversation_settings')[0]
        self.assertEqual(settings,dict(stopped=False,stop_generation=0,command_generation=1))
        with self.assertRaisesRegex(ProgramDenied,'generation_changed'):
            self.reserve('TEST_SLOW_BODY',generation=captured)
        with self.assertRaises(ProgramDenied):
            self.commands.require_generation(self.current,captured)
        status = self.commands.get(self.current,row['command_id'])
        self.assertEqual((status['state'],status['finished_at']),('cancelled',self.now))
        self.assertTrue(self.reserve('TEST_AFTER_KILL',generation=(0,1))[1])
        self.control('stop'); self.control('start')
        self.assertEqual(self.commands.capture(self.current),(1,2))
        with self.assertRaises(ProgramDenied):
            self.reserve('TEST_AFTER_KILL',generation=(1,2))
        self.assertTrue(self.reserve('TEST_NEW',generation=(1,2))[1])

    def test_single_cancel_is_live_and_scope_and_rotation_never_rebind_old_work(self):
        row,_ = self.reserve()
        self.assertTrue(self.commands.cancel(self.current,row['command_id']))
        with self.tx() as cursor,self.assertRaises(ProgramDenied):
            self.commands.check(cursor,row)
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        with self.assertRaises(PermissionError):
            self.commands.reserve(replace(self.current,endpoint=other),generation=(0,0),program='TEST',request_id='TEST',argv=['true'])
        self.current = self.sessions.issue(self.endpoint,'s123456',self.current.boot_id,expected_route=(1,1)).source
        with self.assertRaises(PermissionError):
            self.reserve()
        self.assertEqual(self.commands.get(self.current,row['command_id'])['state'],'cancelled')
        with self.assertRaisesRegex(ProgramDenied,'not_found'):
            self.commands.get(self.current,'TEST_UNKNOWN')

    def test_idle_shutdown_holds_pending_and_uncertain_commands_and_serializes_new_starts(self):
        row,_ = self.reserve()
        self.assertFalse(self.sessions.revoke_vm('s123456',if_idle=True))
        self.assertEqual(self.sessions.authenticate(self.credential.token),self.current)
        self.query("UPDATE qq_official_background_commands SET state='ambiguous'")
        self.control('kill')
        self.assertFalse(self.sessions.revoke_vm('s123456',if_idle=True))
        self.query("UPDATE qq_official_background_commands SET state='cancelled',finished_at=?",(self.now,))
        self.assertTrue(self.sessions.revoke_vm('s123456',if_idle=True))
        with self.assertRaises(PermissionError):
            self.sessions.authenticate(self.credential.token)
        for index in range(4):
            self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
            def reserve():
                try:
                    return self.reserve('TEST_RACE_'+str(index),generation=(0,1))[1]
                except PermissionError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                admitted = pool.submit(reserve)
                revoked = pool.submit(self.sessions.revoke_vm,'s123456',if_idle=True)
                self.assertNotEqual(admitted.result(),revoked.result())
            self.query("UPDATE qq_official_background_commands SET state='cancelled',finished_at=?",(self.now,))

    def test_real_downgrade_cannot_drop_command_evidence(self):
        import os
        from unittest.mock import patch
        from alembic import command
        from alembic.config import Config
        from src.bot_storage.schema import HEAD_REVISION
        row,_ = self.reserve()
        with patch.dict(os.environ,{'AI_POSTGRES_DSN':self.database.dsn,'AI_POSTGRES_SCHEMA':self.database.schema}):
            with self.assertRaisesRegex(RuntimeError,'background_commands_require_export'):
                command.downgrade(Config('alembic.ini'),'0085_program_files')
        self.assertEqual(self.query('SELECT version_num FROM alembic_version')[0]['version_num'],HEAD_REVISION)
        self.assertEqual(self.commands.get(self.current,row['command_id'])['state'],'pending')

    def test_disabled_program_invalid_argv_and_transactional_control_rollback(self):
        for argv in ([],[''],['echo',None],['echo','\0'],['echo']*129,['echo','X'*16384]):
            with self.assertRaises(ProgramDenied):
                self.reserve(argv=argv)
        row,_ = self.reserve()
        with self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'):
            with self.tx() as cursor:
                change(cursor,self.endpoint,'kill',now=self.now,actor='TEST_ADMIN',authorized=True)
                raise RuntimeError('TEST_ROLLBACK')
        self.assertEqual(self.commands.capture(self.current),(0,0))
        self.assertFalse(self.commands.get(self.current,row['command_id'])['cancel_requested'])
        self.query('INSERT INTO program_controls(scope_key,program_name,enabled,updated_at) VALUES (?,?,FALSE,?)',
            (self.current.scope_key,'TEST_PROGRAM',self.now))
        with self.tx() as cursor,self.assertRaisesRegex(ProgramDenied,'program_disabled'):
            self.commands.check(cursor,row)
