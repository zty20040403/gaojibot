"""Only owned migration-test schemas are upgraded or downgraded."""
import unittest
from unittest.mock import patch
from src.bot_storage import PostgresDatabase
from tests import test_ops_actions as fixture,test_qq_official_outbox_migration as migration


class OpsActionMigrationTests(unittest.TestCase):
    def test_empty_downgrade_but_real_audit_evidence_requires_export(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0088_agent_time_limits')
        migration.migrate(schema,'0090_ops_notices')
        migration.migrate(schema,'0089_ops_actions',downgrade=True)
        migration.migrate(schema,'0088_agent_time_limits',downgrade=True)
        migration.migrate(schema,'0089_ops_actions')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema)
        self.addCleanup(database.close)
        case = fixture.OpsActionTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):
            case.setUp()
        original = case.request().action
        with case.tx() as cursor:
            case.ops.close(cursor,original['action_id'],state='denied',error='owner_declined')
        with self.assertRaisesRegex(RuntimeError,'ops_actions_require_export'):
            migration.migrate(schema,'0088_agent_time_limits',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT action_id,state FROM ops_actions').fetchone(),(original['action_id'],'denied'))
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0089_ops_actions',))

    def test_notice_source_and_ephemeral_evidence_cannot_be_dropped(self):
        from src.plugins.ai_chat.adapters.qq_official.ops_notices import OpsNotices
        from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
        from src.plugins.ai_chat.delivery import DeliveryStore
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0090_ops_notices')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = fixture.OpsActionTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):
            case.setUp()
        deliveries = DeliveryStore(database); self.addCleanup(deliveries.close)
        outbox = OfficialSendStore(deliveries,admission=lambda *args:True,pepper=case.ops.pepper,clock=lambda:case.now)
        notices = OpsNotices(case.ops,outbox,configuration=lambda:case.config); outbox.ops_authority = notices
        proposal = case.request()
        with patch.object(type(case.config),'secret',return_value='TEST_SECRET_'*4),case.tx() as cursor:
            ref = notices.prepare(cursor,proposal)
        with self.assertRaisesRegex(RuntimeError,'ops_notices_require_export'):
            migration.migrate(schema,'0089_ops_actions',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT notice_plan_id FROM ops_actions').fetchone(),(ref.plan_id,))
