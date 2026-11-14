import unittest

from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests import test_qq_official_outbox_migration as migration


class ConversationLinkMigrationTests(unittest.TestCase):
    def test_no_inferred_links_preserves_existing_state_and_evidence_blocks_downgrade(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0097_budget_notices')
        account = AccountKey('qq-official','TEST_LINK_MIGRATION','test')
        endpoint = EndpointKey(account,'group','TEST_LINK_SOURCE')
        with migration.connect(schema) as connection:
            migration.insert(connection,'qq_official_accounts',dict(account_key=account.key,enabled=True,fencing_epoch=1,
                gateway_state='active',created_at=1,updated_at=1))
            migration.insert(connection,'qq_official_endpoints',dict(endpoint_key=endpoint.key,account_key=account.key,kind='group',
                native_conversation_id=endpoint.native_conversation_id,canonical_scope_key=endpoint.scope.key,
                binding_state='active',membership_state='present',created_at=1,updated_at=1))
            migration.insert(connection,'conversation_settings',dict(scope_key=endpoint.scope.key,stopped=True,stop_generation=5))
        migration.migrate(schema,'0098_conversation_links')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM qq_official_conversation_links').fetchone(),(0,))
            self.assertEqual(connection.execute('SELECT count(*) FROM qq_official_link_challenges').fetchone(),(0,))
            self.assertEqual(connection.execute('SELECT stopped,stop_generation FROM conversation_settings').fetchone(),(True,5))
        migration.migrate(schema,'0097_budget_notices',downgrade=True)
        migration.migrate(schema,'0098_conversation_links')
        with migration.connect(schema) as connection:
            migration.insert(connection,'qq_official_link_challenges',dict(challenge_id='LC-'+'f'*32,account_key=account.key,
                account_epoch=1,source_endpoint_key=endpoint.key,source_actor_key=ActorKey(account,'group_member','TEST_LINK_SOURCE','TEST_ADMIN').key,
                source_scope_key=endpoint.scope.key,source_generation=1,source_route_epoch=1,source_receipt_hash='a'*64,
                source_authority_hash='b'*64,code_hmac='c'*64,created_at=1,expires_at=601))
        with self.assertRaisesRegex(RuntimeError,'conversation_links_require_export_before_downgrade'):
            migration.migrate(schema,'0097_budget_notices',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0098_conversation_links',))
            self.assertEqual(connection.execute('SELECT count(*) FROM qq_official_link_challenges').fetchone(),(1,))
            self.assertEqual(connection.execute('SELECT stopped,stop_generation FROM conversation_settings').fetchone(),(True,5))
