"""Real PG soft-budget accounting and durable non-replay/notice markers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
import unittest

from src.plugins.ai_chat import daily_budgets as budgets
from src.plugins.ai_chat.model_billing import BillingSource
from src.plugins.ai_chat.model_usage import TokenUsage
from tests import test_model_billing as fixture
from tests import test_model_billing_views as views
from tests import test_qq_official_outbox_migration as migration


class DailyBudgetTests(unittest.TestCase):
    setUp = fixture.ModelBillingTests.setUp
    receipt = views.BillingViewsTests.receipt

    def setting(self,value):
        with self.store.transaction() as cursor:
            cursor.execute('''INSERT INTO conversation_settings(scope_key,daily_budget_usd) VALUES (?,?)
                ON CONFLICT(scope_key) DO UPDATE SET daily_budget_usd=excluded.daily_budget_usd''',
                (self.endpoint.scope.key,value))

    def state(self,*,refresh=False,endpoint=None,now=None):
        with self.store.transaction() as cursor:
            return (budgets.refresh if refresh else budgets.read)(cursor,endpoint or self.endpoint,
                now=self.now if now is None else now)

    def test_amounts_and_beijing_midnight_are_explicit_not_float_or_utc(self):
        self.assertEqual(budgets.amount('0.000001'),Decimal('0.000001'))
        for value in (None,True,0,1.0,'1e2','NaN','-1',' 1','１','1000001','0.0000001'):
            with self.subTest(value=value),self.assertRaises(ValueError):budgets.amount(value)
        midnight = int(datetime(2026,11,14,tzinfo=budgets.ZONE).timestamp())
        self.assertEqual(budgets.interval(midnight),(midnight,midnight+86400))
        self.assertEqual(budgets.interval(midnight-1),(midnight-86400,midnight))
        for clock in (True,-1,None,1.5,2**63):
            with self.assertRaises(ValueError):budgets.interval(clock)

    def test_disabled_default_and_known_lower_bound_are_not_reserved_quota(self):
        self.receipt(); self.receipt(priced=False); self.receipt(usage=None)
        self.setting('1')
        with self.store.transaction() as cursor:
            cursor.execute('''INSERT INTO usage_events(scope_key,source,provider,model,input_tokens,output_tokens,occurred_at)
                VALUES (?,'program_wake_bound','TEST','TEST',999999,888888,?)''',(self.endpoint.scope.key,self.now))
        state = self.state()
        self.assertEqual(state.spent,Decimal('0.00132')); self.assertFalse(state.blocked)
        self.assertEqual((state.incomplete_calls,state.pending_calls),(2,1))
        self.setting(None)
        self.assertFalse(self.state(refresh=True).blocked)
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT count(*) FROM conversation_budget_days').fetchone()[0],0)

    def test_scope_alias_other_endpoint_and_next_day_do_not_borrow_budget_or_fee(self):
        self.setting('0.001'); self.receipt(); self.receipt(endpoint=self.other)
        self.assertTrue(self.state().blocked)
        self.assertIsNone(self.state(endpoint=self.other).limit)
        self.assertEqual(self.state(endpoint=self.history).spent,0)
        state = self.state(refresh=True)
        self.assertFalse(self.state(now=state.day_end).blocked)
        self.assertEqual(self.state(now=state.day_end).generation,state.generation)
        with self.store.transaction() as cursor:
            with self.assertRaisesRegex(PermissionError,'replaced'):
                budgets.require(cursor,self.endpoint,now=state.day_end,generation=state.generation-1)

    def test_concurrent_crossing_restart_and_stop_preserve_once_only_marker(self):
        self.receipt(); self.setting('0.001')
        with self.store.transaction() as cursor:
            cursor.execute('UPDATE conversation_settings SET stopped=TRUE,stop_generation=7 WHERE scope_key=?',(self.endpoint.scope.key,))
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(lambda _:self.state(refresh=True),range(2)))
        self.assertEqual([row.generation for row in states],[1,1])
        self.assertEqual(self.state(refresh=True).generation,1)
        with self.store.transaction() as cursor:
            notice = cursor.execute('SELECT * FROM conversation_budget_days').fetchone()
            self.assertEqual(notice['notice_state'],'pending'); self.assertEqual(notice['spent_usd'],Decimal('0.00132'))
            row = cursor.execute('SELECT stopped,stop_generation,proactive_generation FROM conversation_settings').fetchone()
            self.assertEqual((row['stopped'],row['stop_generation'],row['proactive_generation']),(True,7,1))

    def test_reconfigured_second_crossing_invalidates_work_without_second_notice(self):
        self.setting('0.001'); self.receipt(); self.assertEqual(self.state(refresh=True).generation,1)
        # Simulate the trusted config writer's epoch advance; native/admin entry
        # wiring is tested separately when those interfaces are connected.
        self.setting('1')
        with self.store.transaction() as cursor:
            cursor.execute('UPDATE conversation_settings SET budget_generation=budget_generation+1 WHERE scope_key=?',(self.endpoint.scope.key,))
        self.assertFalse(self.state(refresh=True).blocked)
        self.receipt(usage=TokenUsage(1000000,0,0,0))
        self.assertEqual(self.state(refresh=True).generation,3)
        self.assertEqual(self.state(refresh=True).generation,3)
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT count(*) FROM conversation_budget_days').fetchone()[0],1)
            self.assertEqual(cursor.execute('SELECT budget_usd FROM conversation_budget_days').fetchone()[0],Decimal('0.001'))
            with self.assertRaisesRegex(PermissionError,'exceeded'):budgets.require(cursor,self.endpoint,now=self.now)

    def test_receipt_crossing_commits_before_auto_denial_while_addressed_calls_still_work(self):
        self.setting('0.001')
        call = self.store.begin(self.source,self.profile)
        self.assertEqual(self.state().generation,0)
        self.store.finish(call,TokenUsage(1000,50,600,0))
        self.assertEqual(self.state().generation,1)
        for purpose in ('proactive','program_wake'):
            with self.assertRaisesRegex(PermissionError,'daily_budget_exceeded'):
                self.store.begin(BillingSource(self.endpoint,purpose),self.profile)
        self.receipt()  # Explicitly addressed work is not a hard spending cap.
        self.assertEqual(self.state().spent,Decimal('0.00264'))
        self.assertEqual(self.state().generation,1)

    def test_denied_guard_is_read_only_and_transactions_do_not_leave_half_marker(self):
        self.setting('0'); self.assertTrue(self.state().blocked)
        with self.assertRaises(PermissionError),self.store.transaction() as cursor:
            budgets.require(cursor,self.endpoint,now=self.now)
        with self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'),self.store.transaction() as cursor:
            budgets.refresh(cursor,self.endpoint,now=self.now)
            raise RuntimeError('TEST_ROLLBACK')
        self.assertEqual(self.state().generation,0)
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT count(*) FROM conversation_budget_days').fetchone()[0],0)
        self.assertEqual(self.state(refresh=True).generation,1)
        with self.store.transaction() as cursor:
            with self.assertRaises(ValueError):budgets.read(cursor,self.endpoint.scope,now=self.now)
            self.assertEqual(budgets.require(cursor,self.other,now=self.now,generation=0),0)


class DailyBudgetMigrationTests(unittest.TestCase):
    def test_default_preserves_old_settings_and_only_empty_increment_downgrades(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0095_billing_source_metadata')
        with migration.connect(schema) as connection:
            connection.execute("INSERT INTO conversation_settings(scope_key,stopped,stop_generation,proactive_generation) VALUES ('TEST_SCOPE',TRUE,7,2)")
        migration.migrate(schema,'0096_daily_budgets')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT stopped,stop_generation,proactive_generation,daily_budget_usd,budget_generation FROM conversation_settings').fetchone(),
                (True,7,2,None,0))
        migration.migrate(schema,'0095_billing_source_metadata',downgrade=True)
        migration.migrate(schema,'0096_daily_budgets')
        with migration.connect(schema) as connection:
            connection.execute('UPDATE conversation_settings SET daily_budget_usd=0.01,budget_generation=1')
        with self.assertRaisesRegex(RuntimeError,'daily_budget_requires_export'):
            migration.migrate(schema,'0095_billing_source_metadata',downgrade=True)
