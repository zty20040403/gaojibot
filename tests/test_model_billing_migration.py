import unittest

from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_qq_official_outbox_migration as migration


class ModelBillingMigrationTests(unittest.TestCase):
    def test_no_legacy_repricing_empty_rollback_and_nonempty_evidence_protection(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0093_conversation_config')
        account = AccountKey('qq-official','TEST_BILL_MIGRATION','test')
        endpoint = EndpointKey(account,'group','TEST_BILL_SCOPE')
        with migration.connect(schema) as connection:
            migration.insert(connection,'qq_official_accounts',dict(account_key=account.key,enabled=True,fencing_epoch=1,
                gateway_state='active',created_at=1,updated_at=1))
            migration.insert(connection,'qq_official_endpoints',dict(endpoint_key=endpoint.key,account_key=account.key,kind='group',
                native_conversation_id=endpoint.native_conversation_id,canonical_scope_key=endpoint.scope.key,
                binding_state='active',membership_state='present',created_at=1,updated_at=1))
            migration.insert(connection,'usage_events',dict(scope_key=endpoint.scope.key,source='official_ocr_bound',
                provider='TEST_PROVIDER',model='TEST_MODEL',input_tokens=10000,output_tokens=10000,occurred_at=1))
        migration.migrate(schema,'0094_model_usage_calls')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM model_usage_calls').fetchone()[0],0)
            self.assertEqual(connection.execute('SELECT input_tokens,output_tokens FROM usage_events').fetchone(),(10000,10000))
        migration.migrate(schema,'0093_conversation_config',downgrade=True)
        migration.migrate(schema,'0094_model_usage_calls')
        with migration.connect(schema) as connection:
            migration.insert(connection,'model_usage_calls',dict(call_id='f'*32,endpoint_key=endpoint.key,
                scope_key=endpoint.scope.key,purpose='turn',provider='TEST_PROVIDER',model='TEST_MODEL',profile='test',started_at=1))
        with self.assertRaisesRegex(RuntimeError,'model_usage_requires_export'):
            migration.migrate(schema,'0093_conversation_config',downgrade=True)
