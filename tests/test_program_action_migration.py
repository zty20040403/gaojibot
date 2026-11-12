"""Actual 0082/0083 round trip in a unique, owned loopback-test schema."""
import unittest
from tests import test_qq_official_outbox_migration as fixture


class ProgramActionMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_settings_and_downgrade_requires_export(self):
        schema = fixture.owned_schema()
        self.addCleanup(fixture.drop,schema)
        fixture.migrate(schema,'0082_vm_sessions')
        with fixture.connect(schema) as connection:
            connection.execute('''INSERT INTO conversation_settings(scope_key,image_captions,stopped,stop_generation)
                VALUES ('TEST_SCOPE',TRUE,TRUE,3)''')
        fixture.migrate(schema,'0083_program_actions')
        with fixture.connect(schema) as connection:
            row = connection.execute('''SELECT image_captions,stopped,stop_generation,
                programs_enabled,program_say_limit,program_wake_limit FROM conversation_settings''').fetchone()
            self.assertEqual(row,(True,True,3,True,60,6))
            connection.execute('UPDATE conversation_settings SET program_say_limit=0')
        with self.assertRaisesRegex(RuntimeError,'program_settings_require_export'):
            fixture.migrate(schema,'0082_vm_sessions',downgrade=True)
        with fixture.connect(schema) as connection:
            connection.execute('UPDATE conversation_settings SET program_say_limit=60')
            connection.execute("INSERT INTO program_quota VALUES ('TEST_SCOPE','say',1,1)")
        with self.assertRaisesRegex(RuntimeError,'program_actions_require_export'):
            fixture.migrate(schema,'0082_vm_sessions',downgrade=True)
        with fixture.connect(schema) as connection:
            connection.execute('DELETE FROM program_quota')
        fixture.migrate(schema,'0082_vm_sessions',downgrade=True)
        with fixture.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT image_captions,stopped,stop_generation FROM conversation_settings').fetchone(),
                (True,True,3))
        fixture.migrate(schema,'0083_program_actions')
