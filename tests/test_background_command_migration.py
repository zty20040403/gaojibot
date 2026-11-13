"""Actual 0085/0086 upgrade and guarded rollback in an owned test schema."""
import unittest
from tests import test_qq_official_outbox_migration as fixture


class BackgroundCommandMigrationTests(unittest.TestCase):
    def test_empty_roundtrip_preserves_other_settings_but_not_nonzero_command_fence(self):
        schema = fixture.owned_schema(); self.addCleanup(fixture.drop,schema)
        fixture.migrate(schema,'0085_program_files')
        with fixture.connect(schema) as connection:
            connection.execute("INSERT INTO conversation_settings(scope_key,stopped,stop_generation,program_say_limit) VALUES ('TEST_SCOPE',TRUE,8,3)")
        fixture.migrate(schema,'0086_background_commands')
        with fixture.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT stopped,stop_generation,program_say_limit,command_generation FROM conversation_settings').fetchone(),(True,8,3,0))
            self.assertEqual(connection.execute('SELECT source_command_id FROM qq_official_program_actions').fetchall(),[])
            connection.execute('UPDATE conversation_settings SET command_generation=1')
        with self.assertRaisesRegex(RuntimeError,'command_generations_require_export'):
            fixture.migrate(schema,'0085_program_files',downgrade=True)
        with fixture.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT command_generation FROM conversation_settings').fetchone(),(1,))
            connection.execute('UPDATE conversation_settings SET command_generation=0')
        fixture.migrate(schema,'0085_program_files',downgrade=True)
        with fixture.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT stopped,stop_generation,program_say_limit FROM conversation_settings').fetchone(),(True,8,3))
        fixture.migrate(schema,'0086_background_commands')
