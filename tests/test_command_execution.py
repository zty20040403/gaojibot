"""Real PostgreSQL monitor claims, terminal facts and exactly-once wake admission."""
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.command_execution import CommandExecution, observed
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions, ProgramDenied
from src.plugins.ai_chat.adapters.qq_official.program_wakes import ProgramWakes
from tests import test_background_commands as fixture
from tests.qq_official_outbox_fixture import OutboxFixture


def report(**changes):
    return dict(ok=True,state='running',stopped=False,exit_code=None,elapsed_seconds=None,
        output_head='TEST_HEAD',output_tail='TEST_TAIL',output_bytes=18,error='')|changes


class CommandExecutionTests(OutboxFixture):
    reserve = fixture.BackgroundCommandTests.reserve
    control = fixture.BackgroundCommandTests.control

    def setUp(self):
        super().setUp()
        self.sessions = fixture.VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.credential = self.sessions.issue(self.endpoint,'s123456',fixture.BOOT,expected_route=(1,1))
        self.current = self.credential.source
        self.commands = fixture.BackgroundCommands(self.sessions)
        self.wakes = ProgramWakes(ProgramActions(self.sessions))
        self.execution = CommandExecution(self.commands,self.wakes)
        self.row,_ = self.reserve()
        self.id = self.row['command_id']

    def stored(self):
        return self.query('SELECT * FROM qq_official_background_commands WHERE command_id=?',(self.id,))[0]

    def test_concurrent_claims_only_start_once_and_unknown_guest_never_restarts(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            operations = list(pool.map(lambda _:self.execution.next(self.id)[0],range(4)))
        self.assertEqual(operations.count('start'),1)
        self.assertEqual(operations.count('status'),3)
        self.execution.uncertain(self.id)
        self.execution = CommandExecution(self.commands,self.wakes)
        self.assertEqual(self.execution.next(self.id)[0],'status')
        self.assertEqual(self.stored()['state'],'ambiguous')
        self.assertIsNone(self.stored()['finished_at'])
        self.assertIn(self.id,self.execution.pending())
        self.assertFalse(self.query('SELECT * FROM usage_events'))

    def test_terminal_snapshot_notifies_once_and_late_running_cannot_erase_exit(self):
        self.execution.next(self.id)
        self.execution.record(self.id,report(state='completed',stopped=True,exit_code=0,elapsed_seconds=12,
            output_head=self.credential.token+'X'*1900,output_tail='Z'*2048))
        self.execution.record(self.id,report())
        self.assertEqual((self.stored()['state'],self.stored()['exit_code'],self.stored()['elapsed_seconds']),('completed',0,12))
        self.assertTrue(self.execution.notify(self.id)); self.assertFalse(self.execution.notify(self.id))
        actions = self.query('SELECT * FROM qq_official_program_actions')
        self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]['source_command_id'],self.id)
        text = json.loads(actions[0]['payload_json'])['text']
        self.assertLessEqual(len(text),4000)
        self.assertNotIn(self.credential.token,text); self.assertIn('[VM_TOKEN]',text)
        self.assertIn('12 秒',text)
        self.assertNotIn(self.id,self.execution.pending())
        self.assertFalse(self.query('SELECT * FROM usage_events'))

    def test_kill_preserves_cancellation_until_guest_stops_and_blocks_saved_completion(self):
        self.execution.next(self.id)
        self.control('kill')
        self.assertEqual(self.execution.next(self.id)[0],'stop')
        self.execution.record(self.id,report())
        self.assertEqual(self.stored()['state'],'stopping')
        self.assertIsNone(self.stored()['finished_at'])
        self.execution.record(self.id,report(state='completed',stopped=True,exit_code=0,elapsed_seconds=5))
        self.assertEqual(self.stored()['exit_code'],0)  # Keep actual exit, not a fabricated interruption.
        self.assertFalse(self.execution.notify(self.id))
        self.assertEqual(self.stored()['notification_error'],'command_notification_source_unavailable')
        self.assertFalse(self.query('SELECT * FROM qq_official_program_actions'))

    def test_timeout_can_notify_partial_output_but_revoked_source_only_stops(self):
        self.execution.next(self.id)
        self.query('UPDATE qq_official_background_commands SET created_at=?,deadline_at=? WHERE command_id=?',
            (self.now-100,self.now-1,self.id))
        self.assertEqual(self.execution.next(self.id)[0],'stop')
        self.assertFalse(self.stored()['cancel_requested'])
        self.execution.record(self.id,report(state='cancelled',stopped=True,error='command_cancelled',elapsed_seconds=99))
        self.assertEqual((self.stored()['state'],self.stored()['error_code']),('failed','command_timeout'))
        self.assertTrue(self.execution.notify(self.id))
        second,_ = self.reserve('TEST_SECOND')
        self.execution.next(second['command_id'])
        self.sessions.revoke(self.endpoint)
        operation,row = self.execution.next(second['command_id'])
        self.assertEqual(operation,'stop'); self.assertTrue(row['cancel_requested'])

    def test_unstarted_expiry_needs_no_guest_and_quota_failure_is_persisted(self):
        self.query('UPDATE qq_official_background_commands SET created_at=?,deadline_at=? WHERE command_id=?',
            (self.now-100,self.now-1,self.id))
        self.assertEqual(self.execution.next(self.id)[0],'notify')
        self.assertIsNone(self.stored()['started_at'])
        self.assertEqual(self.stored()['state'],'failed')
        self.query('INSERT INTO conversation_settings(scope_key,program_wake_limit) VALUES (?,0)',(self.current.scope_key,))
        self.assertFalse(self.execution.notify(self.id))
        self.assertEqual(self.stored()['notification_error'],'command_notification_quota_unavailable')
        self.assertFalse(self.query('SELECT * FROM program_quota'))
        self.assertNotIn(self.id,self.execution.pending())

    def test_notice_savepoint_rolls_back_partial_quota_before_recording_failure(self):
        self.execution.next(self.id)
        self.execution.record(self.id,report(state='failed',stopped=True,exit_code=7,elapsed_seconds=1))
        def rejected(*args,transaction_cursor,**kwargs):
            transaction_cursor.execute("INSERT INTO program_quota VALUES (?,'wake',?,1)",(self.current.scope_key,self.now//3600))
            raise ProgramDenied('program_wake_buffer_full',429)
        with patch.object(self.wakes,'submit',side_effect=rejected):
            self.assertFalse(self.execution.notify(self.id))
        self.assertFalse(self.query('SELECT * FROM program_quota'))
        self.assertTrue(self.stored()['notification_error'])

    def test_observation_migration_round_trip_and_evidence_guard(self):
        import os
        from alembic import command
        from alembic.config import Config
        from src.bot_storage.schema import HEAD_REVISION
        with patch.dict(os.environ,{'AI_POSTGRES_DSN':self.database.dsn,'AI_POSTGRES_SCHEMA':self.database.schema}):
            command.downgrade(Config('alembic.ini'),'0086_background_commands')
            command.upgrade(Config('alembic.ini'),HEAD_REVISION)
            self.execution.next(self.id)
            with self.assertRaisesRegex(RuntimeError,'observations_require_export'):
                command.downgrade(Config('alembic.ini'),'0086_background_commands')
        self.assertEqual(self.query('SELECT version_num FROM alembic_version')[0]['version_num'],HEAD_REVISION)
        self.assertEqual(self.stored()['state'],'starting')

    def test_malformed_guest_cannot_free_capacity_or_store_secrets_as_error_codes(self):
        for changes in ({'stopped':True},{'state':[]},{'output_bytes':True},{'exit_code':999},
                {'elapsed_seconds':-1},{'error':'TEST_SECRET https://invalid'},{'output_head':'X'*2049},
                {'state':'completed','stopped':True,'exit_code':None},{'ok':False}):
            with self.assertRaises(ValueError):
                observed(report(**changes))
        with self.assertRaisesRegex(ProgramDenied,'not_started'):
            self.execution.record(self.id,report(state='completed',stopped=True,exit_code=0))
        self.assertIsNone(self.stored()['finished_at'])
