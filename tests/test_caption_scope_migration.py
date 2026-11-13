"""A real pre-0092 alias upgrade cannot silently enable another physical chat."""
import unittest

from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_qq_official_outbox_migration as migration


class CaptionScopeMigrationTests(unittest.TestCase):
    def test_upgrade_disables_alias_grants_preserves_other_controls_and_never_refunds(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0091_ops_replies')
        account = AccountKey('qq-official','TEST_CAPTION_MIGRATION','test')
        endpoint = EndpointKey(account,'group','TEST_ROUTED_"群\\')
        history = EndpointKey(account,'group','TEST_HISTORY')
        regular = EndpointKey(account,'group','TEST_REGULAR')
        with migration.connect(schema) as connection:
            connection.execute('''INSERT INTO qq_official_accounts(account_key,enabled,fencing_epoch,gateway_state,created_at,updated_at)
                VALUES (%s,TRUE,1,'active',1,1)''',(account.key,))
            for physical,target in ((endpoint,history),(regular,regular)):
                connection.execute('''INSERT INTO qq_official_endpoints
                    (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                    VALUES (%s,%s,'group',%s,%s,'active','present',1,1)''',
                    (physical.key,account.key,physical.native_conversation_id,target.scope.key))
            for scope,count in ((endpoint.scope.key,3),(history.scope.key,7),(regular.scope.key,2)):
                connection.execute('''INSERT INTO conversation_settings(scope_key,image_captions,image_daily_limit,stopped,stop_generation,
                    program_say_limit,agent_timeout_hours) VALUES (%s,TRUE,99,TRUE,4,12,3)''',(scope,))
                connection.execute("INSERT INTO image_caption_daily VALUES (%s,'2026-11-13',%s)",(scope,count))
        migration.migrate(schema,'0092_caption_scopes')
        with migration.connect(schema) as connection:
            rows = {row[0]:row[1:] for row in connection.execute('''SELECT scope_key,image_captions,image_daily_limit,
                stopped,stop_generation,program_say_limit,agent_timeout_hours FROM conversation_settings''')}
            self.assertEqual(rows[endpoint.scope.key],(False,200,True,4,12,3))
            self.assertEqual(rows[history.scope.key],(False,200,True,4,12,3))
            self.assertEqual(rows[regular.scope.key],(True,99,True,4,12,3))
            counts = dict(connection.execute('SELECT scope_key,admitted FROM image_caption_daily'))
            self.assertEqual(counts,{endpoint.scope.key:7,history.scope.key:7,regular.scope.key:2})
        with self.assertRaisesRegex(RuntimeError,'caption_alias_policy_requires_export'):
            migration.migrate(schema,'0091_ops_replies',downgrade=True)
        with migration.connect(schema) as connection:
            connection.execute('DELETE FROM image_caption_daily WHERE scope_key IN (%s,%s)',(endpoint.scope.key,history.scope.key))
            connection.execute('UPDATE conversation_settings SET image_captions=TRUE WHERE scope_key=%s',(endpoint.scope.key,))
        with self.assertRaisesRegex(RuntimeError,'caption_alias_policy_requires_export'):
            migration.migrate(schema,'0091_ops_replies',downgrade=True)
        with migration.connect(schema) as connection:
            connection.execute('UPDATE conversation_settings SET image_captions=FALSE WHERE scope_key=%s',(endpoint.scope.key,))
        migration.migrate(schema,'0091_ops_replies',downgrade=True)
