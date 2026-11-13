"""Real PostgreSQL accounting windows: partial sums never masquerade as totals."""
from dataclasses import replace
from decimal import Decimal
import unittest

from src.plugins.ai_chat.model_billing import BillingSource
from src.plugins.ai_chat.model_billing_views import windows,status_lines,usd
from src.plugins.ai_chat.model_usage import TokenUsage
from tests import test_model_billing as fixture


class BillingViewsTests(unittest.TestCase):
    setUp = fixture.ModelBillingTests.setUp

    def receipt(self,*,when=None,endpoint=None,priced=True,usage=TokenUsage(1000,50,600,0)):
        now = self.now
        if when is not None:self.now = when
        source = self.source if endpoint is None else BillingSource(endpoint,'proactive')
        call = self.store.begin(source,replace(self.profile,pricing=self.profile.pricing if priced else None))
        if usage is not None:self.store.finish(call,usage)
        self.now = now
        return call

    def totals(self):
        with self.store.transaction() as cursor:return windows(cursor,self.endpoint,now=self.now)

    def test_fixed_windows_include_boundary_and_exclude_old_future_and_foreign_calls(self):
        for when in (self.now,self.now-86400,self.now-86401,self.now-604800,self.now-604801,self.now+1):
            self.receipt(when=when)
        self.receipt(endpoint=self.other)
        totals = self.totals()
        self.assertEqual((totals[86400]['calls'],totals[604800]['calls']),(2,4))
        self.assertEqual(totals[86400]['cost_usd'],Decimal('0.00264'))
        self.assertEqual(totals[604800]['input_tokens'],4000)
        self.assertTrue(totals[86400]['cost_complete'])
        self.assertEqual(totals[86400]['cache_percent'],Decimal('60.0'))
        self.assertNotIn('≥',status_lines(totals))
        with self.store.transaction() as cursor:
            self.assertEqual(windows(cursor,self.history,now=self.now)[604800]['calls'],0)

    def test_unpriced_partial_and_orphan_requests_all_make_cost_a_lower_bound(self):
        self.receipt()
        self.receipt(priced=False)
        self.receipt(usage=TokenUsage(None,5,None,None))
        self.receipt(usage=None)
        self.receipt(usage=TokenUsage(100,0,50,10))
        row = self.totals()[86400]
        self.assertEqual((row['calls'],row['pending_calls'],row['incomplete_cost_calls']),(5,1,4))
        self.assertEqual((row['input_tokens'],row['output_tokens'],row['cached_tokens']),(2100,105,1250))
        self.assertEqual((row['missing_input_calls'],row['missing_output_calls'],row['missing_cache_calls']),(2,1,2))
        self.assertIsNone(row['cache_percent'])
        self.assertEqual(row['cost_usd'],Decimal('0.00145'))
        text = status_lines(self.totals())
        self.assertIn('输入 ≥2,100',text); self.assertIn('输出 ≥105',text)
        self.assertIn('缓存命中 未知',text); self.assertIn('≥ $0.00145',text)
        self.assertIn('4 次费用不完整，其中 1 次未收尾',text)

    def test_cache_hit_rate_is_token_weighted_not_mean_of_calls(self):
        self.receipt()
        self.receipt(usage=TokenUsage(9000,0,0,0))
        self.assertEqual(self.totals()[86400]['cache_percent'],Decimal('6.0'))

    def test_zero_and_empty_are_distinct_and_large_sums_do_not_overflow_bigint(self):
        self.assertIn('暂无已记录',status_lines(self.totals()))
        self.receipt(usage=TokenUsage(0,0,0,0))
        self.assertIn('缓存命中 无输入 · $0',status_lines(self.totals()))
        for _ in range(2):self.receipt(usage=TokenUsage(2**63-1,0,0,0))
        self.assertEqual(self.totals()[86400]['input_tokens'],2*(2**63-1))
        self.assertEqual(usd(Decimal('0.000000000001')),'0.000000000001')

    def test_projection_never_reprices_history_or_counts_quota_reservations(self):
        self.receipt()
        self.profile = replace(self.profile,pricing=None)
        with self.store.transaction() as cursor:
            cursor.execute('''INSERT INTO usage_events(scope_key,source,provider,model,input_tokens,output_tokens,occurred_at)
                VALUES (?,'program_wake_bound','TEST','TEST',999999,888888,?)''',(self.endpoint.scope.key,self.now))
        self.assertEqual(self.totals()[86400]['cost_usd'],Decimal('0.00132'))
        self.assertEqual(self.totals()[86400]['input_tokens'],1000)

    def test_invalid_clock_is_rejected_before_querying(self):
        with self.store.transaction() as cursor:
            for now in (True,-1,2**63,1.5):
                with self.assertRaisesRegex(ValueError,'clock'):windows(cursor,self.endpoint,now=now)
