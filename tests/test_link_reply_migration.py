import unittest
from unittest.mock import patch

from src.bot_storage import PostgresDatabase
from tests import test_link_replies as fixture,test_qq_official_outbox_migration as migration


class LinkReplyMigrationTests(unittest.TestCase):
    def test_empty_downgrade_preserves_issuance_but_control_outcomes_require_export(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0101_link_notices')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = fixture.LinkReplyTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.test_qq_official_inbox.postgres_for_test',return_value=database):case.setUp()
        issued = case.issue()
        migration.migrate(schema,'0102_link_replies'); migration.migrate(schema,'0101_link_notices',downgrade=True)
        self.assertEqual(case.challenge(issued)['source_inbox_id'],issued.inbox_id)
        migration.migrate(schema,'0102_link_replies')
        accepted = case.accept(case.link_frame('/link list')); row = case.reply(accepted)
        with self.assertRaisesRegex(RuntimeError,'link_replies_require_export'):
            migration.migrate(schema,'0101_link_notices',downgrade=True)
        self.assertEqual(case.reply(accepted),row)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0102_link_replies',))
