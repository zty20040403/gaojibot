from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import nonebot

nonebot.init()

from src.plugins.ai_chat.llm_gateway import (
    LLMConnectionError,
    LLMGateway,
    LLMProviderError,
    OpenAIChatProvider,
    _anthropic_request,
)
from src.plugins.ai_chat.model_budget import (
    ModelCallBudget,
    ModelBudgetExceeded,
    active_model_budget,
    model_budget_scope,
)
from src.plugins.ai_chat.model_catalog import ModelCatalog
from tests.test_llm_gateway import profile, FakeStream, stream_chunk


def completion(incoming=30, outgoing=10, cached=0):
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=incoming,
            completion_tokens=outgoing,
            prompt_tokens_details={"cached_tokens": cached},
        )
    )


class ModelBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = profile("primary")
        self.provider = SimpleNamespace(
            create_completion=AsyncMock(return_value=completion()), close=AsyncMock()
        )
        self.gateway = LLMGateway({"openai-chat": self.provider})

    async def test_lease_guard_runs_before_request_reservation_or_provider_call(self):
        from src.plugins.ai_chat.agent.control import LeaseLost

        guard = AsyncMock(side_effect=LeaseLost("cancelled"))
        budget = ModelCallBudget(10000, before_request=guard)
        with model_budget_scope(budget), self.assertRaises(LeaseLost):
            await self.gateway.create_completion(self.profile, messages=[])
        guard.assert_awaited_once()
        self.provider.create_completion.assert_not_awaited()
        self.assertEqual(budget.summary()["requests"], 0)

    async def test_caps_output_and_accounts_for_each_model_round(self):
        budget = ModelCallBudget(2000)
        with model_budget_scope(budget):
            for _ in range(3):
                await self.gateway.create_completion(
                    self.profile, messages=[], max_tokens=90000
                )
        self.assertEqual(budget.summary()["requests"], 3)
        self.assertEqual(budget.summary()["charged_tokens"], 120)
        for call in self.provider.create_completion.await_args_list:
            self.assertLess(call.kwargs["max_tokens"], 2000)
        self.assertTrue(budget.summary()["usage_known"])

    async def test_large_input_stops_before_any_network_call(self):
        budget = ModelCallBudget(1000)
        with model_budget_scope(budget), self.assertRaises(ModelBudgetExceeded):
            await self.gateway.create_completion(
                self.profile, messages=[{"role": "user", "content": "x" * 2000}]
            )
        self.provider.create_completion.assert_not_awaited()

    async def test_request_count_limit_prevents_low_token_loops(self):
        budget = ModelCallBudget(100000, max_requests=2)
        with model_budget_scope(budget):
            for _ in range(2):
                await self.gateway.create_completion(self.profile, messages=[])
            with self.assertRaises(ModelBudgetExceeded):
                await self.gateway.create_completion(self.profile, messages=[])
        self.assertEqual(self.provider.create_completion.await_count, 2)

    async def test_fallback_is_another_charged_attempt(self):
        primary = replace(
            self.profile, fallback_profiles=("backup",), max_output_tokens=100
        )
        backup = replace(profile("backup"), max_output_tokens=100)
        catalog = ModelCatalog(
            {item.name: item for item in (primary, backup)},
            default_profile=primary.name,
        )
        self.gateway = LLMGateway({"openai-chat": self.provider}, catalog=catalog)
        self.provider.create_completion.side_effect = [
            LLMConnectionError("offline"),
            completion(),
        ]
        budget = ModelCallBudget(3000)
        with model_budget_scope(budget):
            result = await self.gateway.create_completion_with_profile(
                primary, messages=[]
            )
        self.assertEqual(result.profile.name, "backup")
        self.assertEqual(budget.summary()["requests"], 2)
        self.assertGreater(budget.charged_tokens, 600)
        self.assertFalse(budget.summary()["usage_known"])

    async def test_compatibility_retry_also_consumes_request_budget(self):
        self.provider.create_completion.side_effect = LLMProviderError(
            "test", 400, "tool_choice_not_supported"
        )
        budget = ModelCallBudget(10000, max_requests=1)
        with model_budget_scope(budget), self.assertRaises(ModelBudgetExceeded):
            await self.gateway.create_completion(
                self.profile,
                messages=[],
                tools=[{"type": "function"}],
                tool_choice={"type": "function", "function": {"name": "reply"}},
            )
        self.assertEqual(self.provider.create_completion.await_count, 1)

    async def test_parallel_calls_reserve_before_awaiting_provider(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def provider(*args, **kwargs):
            started.set()
            await release.wait()
            return completion()

        self.provider.create_completion.side_effect = provider
        budget = ModelCallBudget(1000)
        with model_budget_scope(budget):
            first = asyncio.create_task(
                self.gateway.create_completion(self.profile, messages=[])
            )
            try:
                await asyncio.wait_for(started.wait(), 1)
                with self.assertRaises(ModelBudgetExceeded):
                    await self.gateway.create_completion(self.profile, messages=[])
            finally:
                release.set()
                await first
        self.assertEqual(self.provider.create_completion.await_count, 1)

    async def test_stream_uses_final_usage_and_early_close_does_not_refund(self):
        budget = ModelCallBudget(5000)
        stream = FakeStream(
            stream_chunk("hello"), stream_chunk(finish="stop"), completion(50, 10, 20)
        )
        self.provider.create_completion.return_value = stream
        with model_budget_scope(budget):
            response = await self.gateway.create_completion(
                self.profile, messages=[], stream=True
            )
            async for _ in response:
                pass
            await response.close()
        self.assertEqual(budget.charged_tokens, 60)
        self.assertEqual(budget.summary()["cached_tokens"], 20)
        self.assertTrue(
            self.provider.create_completion.call_args.kwargs["stream_options"][
                "include_usage"
            ]
        )
        partial = ModelCallBudget(5000)
        self.provider.create_completion.return_value = FakeStream(
            stream_chunk("partial"), completion()
        )
        with model_budget_scope(partial):
            response = await self.gateway.create_completion(
                self.profile, messages=[], stream=True
            )
            await response.close()
        self.assertGreater(partial.charged_tokens, 4000)
        self.assertFalse(partial.summary()["usage_known"])

    async def test_cancellation_missing_usage_and_overrun_are_not_silently_free(self):
        for response in (SimpleNamespace(), completion(2000, 2000)):
            budget = ModelCallBudget(1000)
            self.provider.create_completion.return_value = response
            with model_budget_scope(budget):
                await self.gateway.create_completion(self.profile, messages=[])
                with self.assertRaises(ModelBudgetExceeded):
                    await self.gateway.create_completion(self.profile, messages=[])
            self.assertGreaterEqual(budget.charged_tokens, 1000)
        self.provider.create_completion.side_effect = asyncio.CancelledError()
        budget = ModelCallBudget(1000)
        with model_budget_scope(budget), self.assertRaises(asyncio.CancelledError):
            await self.gateway.create_completion(self.profile, messages=[])
        self.assertEqual(budget.charged_tokens, 1000)

    def test_explicit_completion_limit_and_anthropic_translation(self):
        budget = ModelCallBudget(1000)
        request, _ = budget.prepare(
            self.profile,
            {"messages": [], "max_tokens": 100, "max_completion_tokens": 50},
        )
        self.assertNotIn("max_tokens", request)
        self.assertEqual(request["max_completion_tokens"], 50)
        anthropic = profile("claude", protocol="anthropic-messages")
        self.assertEqual(
            _anthropic_request(anthropic, {"messages": [], "max_tokens": 35})[
                "max_tokens"
            ],
            35,
        )

    def test_nested_scopes_cannot_replace_budget_and_context_is_restored(self):
        budget = ModelCallBudget(1000)
        self.assertIsNone(active_model_budget())
        with model_budget_scope(budget):
            with model_budget_scope(budget):
                self.assertIs(active_model_budget(), budget)
            with (
                self.assertRaises(ValueError),
                model_budget_scope(ModelCallBudget(100000)),
            ):
                pass
        self.assertIsNone(active_model_budget())

    def test_media_needs_a_window_reservation_and_extra_body_cannot_bypass_limits(self):
        budget = ModelCallBudget(5000)
        request = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/a.png"},
                        }
                    ],
                }
            ]
        }
        with self.assertRaises(ModelBudgetExceeded):
            budget.prepare(self.profile, request)
        prepared, reservation = budget.prepare(
            replace(self.profile, max_input_tokens=2000), request
        )
        self.assertEqual(prepared["max_tokens"], 3000)
        self.assertEqual(reservation.reserved, 5000)
        with self.assertRaises(ModelBudgetExceeded):
            ModelCallBudget(1000).prepare(
                self.profile, {"messages": [], "extra_body": {"max_tokens": 100000}}
            )

    async def test_openai_sdk_retries_disabled_only_for_budgeted_calls(self):
        provider = OpenAIChatProvider()
        client = Mock()
        client.chat.completions.create = AsyncMock(return_value=completion())
        client.with_options.return_value = client
        provider._client = Mock(return_value=client)
        await provider.create_completion(self.profile, messages=[])
        client.with_options.assert_not_called()
        with model_budget_scope(ModelCallBudget(1000)):
            await provider.create_completion(self.profile, messages=[])
        client.with_options.assert_called_once_with(max_retries=0)


if __name__ == "__main__":
    unittest.main()
