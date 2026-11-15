"""Fixed settings, physical scope, atomic audit and independent proactive fencing."""
import unittest

from src.plugins.ai_chat import conversation_config as config
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_ingest as fixture,test_qq_official_outbox_migration as migration


class ConfigParsingTests(unittest.TestCase):
    def test_list_read_set_and_default_have_unambiguous_types(self):
        self.assertEqual(config.parse(''),('',None))
        for name,item in config.SETTINGS.items():
            self.assertEqual(config.parse(name),(name,None))
            self.assertEqual(config.parse(name+' default'),(name,item.default))
        self.assertEqual(config.parse('proactive_enabled off'),('proactive_enabled',False))
        self.assertEqual(config.parse('image_captions on'),('image_captions',True))
        self.assertEqual(config.parse('program_say_limit 0'),('program_say_limit',0))
        for text in ('scope_key TEST_OTHER','image_captions yes','proactive_enabled 1','agent_timeout_hours 0',
                     'agent_timeout_hours 7','program_wake_limit 10001','program_say_limit -1',
                     'agent_timeout_hours １','agent_timeout_hours 1.0','agent_timeout_hours 1;DELETE','agent_timeout_hours 1 extra'):
            with self.subTest(text=text),self.assertRaises(ValueError):config.parse(text)
        for value in (True,1.0,'1',None):
            with self.assertRaises(ValueError):config.SETTINGS['agent_timeout_hours'].validate(value)


    def test_daily_dollar_budget_is_exact_off_is_not_read_and_invalid_values_reject(self):
        self.assertEqual(config.parse('daily_budget_usd 0.001000'),('daily_budget_usd','0.001'))
        self.assertEqual(config.parse('daily_budget_usd off'),('daily_budget_usd','off'))
        self.assertEqual(config.parse('daily_budget_usd default'),('daily_budget_usd','off'))
        self.assertEqual(config.parse('daily_budget_usd 0'),('daily_budget_usd','0'))
        for value in (None,True,1,1.0,'1e2','NaN','-1','１','0.0000001'):
            with self.assertRaises(ValueError):config.SETTINGS['daily_budget_usd'].validate(value)


class ConversationConfigTests(unittest.TestCase):
    setUp = fixture.QQOfficialIngestTests.setUp
    tx = fixture.QQOfficialIngestTests.tx
    query = fixture.QQOfficialIngestTests.query
    provision = fixture.QQOfficialIngestTests.provision

    def change(self,name,value):
        with self.tx() as cursor:
            return config.change(cursor,self.endpoint,name,value,actor='TEST_ADMIN',now=fixture.NOW)

    def test_defaults_limits_and_generation_do_not_clear_stop_or_follow_history(self):
        other = EndpointKey(self.account,'group','TEST_HISTORY')
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=?',(other.scope.key,))
        with self.tx() as cursor:
            values = config.snapshot(cursor,self.endpoint)
            self.assertEqual(values,{name:item.default for name,item in config.SETTINGS.items()})
            self.assertIn('只影响新任务',config.display(values))
            cursor.execute('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,7)',(self.endpoint.scope.key,))
        self.change('proactive_enabled',True)
        self.change('proactive_enabled',False)
        self.change('proactive_enabled',False)
        self.change('proactive_enabled',True)
        for name,value in (('program_say_limit',0),('program_wake_limit',10000),('agent_timeout_hours',1),('image_captions',True)):
            self.assertEqual(self.change(name,value)[name],value)
        with self.tx() as cursor:
            self.assertEqual(config.proactive_state(cursor,self.endpoint),(True,2))
            self.assertEqual(config.snapshot(cursor,other),{name:item.default for name,item in config.SETTINGS.items()})
        row = self.query('SELECT stopped,stop_generation FROM conversation_settings')[0]
        self.assertEqual((row['stopped'],row['stop_generation']),(True,7))
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),8)
        self.assertEqual({row['resource_key']:row['version'] for row in self.query('SELECT * FROM admin_resource_versions')},
            {'conversations':4,'programs':2,'commands':1,'captions':1})

    def test_budget_change_uses_exact_audit_and_generations_without_clearing_stop(self):
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,7)',(self.endpoint.scope.key,))
        self.assertEqual(self.change('daily_budget_usd','0.001000')['daily_budget_usd'],'0.001')
        self.change('daily_budget_usd','0.001')
        row = self.query('SELECT budget_generation,proactive_generation,stop_generation,stopped FROM conversation_settings')[0]
        self.assertEqual((row['budget_generation'],row['proactive_generation'],row['stop_generation'],row['stopped']),(1,1,7,True))
        self.assertEqual(self.change('daily_budget_usd','off')['daily_budget_usd'],'off')
        row = self.query('SELECT daily_budget_usd,budget_generation FROM conversation_settings')[0]
        self.assertEqual((row['daily_budget_usd'],row['budget_generation']),(None,2))
        self.assertEqual(self.query('SELECT version FROM admin_resource_versions WHERE resource_key=?',('budgets',))[0]['version'],3)
        audit = self.query('SELECT after_json FROM admin_audit_log WHERE resource_key=? ORDER BY audit_id',('budgets',))
        self.assertIn('"0.001"',audit[0]['after_json']); self.assertIn('"off"',audit[-1]['after_json'])

    def test_invalid_write_and_transaction_rollback_leave_no_settings_or_audit(self):
        for name,value in (('program_say_limit',True),('image_captions',1),('unknown',2)):
            with self.assertRaises(ValueError):self.change(name,value)
        with self.assertRaisesRegex(RuntimeError,'TEST rollback'):
            with self.tx() as cursor:
                config.change(cursor,self.endpoint,'agent_timeout_hours',2,actor='TEST_ADMIN',now=fixture.NOW)
                raise RuntimeError('TEST rollback')
        for table in ('conversation_settings','admin_audit_log','admin_resource_versions'):
            self.assertEqual(self.query('SELECT * FROM '+table),[])

    def test_real_migration_preserves_existing_values_and_guards_generation_downgrade(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0092_caption_scopes')
        with migration.connect(schema) as connection:
            connection.execute("INSERT INTO conversation_settings(scope_key,stopped,agent_timeout_hours) VALUES ('TEST_SCOPE',TRUE,3)")
        migration.migrate(schema,'0093_conversation_config')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT stopped,agent_timeout_hours,proactive_enabled,proactive_generation FROM conversation_settings').fetchone(),
                (True,3,True,0))
            connection.execute('UPDATE conversation_settings SET proactive_enabled=FALSE,proactive_generation=1')
        with self.assertRaisesRegex(RuntimeError,'conversation_config_requires_export'):
            migration.migrate(schema,'0092_caption_scopes',downgrade=True)
        with migration.connect(schema) as connection:
            connection.execute('UPDATE conversation_settings SET proactive_enabled=TRUE,proactive_generation=0')
        migration.migrate(schema,'0092_caption_scopes',downgrade=True)
