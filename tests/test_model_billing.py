from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import unittest

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.model_billing import BillingSource,ModelBilling
from src.plugins.ai_chat.model_pricing import ModelPricing
from src.plugins.ai_chat.model_usage import TokenUsage
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests.postgres_fixture import isolated_postgres_for_test
from tests.test_llm_gateway import profile


class ModelBillingTests(unittest.TestCase):
    def setUp(self):
        self.now = 360000
        self.store = ModelBilling(isolated_postgres_for_test(self),clock=lambda:self.now)
        account = AccountKey('qq-official','TEST_BILLING','test')
        self.endpoint = EndpointKey(account,'group','TEST_PHYSICAL')
        self.other = EndpointKey(account,'group','TEST_OTHER')
        self.history = EndpointKey(account,'group','TEST_HISTORY')
        self.actor = ActorKey(account,'group_member','TEST_PHYSICAL','TEST_CALLER')
        self.source = BillingSource(self.endpoint,'turn',self.actor)
        self.profile = replace(profile('test',api_key='TEST_PRIVATE_KEY'),pricing=ModelPricing('2','0.2','8'))
        with self.store.transaction() as cursor:
            cursor.execute('''INSERT INTO qq_official_accounts(account_key,enabled,fencing_epoch,gateway_state,created_at,updated_at)
                VALUES (?,TRUE,1,'active',1,1)''',(account.key,))
            for physical,history in ((self.endpoint,self.history),(self.other,self.other)):
                cursor.execute('''INSERT INTO qq_official_endpoints
                    (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                    VALUES (?,?,'group',?,?,'active','present',1,1)''',
                    (physical.key,account.key,physical.native_conversation_id,history.scope.key))

    def row(self,call_id):
        with self.store.transaction() as cursor:
            return dict(cursor.execute('SELECT * FROM model_usage_calls WHERE call_id=?',(call_id,)).fetchone())

    def test_source_is_physical_and_price_is_the_start_snapshot(self):
        call = self.store.begin(self.source,self.profile)
        self.profile = replace(self.profile,pricing=ModelPricing('20','2','80'))
        self.now += 10
        self.assertTrue(self.store.finish(call,TokenUsage(1000,50,600,0)))
        row = self.row(call)
        self.assertEqual(row['cost_usd'],Decimal('0.00132'))
        self.assertTrue(row['cost_complete'])
        self.assertEqual(row['scope_key'],self.endpoint.scope.key)
        self.assertNotIn(self.history.scope.key,str(row))
        self.assertNotIn(self.profile.api_key,str(row))
        self.assertEqual(row['actor_key'],self.actor.key)
        self.assertFalse(self.store.finish(call,TokenUsage(1,0,0,0)))
        self.assertEqual(self.row(call),row)

    def test_unknown_counts_prices_and_pending_calls_are_not_free_known_receipts(self):
        for pricing,usage,expected in ((None,TokenUsage(1000,50,600,0),'0'),
                (self.profile.pricing,TokenUsage.read(None),'0'),
                (self.profile.pricing,TokenUsage(1000,50,None,0),'0.0004'),
                (self.profile.pricing,TokenUsage(1000,50,600,100),'0.00112')):
            call = self.store.begin(self.source,replace(self.profile,pricing=pricing))
            self.assertIsNone(self.row(call)['finished_at'])
            self.assertFalse(self.row(call)['cost_complete'])
            self.store.finish(call,usage)
            self.assertFalse(self.row(call)['cost_complete'])
            self.assertEqual(self.row(call)['cost_usd'],Decimal(expected))
        call = self.store.begin(self.source,self.profile)
        self.store = ModelBilling(self.store.database,clock=lambda:self.now)
        self.assertIsNone(self.row(call)['input_tokens'])
        self.assertIsNone(self.row(call)['finished_at'])

    def test_begin_can_share_admission_transaction_without_partial_history(self):
        with self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'),self.store.transaction() as cursor:
            self.store.begin(self.source,self.profile,transaction_cursor=cursor)
            raise RuntimeError('TEST_ROLLBACK')
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT count(*) AS n FROM model_usage_calls').fetchone()['n'],0)

    def test_receipt_survives_stop_and_is_write_once_under_concurrency(self):
        call = self.store.begin(self.source,self.profile)
        with self.store.transaction() as cursor:
            cursor.execute('INSERT INTO conversation_settings(scope_key,stopped) VALUES (?,TRUE)',(self.endpoint.scope.key,))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _:self.store.finish(call,TokenUsage(1000,50,600,0)),range(2)))
        self.assertEqual(sorted(results),[False,True])
        self.assertTrue(self.row(call)['cost_complete'])

    def test_database_rejects_repricing_reassignment_and_receipt_rewrite(self):
        call = self.store.begin(self.source,self.profile)
        for column,value in (('pricing_json','{}'),('scope_key',self.other.scope.key),('actor_key',None),('model','TEST_OTHER_MODEL')):
            with self.assertRaises(DatabaseError) as caught,self.store.transaction() as cursor:
                cursor.execute('UPDATE model_usage_calls SET '+column+'=? WHERE call_id=?',(value,call))
            self.assertIn('immutable_model_usage_call',str(caught.exception.__cause__))
        self.store.finish(call,TokenUsage.read(None))
        with self.assertRaises(DatabaseError) as caught,self.store.transaction() as cursor:
            cursor.execute('UPDATE model_usage_calls SET cost_usd=1 WHERE call_id=?',(call,))
        self.assertIn('immutable_model_usage_call',str(caught.exception.__cause__))

    def test_job_turn_and_program_sources_cannot_be_forged_or_cross_scope(self):
        with self.assertRaisesRegex(ValueError,'actor_endpoint'):
            replace(self.source,endpoint=self.other)
        for field in ('background_job_id','turn_id'):
            with self.assertRaisesRegex(ValueError,'source_mismatch'):
                self.store.begin(replace(self.source,**{field:99999}),self.profile)
        wake_id = 'f'*32
        with self.store.transaction() as cursor:
            cursor.execute('''INSERT INTO qq_official_program_wakes(wake_id,account_key,endpoint_key,due_at,funded,created_at)
                VALUES (?,?,?,?,FALSE,?)''',(wake_id,self.endpoint.account.key,self.other.key,self.now,self.now))
        with self.assertRaisesRegex(ValueError,'impersonate'):
            replace(self.source,program_wake_id=wake_id)
        with self.assertRaisesRegex(ValueError,'wake_source_mismatch'):
            self.store.begin(BillingSource(self.endpoint,'program_wake',program_wake_id=wake_id),self.profile)
        call = self.store.begin(BillingSource(self.other,'program_wake',program_wake_id=wake_id),self.profile)
        self.assertIsNone(self.row(call)['actor_key'])
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT count(*) AS n FROM model_usage_calls').fetchone()['n'],1)
