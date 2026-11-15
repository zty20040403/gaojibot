import unittest
from unittest.mock import patch

from src.bot_storage import PostgresDatabase
from tests import test_link_notice_schema as fixture,test_qq_official_outbox_migration as migration


class LinkNoticeMigrationTests(unittest.TestCase):
    def test_empty_increment_downgrades_but_link_ingress_and_plan_evidence_require_export(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0100_link_receipts')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = fixture.LinkNoticeSchemaTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):case.setUp()
        source = case.challenge()
        with migration.connect(schema) as connection:
            before = dict(connection.execute("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname IN ('delivery_plans_check2','delivery_plans_purpose_check','ck_deliveries_official_fields','ck_deliveries_ephemeral') AND connamespace=%s::regnamespace",(schema,)).fetchall())
        migration.migrate(schema,'0101_link_notices')
        migration.migrate(schema,'0100_link_receipts',downgrade=True)
        with migration.connect(schema) as connection:
            after = dict(connection.execute("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname IN ('delivery_plans_check2','delivery_plans_purpose_check','ck_deliveries_official_fields','ck_deliveries_ephemeral') AND connamespace=%s::regnamespace",(schema,)).fetchall())
            self.assertEqual(before,after)
            self.assertEqual(connection.execute('SELECT source_receipt_hash FROM qq_official_link_challenges').fetchone(),(source['source_receipt_hash'],))
        migration.migrate(schema,'0101_link_notices')
        inbox = case.ingress(source); plan = case.plan(source)
        case.query('UPDATE qq_official_link_challenges SET source_inbox_id=?,notice_plan_id=? WHERE challenge_id=?',(inbox,plan,source['challenge_id']))
        with self.assertRaisesRegex(RuntimeError,'link_notices_require_export'):
            migration.migrate(schema,'0100_link_receipts',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT source_inbox_id,notice_plan_id FROM qq_official_link_challenges').fetchone(),(inbox,plan))
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0101_link_notices',))
