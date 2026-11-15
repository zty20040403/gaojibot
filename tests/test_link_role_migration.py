import unittest

from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests import test_qq_official_outbox_migration as migration


class LinkRoleMigrationTests(unittest.TestCase):
    def test_empty_role_increment_rolls_back_but_observed_authority_needs_export(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0098_conversation_links')
        account = AccountKey('qq-official','TEST_LINK_ROLE_MIGRATION','test'); endpoint = EndpointKey(account,'group','TEST_GROUP')
        with migration.connect(schema) as connection:
            migration.insert(connection,'qq_official_accounts',dict(account_key=account.key,enabled=True,fencing_epoch=1,created_at=1,updated_at=1))
            migration.insert(connection,'qq_official_endpoints',dict(endpoint_key=endpoint.key,account_key=account.key,kind='group',
                native_conversation_id='TEST_GROUP',canonical_scope_key=endpoint.scope.key,binding_state='active',
                membership_state='present',created_at=1,updated_at=1))
        migration.migrate(schema,'0099_link_role_evidence')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM qq_official_link_roles').fetchone(),(0,))
            self.assertEqual(connection.execute('SELECT count(*) FROM qq_official_endpoints').fetchone(),(1,))
        migration.migrate(schema,'0098_conversation_links',downgrade=True)
        migration.migrate(schema,'0099_link_role_evidence')
        with migration.connect(schema) as connection:
            migration.insert(connection,'qq_official_link_roles',dict(account_key=account.key,endpoint_key=endpoint.key,
                actor_key=ActorKey(account,'group_member','TEST_GROUP','TEST_ADMIN').key,endpoint_generation=1,route_epoch=1,
                role='admin',revision=1,observed_at=1,receipt_hash='a'*64))
        with self.assertRaisesRegex(RuntimeError,'link_role_evidence_requires_export'):
            migration.migrate(schema,'0098_conversation_links',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0099_link_role_evidence',))
            self.assertEqual(connection.execute('SELECT role,revision FROM qq_official_link_roles').fetchone(),('admin',1))
