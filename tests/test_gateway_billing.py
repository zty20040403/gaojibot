import asyncio
from dataclasses import replace
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from src.bot_storage.async_calls import DatabaseCalls
from src.plugins.ai_chat.billing_context import BillingContext,billing_scope,active_billing
from src.plugins.ai_chat.llm_gateway import LLMGateway,LLMConnectionError,LLMConfigError,LLMProviderError,OpenAIChatProvider
from src.plugins.ai_chat.model_budget import ModelCallBudget,ModelBudgetExceeded,model_budget_scope
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.model_pricing import ModelPricing
from tests import test_model_billing as fixture
from tests.test_llm_gateway import FakeStream,stream_chunk
from tests.test_model_budget import completion


class GatewayBillingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.ModelBillingTests.setUp(self)
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)
        self.context = BillingContext(self.store,self.source,self.calls)
        self.provider = SimpleNamespace(create_completion=AsyncMock(return_value=completion(1000,50,600)),close=AsyncMock())
        self.gateway = LLMGateway({'openai-chat':self.provider})

    def rows(self):
        with self.store.transaction() as cursor:
            return [dict(row) for row in cursor.execute('SELECT * FROM model_usage_calls ORDER BY started_at,call_id').fetchall()]

    async def test_retry_fallback_each_keep_their_own_actual_profile_and_snapshot(self):
        primary = replace(self.profile,fallback_profiles=('backup',))
        backup = replace(self.profile,name='backup',model='TEST_BACKUP',pricing=ModelPricing('20','2','80'))
        self.gateway = LLMGateway({'openai-chat':self.provider},catalog=ModelCatalog(
            {p.name:p for p in (primary,backup)},default_profile=primary.name))
        self.provider.create_completion.side_effect = [LLMProviderError('test',400,'tool_choice_not_supported'),
            LLMConnectionError('TEST_NETWORK'),completion(1000,50,600)]
        with billing_scope(self.context):
            response = await self.gateway.create_completion_with_profile(primary,messages=[],tools=[{'type':'function'}],
                tool_choice={'type':'function','function':{'name':'reply'}})
        self.assertEqual(response.profile,backup)
        self.assertIsNone(active_billing())
        rows = self.rows()
        self.assertEqual(len(rows),3)
        self.assertEqual(sum(row['cost_complete'] for row in rows),1)
        row = next(row for row in rows if row['cost_complete'])
        self.assertEqual((row['profile'],row['cost_usd']),('backup',Decimal('0.0132')))
        self.assertTrue(all(row['finished_at'] is not None for row in rows))

    async def test_stream_receipt_and_early_close_are_distinct_from_quota_bounds(self):
        full = FakeStream(stream_chunk('TEST_TEXT'),stream_chunk(finish='stop'),completion(1000,50,600))
        early = FakeStream(stream_chunk('TEST_TEXT'),stream_chunk('TEST_MORE'))
        self.provider.create_completion.side_effect = [full,early]
        with billing_scope(self.context):
            response = await self.gateway.create_completion(self.profile,messages=[],stream=True)
        async for _ in response:pass  # Accounting survives leaving its context.
        await response.close()
        self.assertEqual(full.close_count,1)
        budget = ModelCallBudget(10000)
        with billing_scope(self.context),model_budget_scope(budget):
            response = await self.gateway.create_completion(self.profile,messages=[],stream=True)
            await response.close()
            await response.close()
        self.assertEqual(early.close_count,1)
        self.assertGreater(budget.charged_tokens,0)
        rows = self.rows()
        self.assertEqual(sum(row['cost_usd'] for row in rows),Decimal('0.00132'))
        self.assertEqual(sum(row['input_tokens'] is None for row in rows),1)
        self.assertEqual(sum(row['cost_complete'] for row in rows),1)
        self.assertTrue(self.provider.create_completion.call_args.kwargs['stream_options']['include_usage'])

    async def test_final_usage_is_preserved_if_stream_fails_after_its_receipt(self):
        stream = FakeStream(stream_chunk('TEST'),completion(1000,50,600),LLMConnectionError('TEST_LOST_EOF'))
        self.provider.create_completion.return_value = stream
        with billing_scope(self.context):
            response = await self.gateway.create_completion(self.profile,messages=[],stream=True)
            with self.assertRaises(LLMConnectionError):
                async for _ in response:pass
        self.assertTrue(self.rows()[0]['cost_complete'])
        self.assertEqual(stream.close_count,1)

    async def test_denied_budget_missing_accounting_and_model_override_never_reach_provider(self):
        budget = ModelCallBudget(10000,reserve_request=AsyncMock(side_effect=ModelBudgetExceeded('TEST_DENIED')))
        with billing_scope(self.context),model_budget_scope(budget),self.assertRaises(ModelBudgetExceeded):
            await self.gateway.create_completion(self.profile,messages=[])
        with billing_scope(self.context),self.assertRaises(LLMConfigError):
            await self.gateway._create_single(self.profile,messages=[],extra_body={'model':'TEST_BYPASS_PRICE'})
        with patch.object(self.store,'begin',side_effect=RuntimeError('TEST_DB_DOWN')):
            with billing_scope(self.context),self.assertRaisesRegex(RuntimeError,'TEST_DB_DOWN'):
                await self.gateway.create_completion(self.profile,messages=[])
        self.provider.create_completion.assert_not_awaited()
        self.assertEqual(self.rows(),[])

    async def test_public_request_cannot_override_priced_profile(self):
        with billing_scope(self.context):
            await self.gateway.create_completion(self.profile,messages=[],model='TEST_OTHER',extra_body={'model':'TEST_OTHER'})
        self.assertEqual(self.provider.create_completion.call_args.kwargs['model'],self.profile.model)
        self.assertNotIn('extra_body',self.provider.create_completion.call_args.kwargs)
        self.assertEqual(self.rows()[0]['cost_usd'],Decimal('0.00132'))

    async def test_double_cancellation_drains_admission_and_settlement_without_losing_known_cost(self):
        for method in ('begin','finish'):
            entered,release = Event(),Event()
            original = getattr(self.store,method)
            def blocking(*args,**kwargs):
                entered.set()
                if not release.wait(10):raise RuntimeError('TEST_RELEASE_TIMEOUT')
                return original(*args,**kwargs)
            with patch.object(self.store,method,side_effect=blocking),billing_scope(self.context):
                task = asyncio.create_task(self.gateway.create_completion(self.profile,messages=[]))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,5))
                    task.cancel(); await asyncio.sleep(0)
                    task.cancel(); await asyncio.sleep(0)
                    self.assertFalse(task.done())
                finally:release.set()
                with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,5)
        rows = self.rows()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['finished_at'] is not None for row in rows))
        self.assertEqual(sum(row['cost_complete'] for row in rows),1)
        self.assertEqual(sum(row['cost_usd'] for row in rows),Decimal('0.00132'))
        self.assertEqual(self.provider.create_completion.await_count,1)

    async def test_source_is_rechecked_after_admission_and_sdk_retries_are_disabled(self):
        current = AsyncMock(side_effect=[None,PermissionError('TEST_REVOKED')])
        with billing_scope(replace(self.context,current=current)),self.assertRaises(PermissionError):
            await self.gateway.create_completion(self.profile,messages=[])
        self.provider.create_completion.assert_not_awaited()
        self.assertFalse(self.rows()[0]['cost_complete'])
        client = Mock(); client.with_options.return_value.chat.completions.create = AsyncMock(return_value=completion())
        provider = OpenAIChatProvider()
        with patch.object(provider,'_client',return_value=client),billing_scope(self.context):
            await provider.create_completion(self.profile,messages=[])
        client.with_options.assert_called_once_with(max_retries=0)
