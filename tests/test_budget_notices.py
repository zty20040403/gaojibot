"""Migration 0097_budget_notices round-trip and export guard."""
import unittest

from tests import test_qq_official_outbox_migration as migration


class BudgetNoticeMigrationTests(unittest.TestCase):
    def test_upgrade_cancels_unbound_legacy_notice_and_empty_increment_downgrades(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, '0096_daily_budgets')
        with migration.connect(schema) as connection:
            connection.execute("INSERT INTO qq_official_accounts(account_key,enabled,gateway_state,created_at,updated_at) VALUES ('TEST_ACCOUNT',TRUE,'idle',1,1)")
            connection.execute("""INSERT INTO qq_official_endpoints(endpoint_key,account_key,kind,native_conversation_id,
                canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                VALUES ('TEST_EP','TEST_ACCOUNT','group','TEST_GROUP','TEST_SCOPE','active','present',1,1)""")
            connection.execute("""INSERT INTO conversation_budget_days(scope_key,day_start,endpoint_key,account_epoch,
                endpoint_generation,route_epoch,first_exceeded_at,budget_usd,spent_usd,last_blocked_generation)
                VALUES ('TEST_SCOPE',0,'TEST_EP',1,1,1,1,0,0,2)""")
        migration.migrate(schema, '0097_budget_notices')
        with migration.connect(schema) as connection:
            self.assertEqual(
                connection.execute('SELECT notice_state,conversation_generation,notice_budget_generation FROM conversation_budget_days').fetchone(),
                ('cancelled', None, None),
            )
        migration.migrate(schema, '0096_daily_budgets', downgrade=True)
        migration.migrate(schema, '0097_budget_notices')
        with migration.connect(schema) as connection:
            connection.execute('DELETE FROM conversation_budget_days')
            connection.execute("""INSERT INTO conversation_budget_days(scope_key,day_start,endpoint_key,account_epoch,
                endpoint_generation,route_epoch,first_exceeded_at,budget_usd,spent_usd,last_blocked_generation,
                conversation_generation,notice_budget_generation) VALUES ('TEST_SCOPE',0,'TEST_EP',1,1,1,1,0,0,2,0,2)""")
        with self.assertRaisesRegex(RuntimeError, 'budget_notice_requires_export'):
            migration.migrate(schema, '0096_daily_budgets', downgrade=True)
