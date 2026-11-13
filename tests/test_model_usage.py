"""A missing/malformed receipt is never turned into a zero-dollar known call."""
from decimal import Decimal
from types import SimpleNamespace
import unittest

from src.plugins.ai_chat.llm_gateway import _anthropic_response
from src.plugins.ai_chat.model_pricing import ModelPricing
from src.plugins.ai_chat.model_usage import TokenUsage


class ModelUsageTests(unittest.TestCase):
    def setUp(self):
        self.pricing = ModelPricing('2','0.2','8')

    def test_normalized_chat_and_responses_cache_buckets(self):
        for raw in ({'prompt_tokens':1000,'completion_tokens':50,'prompt_tokens_details':{'cached_tokens':600}},
                    SimpleNamespace(input_tokens=1000,output_tokens=50,input_tokens_details=SimpleNamespace(cached_tokens=600)),
                    {'prompt_tokens':1000,'completion_tokens':50,'prompt_cache_hit_tokens':600}):
            usage = TokenUsage.read(raw)
            self.assertTrue(usage.known)
            self.assertEqual(usage.quote(self.pricing).usd,Decimal('0.00132'))
            self.assertTrue(usage.quote(self.pricing).complete)
            self.assertFalse(usage.quote(None).complete)

    def test_missing_or_bad_cache_details_only_price_known_buckets(self):
        for raw in (None,{}, {'input_tokens':True,'output_tokens':'2'}):
            receipt = TokenUsage.read(raw)
            self.assertFalse(receipt.known)
            self.assertFalse(receipt.quote(self.pricing).complete)
            self.assertEqual(receipt.quote(self.pricing).usd,0)
        for details in ({},{'cached_tokens':1001},{'cached_tokens':-1}):
            receipt = TokenUsage.read({'input_tokens':1000,'output_tokens':50,'input_tokens_details':details})
            self.assertTrue(receipt.known)
            self.assertFalse(receipt.quote(self.pricing).complete)
            self.assertEqual(receipt.quote(self.pricing).usd,Decimal('0.0004'))
        self.assertTrue(TokenUsage.read({'input_tokens':0,'output_tokens':0}).quote(self.pricing).complete)
        self.assertIsNone(TokenUsage.read({'input_tokens':2**63,'output_tokens':0}).input_tokens)

    def test_anthropic_adapter_preserves_missing_usage_and_includes_actual_cache_writes(self):
        self.assertIsNone(_anthropic_response({'content':[]}).usage)
        missing = TokenUsage.read(_anthropic_response({'content':[],'usage':{}}).usage)
        self.assertFalse(missing.known)
        self.assertFalse(missing.quote(self.pricing).complete)
        response = _anthropic_response({'content':[],'usage':{'input_tokens':300,'output_tokens':50,
            'cache_read_input_tokens':600,'cache_creation_input_tokens':100}})
        receipt = TokenUsage.read(response.usage)
        self.assertEqual(receipt,TokenUsage(1000,50,600,100))
        self.assertEqual(receipt.quote(self.pricing).usd,Decimal('0.00112'))
        self.assertFalse(receipt.quote(self.pricing).complete)

    def test_bad_anthropic_counts_cannot_become_known_zero(self):
        for incoming in (-1,True,1.0,'2',2**63):
            response = _anthropic_response({'content':[],'usage':{'input_tokens':incoming,'output_tokens':1}})
            self.assertIsNone(TokenUsage.read(response.usage).input_tokens)
            self.assertFalse(TokenUsage.read(response.usage).quote(self.pricing).complete)
