"""Exact configured prices, without inventing usage or borrowing another model's rate."""
from decimal import Decimal,Inexact,Rounded,localcontext
import json
import unittest

from src.plugins.ai_chat.model_catalog import ModelCatalog,ModelCatalogError
from src.plugins.ai_chat.model_pricing import ModelPricing
from src.plugins.ai_chat.admin_api.model_views import _model_overview


class ModelPricingTests(unittest.TestCase):
    def setUp(self):
        self.rates = {'input_usd_per_million':'2','cached_input_usd_per_million':'0.2','output_usd_per_million':'8'}

    def catalog(self,pricing=None):
        return ModelCatalog.from_json(json.dumps({'default':'test','profiles':{
            'test':{'provider':'openai','model':'TEST_MODEL','api_key':'TEST_SECRET','aliases':['alias'],'pricing':pricing},
            'other':{'provider':'openai','model':'TEST_OTHER','api_key_required':False}}}),default_profile='test',environ={})

    def test_optional_prices_are_unknown_not_zero_and_aliases_keep_exact_prices(self):
        self.assertIsNone(self.catalog().default.pricing)
        priced = self.catalog(self.rates)
        self.assertEqual(priced.resolve('alias').pricing.to_mapping(),{
            'input_usd_per_million':'2.000000','cached_input_usd_per_million':'0.200000','output_usd_per_million':'8.000000'})
        self.assertIsNone(priced.resolve('other').pricing)
        changed = priced.default.with_model('TEST_CHANGED')
        self.assertIsNone(changed.pricing)
        self.assertIs(priced.default.with_model('TEST_MODEL'),priced.default)
        self.assertIsNone(priced.resolve_runtime(profile='test',model='TEST_OTHER').pricing)
        public = _model_overview(priced)
        self.assertEqual(public['profiles'][0]['pricing'],priced.default.pricing.to_mapping())
        self.assertNotIn('TEST_SECRET',json.dumps(public))

    def test_incomplete_invalid_or_unbounded_rates_fail_without_echoing_values(self):
        for raw in ({},[],{**self.rates,'currency':'CNY'}, {'input_usd_per_million':2}):
            with self.subTest(raw=raw),self.assertRaises(ModelCatalogError):self.catalog(raw)
        for value in (True,False,None,-1,'-0','NaN','Infinity',float('inf'),float('nan'),
                      '0.0000001','1e-1000000','1e1000000','1000000.000001','TEST_SECRET','１',' 2 '):
            with self.subTest(value=value),self.assertRaises(ModelCatalogError) as error:
                self.catalog({**self.rates,'input_usd_per_million':value})
            self.assertNotIn('TEST_SECRET',str(error.exception))
        self.assertEqual(self.catalog({name:0 for name in self.rates}).default.pricing.quote(input_tokens=1,output_tokens=1).usd,0)
        self.assertEqual(self.catalog({name:1e-6 for name in self.rates}).default.pricing.input_usd_per_million,Decimal('0.000001'))

    def test_exact_cache_read_discount_and_incomplete_cache_creation_quote(self):
        pricing = ModelPricing.from_mapping(self.rates)
        quote = pricing.quote(input_tokens=1000,output_tokens=50,cached_tokens=600)
        self.assertEqual(quote.usd,Decimal('0.00132'))
        self.assertTrue(quote.complete)
        partial = pricing.quote(input_tokens=1000,output_tokens=50,cached_tokens=600,cache_write_tokens=100)
        self.assertEqual(partial.usd,Decimal('0.00112'))
        self.assertFalse(partial.complete)

    def test_arithmetic_is_independent_of_ambient_decimal_context(self):
        pricing = ModelPricing.from_mapping(self.rates)
        counts = dict(input_tokens=2**63-1,output_tokens=2**63-1,cached_tokens=1)
        expected = pricing.quote(**counts)
        with localcontext() as context:
            context.prec = 2
            context.Emax = 3
            context.traps[Inexact] = context.traps[Rounded] = True
            new = ModelPricing.from_mapping({**self.rates,'input_usd_per_million':'2.00000000'})
            actual = new.quote(**counts)
        self.assertEqual(actual,expected)
        self.assertEqual(ModelPricing('0.1','0.01','0.2').quote(input_tokens=1,output_tokens=1).usd,Decimal('0.0000003'))

    def test_only_complete_valid_token_counts_can_be_quoted(self):
        pricing = ModelPricing.from_mapping(self.rates)
        for counts in ({'input_tokens':True,'output_tokens':1},{'input_tokens':None,'output_tokens':1},
                       {'input_tokens':1.0,'output_tokens':1},{'input_tokens':-1,'output_tokens':1},
                       {'input_tokens':2**63,'output_tokens':1},{'input_tokens':1,'output_tokens':1,'cached_tokens':2},
                       {'input_tokens':1,'output_tokens':1,'cached_tokens':1,'cache_write_tokens':1}):
            with self.subTest(counts=counts),self.assertRaises(ValueError):pricing.quote(**counts)
