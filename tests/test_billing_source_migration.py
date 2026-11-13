"""Actual metadata migration: no attribution backfill, no evidence-losing rollback."""
from decimal import Decimal
import unittest

from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_qq_official_outbox_migration as migration


class BillingSourceMigrationTests(unittest.TestCase):
    def test_old_calls_stay_unattributed_and_only_empty_increment_can_downgrade(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0094_model_usage_calls')
        account = AccountKey('qq-official','TEST_SOURCE_MIGRATION','test')
        endpoint = EndpointKey(account,'group','TEST_SCOPE')
        with migration.connect(schema) as connection:
            migration.insert(connection,'qq_official_accounts',dict(account_key=account.key,enabled=True,fencing_epoch=1,
                gateway_state='active',created_at=1,updated_at=1))
            migration.insert(connection,'qq_official_endpoints',dict(endpoint_key=endpoint.key,account_key=account.key,kind='group',
                native_conversation_id=endpoint.native_conversation_id,canonical_scope_key=endpoint.scope.key,
                binding_state='active',membership_state='present',created_at=1,updated_at=1))
            migration.insert(connection,'model_usage_calls',dict(call_id='f'*32,endpoint_key=endpoint.key,scope_key=endpoint.scope.key,
                purpose='turn',provider='TEST_PROVIDER',model='TEST_MODEL',profile='test',started_at=1,finished_at=2,cost_usd='0.1'))
        migration.migrate(schema,'0095_billing_source_metadata')
        with migration.connect(schema) as connection:
            original = connection.execute('SELECT source_metadata_json,cost_usd,started_at,finished_at FROM model_usage_calls').fetchone()
            self.assertEqual(original,(None,Decimal('0.1'),1,2))
        migration.migrate(schema,'0094_model_usage_calls',downgrade=True)
        migration.migrate(schema,'0095_billing_source_metadata')
        with migration.connect(schema) as connection:
            migration.insert(connection,'model_usage_calls',dict(call_id='e'*32,endpoint_key=endpoint.key,scope_key=endpoint.scope.key,
                purpose='program_wake',provider='TEST_PROVIDER',model='TEST_MODEL',profile='test',started_at=3,
                source_metadata_json='{"person":null,"display":null,"programs":["TEST_A","TEST_B"],"commands":[]}'))
        with self.assertRaisesRegex(RuntimeError,'billing_source_metadata_requires_export'):
            migration.migrate(schema,'0094_model_usage_calls',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute("SELECT source_metadata_json FROM model_usage_calls WHERE call_id=%s",('f'*32,)).fetchone()[0],None)
            self.assertEqual(connection.execute('SELECT count(*) FROM model_usage_calls').fetchone()[0],2)
