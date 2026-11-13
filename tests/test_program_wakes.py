"""Real PostgreSQL wake batches: admission, bounded overflow and revocation."""
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from tests import test_qq_official_outbox_migration as migration
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions, ProgramDenied
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from tests.test_vm_sessions import BOOT
from src.plugins.ai_chat.adapters.qq_official.program_wakes import ProgramWakes


class ProgramWakeTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.sessions = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        self.actions = ProgramActions(self.sessions)
        self.wakes = ProgramWakes(self.actions)

    def submit(self, number, **changes):
        return self.wakes.submit(self.current,**(dict(generation=0,program='TEST_PROGRAM',
            request_id='TEST_'+str(number),text='TEST event '+str(number))|changes))

    def limit(self, number):
        self.query('INSERT INTO conversation_settings(scope_key,program_wake_limit) VALUES (?,?)',
            (self.current.scope_key,number))

    def test_six_concurrent_admissions_and_one_durable_overflow_batch(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda n:self.submit(n)[0],range(18)))
        self.assertEqual(sum(row['state']=='queued' for row in rows),6)
        self.assertEqual(sum(row['state']=='coalesced' for row in rows),12)
        overflow = [row for row in rows if row['state']=='coalesced']
        self.assertEqual(len({row['wake_id'] for row in overflow}),1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_program_wakes')),7)
        self.assertEqual(self.query('SELECT used FROM program_quota'),[{'used':6}])
        self.wakes = ProgramWakes(self.actions)
        again,created = self.submit(int(overflow[0]['request_id'].split('_')[1]))
        self.assertFalse(created)
        self.assertEqual(again,overflow[0])
        with self.assertRaisesRegex(ProgramDenied,'program_request_conflict'):
            self.submit(0,text='TEST changed')

    def test_next_hour_batch_gets_one_slot_before_a_new_request(self):
        self.limit(1)
        self.submit(0)
        first,_ = self.submit(1)
        self.submit(2)
        self.now = (self.now//3600+1)*3600
        new,_ = self.submit(3)
        self.assertEqual(new['state'],'coalesced')
        self.assertNotEqual(first['wake_id'],new['wake_id'])
        old = self.query('SELECT funded,due_at FROM qq_official_program_wakes WHERE wake_id=?',(first['wake_id'],))[0]
        self.assertEqual(old,{'funded':True,'due_at':self.now})
        self.assertEqual(self.actions.get(self.current,first['action_id'])['state'],'queued')
        self.assertEqual(self.query('SELECT used FROM program_quota ORDER BY hour'),[{'used':1},{'used':1}])

    def test_zero_disables_instead_of_building_an_unbounded_deferred_queue(self):
        self.limit(0)
        with self.assertRaisesRegex(ProgramDenied,'program_rate_limited'):
            self.submit(0)
        self.assertEqual(self.query('SELECT * FROM qq_official_program_wakes'),[])

    def test_overflow_has_explicit_count_and_byte_bounds_with_atomic_rollback(self):
        self.limit(1); self.submit(0)
        with patch('src.plugins.ai_chat.adapters.qq_official.program_wakes.MAX_BATCH_COUNT',2):
            self.submit(1); self.submit(2)
            with self.assertRaisesRegex(ProgramDenied,'program_wake_buffer_full'):
                self.submit(3)
        with patch('src.plugins.ai_chat.adapters.qq_official.program_wakes.MAX_BATCH_BYTES',1):
            with self.assertRaisesRegex(ProgramDenied,'program_wake_buffer_full'):
                self.submit(4)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_program_actions')),3)
        self.assertEqual(self.query('SELECT used FROM program_quota'),[{'used':1}])

    def test_one_disabled_program_does_not_hide_the_other_program_in_a_batch(self):
        self.limit(1); self.submit(0)
        disabled,_ = self.submit(1)
        live,_ = self.submit(2,program='TEST_OTHER')
        self.query('INSERT INTO program_controls VALUES (?,?,FALSE,?)',(self.current.scope_key,'TEST_PROGRAM',self.now))
        self.now = (self.now//3600+1)*3600
        self.submit(3,program='TEST_OTHER')
        self.assertEqual(self.actions.get(self.current,disabled['action_id'])['state'],'cancelled')
        self.assertEqual(self.actions.get(self.current,live['action_id'])['state'],'queued')

    def test_stop_start_and_expiry_discard_overflow_without_spending_new_quota(self):
        self.limit(1); self.submit(0)
        old,_ = self.submit(1)
        self.query('UPDATE conversation_settings SET stop_generation=1')
        fresh,_ = self.submit(2,generation=1)
        self.assertNotEqual(fresh['wake_id'],old['wake_id'])
        self.now = (self.now//3600+1)*3600+301
        newest,_ = self.submit(3,generation=1)
        self.assertEqual(newest['state'],'queued')
        self.assertEqual(self.actions.get(self.current,old['action_id'])['error_code'],'conversation_stopped_or_replaced')
        self.assertEqual(self.actions.get(self.current,fresh['action_id'])['error_code'],'program_action_expired')
        self.assertEqual(self.query('SELECT used FROM program_quota ORDER BY hour'),[{'used':1},{'used':1}])


class ProgramWakeMigrationTests(unittest.TestCase):
    def test_actual_upgrade_downgrade_requires_export_of_wake_state(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0083_program_actions')
        with migration.connect(schema) as connection:
            connection.execute("INSERT INTO program_quota VALUES ('TEST_SCOPE','wake',1,6)")
        migration.migrate(schema,'0084_program_wakes')
        with migration.connect(schema) as self.connection:
            endpoint = migration.OfficialOutboxMigrationTests.endpoint(self)
            self.connection.execute('''INSERT INTO qq_official_program_wakes
                (wake_id,account_key,endpoint_key,due_at,funded,created_at) VALUES (%s,%s,%s,100,TRUE,100)''',
                ('a'*32,endpoint.account.key,endpoint.key))
        with self.assertRaisesRegex(RuntimeError,'program_wakes_require_export'):
            migration.migrate(schema,'0083_program_actions',downgrade=True)
        with migration.connect(schema) as connection:
            connection.execute('DELETE FROM qq_official_program_wakes')
        migration.migrate(schema,'0083_program_actions',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT used FROM program_quota').fetchone(),(6,))
        migration.migrate(schema,'0084_program_wakes')
