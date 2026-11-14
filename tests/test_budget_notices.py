"""Frozen budget notice admission only; actual HTTP delivery is tested separately."""
from decimal import Decimal
import unittest

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.adapters.qq_official import budget_notices as notices
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.adapters.qq_official.send_types import OutboxInvariantError
from tests.qq_official_outbox_fixture import OutboxFixture
from tests import test_qq_official_outbox_migration as migration


class BudgetNoticeSourceTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'d':{'user':{'id':'TEST_REAL_BOT','bot':True}}},self.now)
            change(cursor,self.endpoint,'daily_budget_usd','0',actor='TEST_ADMIN',now=self.now)

    def row(self):
        return dict(self.query('SELECT * FROM conversation_budget_days WHERE scope_key=?',(self.endpoint.scope.key,))[0])

    def test_crossing_freezes_source_and_text_but_is_not_a_delivery(self):
        row = self.row()
        self.assertEqual((row['conversation_generation'],row['notice_budget_generation']),(0,2))
        self.assertEqual(row['notice_state'],'pending'); self.assertIsNone(row['notice_plan_id'])
        other = EndpointKey(self.account,'group','TEST_OTHER_GROUP'); self.provision(other)
        with self.tx() as cursor:
            endpoint,actor = notices.check(cursor,self.token,row,now=self.now)
            self.assertEqual(endpoint,self.endpoint); self.assertEqual(actor.identity_kind,'bot')
            plan = notices.rendered(row)
            args = dict(token=self.token,day=row['day_start'],plan=plan,requester=actor,logical_key='budget:'+str(row['day_start']),
                source_scope_key=self.endpoint.scope.key,source_native_message_id='',source_canonical_message_id=None,
                turn_id=None,purpose='unsolicited',expires_at=notices.deadline(row),source_generation=0,now=self.now)
            notices.validate(cursor,**args)
            for changes in ({'requester':self.actor},{'source_scope_key':'TEST_HISTORY'},{'source_native_message_id':'TEST_RECENT'},
                    {'turn_id':1},{'source_generation':1},{'purpose':'final'},{'expires_at':self.now+30},
                    {'plan':render_plan(MessageBody((TextNode(0,'TEST forged'),)),self.endpoint)}):
                with self.subTest(changes=changes),self.assertRaises(OutboxInvariantError):notices.validate(cursor,**(args|changes))
            with self.assertRaises(OutboxInvariantError):
                notices.validate(cursor,**(args|{'plan':render_plan(MessageBody((TextNode(0,'TEST'),)),other)}))
        self.assertIn('群内只响应 @',plan.parts[0].canonical_text)
        self.assertIn('每天只尝试一次',plan.parts[0].canonical_text)
        self.assertEqual(self.query('SELECT * FROM deliveries WHERE endpoint_key=?',(self.endpoint.key,)),[])

    def test_stop_restore_budget_restore_route_and_new_day_cannot_refresh_original_authorization(self):
        row = self.row()
        with self.tx() as cursor:
            cursor.execute('UPDATE conversation_settings SET stop_generation=stop_generation+1 WHERE scope_key=?',(self.endpoint.scope.key,))
            with self.assertRaises(PermissionError):notices.check(cursor,self.token,row,now=self.now)
            cursor.execute('UPDATE conversation_settings SET stop_generation=0 WHERE scope_key=?',(self.endpoint.scope.key,))
            change(cursor,self.endpoint,'daily_budget_usd','off',actor='TEST_ADMIN',now=self.now)
            change(cursor,self.endpoint,'daily_budget_usd','0',actor='TEST_ADMIN',now=self.now)
            saved = dict(cursor.execute('SELECT * FROM conversation_budget_days WHERE scope_key=?',(self.endpoint.scope.key,)).fetchone())
            self.assertEqual(saved['notice_budget_generation'],2)
            self.assertGreater(saved['last_blocked_generation'],2)
            with self.assertRaises(PermissionError):notices.check(cursor,self.token,saved,now=self.now)
        for changes,clock in (({'route_epoch':2},self.now),({},notices.deadline(row)),({},notices.interval(self.now)[1])):
            with self.tx() as cursor,self.assertRaises(PermissionError):notices.check(cursor,self.token,row|changes,now=clock)
        self.assertEqual(len(self.query('SELECT * FROM conversation_budget_days WHERE scope_key=?',(self.endpoint.scope.key,))),1)

    def test_original_capture_cannot_be_rewritten_or_guessed_for_legacy_rows(self):
        row = self.row()
        for column,value in (('conversation_generation',1),('notice_budget_generation',9),('budget_usd',Decimal('1')),
                ('spent_usd',Decimal('10')),('account_epoch',self.token.epoch+1),('endpoint_key','TEST_REPLACED')):
            with self.subTest(column=column),self.assertRaises(DatabaseError),self.tx() as cursor:
                cursor.execute('UPDATE conversation_budget_days SET '+column+'=? WHERE scope_key=?',(value,self.endpoint.scope.key))
        with self.tx() as cursor,self.assertRaises(PermissionError):
            notices.check(cursor,self.token,row|dict(conversation_generation=None,notice_budget_generation=None),now=self.now)
        self.account_lease.close(); current = self.lease().acquire()
        with self.tx() as cursor,self.assertRaises(PermissionError):notices.check(cursor,current,row,now=self.now)


class BudgetNoticeMigrationTests(unittest.TestCase):
    def test_upgrade_cancels_unbound_legacy_notice_and_empty_increment_downgrades(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0096_daily_budgets')
        with migration.connect(schema) as connection:
            connection.execute("INSERT INTO qq_official_accounts(account_key,enabled,gateway_state,created_at,updated_at) VALUES ('TEST_ACCOUNT',TRUE,'idle',1,1)")
            connection.execute("""INSERT INTO qq_official_endpoints(endpoint_key,account_key,kind,native_conversation_id,
                canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                VALUES ('TEST_EP','TEST_ACCOUNT','group','TEST_GROUP','TEST_SCOPE','active','present',1,1)""")
            connection.execute("""INSERT INTO conversation_budget_days(scope_key,day_start,endpoint_key,account_epoch,
                endpoint_generation,route_epoch,first_exceeded_at,budget_usd,spent_usd,last_blocked_generation)
                VALUES ('TEST_SCOPE',0,'TEST_EP',1,1,1,1,0,0,2)""")
        migration.migrate(schema,'0097_budget_notices')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT notice_state,conversation_generation,notice_budget_generation FROM conversation_budget_days').fetchone(),
                ('cancelled',None,None))
        migration.migrate(schema,'0096_daily_budgets',downgrade=True)
        migration.migrate(schema,'0097_budget_notices')
        with migration.connect(schema) as connection:
            connection.execute("DELETE FROM conversation_budget_days")
            connection.execute("""INSERT INTO conversation_budget_days(scope_key,day_start,endpoint_key,account_epoch,
                endpoint_generation,route_epoch,first_exceeded_at,budget_usd,spent_usd,last_blocked_generation,
                conversation_generation,notice_budget_generation) VALUES ('TEST_SCOPE',0,'TEST_EP',1,1,1,1,0,0,2,0,2)""")
        with self.assertRaisesRegex(RuntimeError,'budget_notice_requires_export'):
            migration.migrate(schema,'0096_daily_budgets',downgrade=True)
