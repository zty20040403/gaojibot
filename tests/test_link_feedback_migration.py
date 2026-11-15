import unittest
from unittest.mock import patch

from src.bot_storage import PostgresDatabase
from tests import test_link_feedback_schema as fixture,test_qq_official_outbox_migration as migration


class LinkFeedbackMigrationTests(unittest.TestCase):
    def test_empty_increment_restores_exact_old_constraints_and_keeps_original_outcomes(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0102_link_replies')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = fixture.LinkFeedbackSchemaTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.test_qq_official_inbox.postgres_for_test',return_value=database):case.setUp()
        source = case.source()
        def constraints():
            with migration.connect(schema) as connection:
                return dict(connection.execute("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname IN ('delivery_plans_check2','delivery_plans_purpose_check','ck_deliveries_official_fields','ck_deliveries_ephemeral') AND connamespace=%s::regnamespace",(schema,)).fetchall())
        before = constraints()
        migration.migrate(schema,'0103_link_feedback'); migration.migrate(schema,'0102_link_replies',downgrade=True)
        self.assertEqual(before,constraints()); self.assertEqual(case.query('SELECT * FROM qq_official_link_replies'),[source])
        migration.migrate(schema,'0103_link_feedback'); plan = case.plan(source)
        with self.assertRaisesRegex(RuntimeError,'link_feedback_requires_export'):
            migration.migrate(schema,'0102_link_replies',downgrade=True)
        self.assertEqual(case.query('SELECT link_reply_inbox_id FROM delivery_plans WHERE plan_id=?',(plan,)),[dict(link_reply_inbox_id=source['source_inbox_id'])])
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0103_link_feedback',))
