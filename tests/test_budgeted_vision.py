from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests.test_llm_gateway import profile
from src.plugins.ai_chat.execution_control import LeaseLost
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.model_budget import (
    ModelBudgetExceeded, ModelCallBudget, active_model_budget, model_budget_scope,
)
from src.plugins.ai_chat.model_catalog import ModelCapabilities, ModelCatalog
from src.plugins.ai_chat.vision_worker import VisionWorker


class BudgetedVisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        selected = replace(profile("vision", capabilities=ModelCapabilities(vision=True)),
                           max_input_tokens=2000, max_output_tokens=100)
        self.response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=
                '{"summary":"test image","description":"test","text":"",'
                '"observations":[],"safety":"safe"}'))],
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=10),
        )
        self.provider = SimpleNamespace(create_completion=AsyncMock(return_value=self.response), close=AsyncMock())
        self.worker = VisionWorker(
            model_catalog=ModelCatalog({selected.name: selected}, default_profile=selected.name),
            llm_gateway=LLMGateway({"openai-chat": self.provider}), vision_profile=selected.name)
        self.guard = AsyncMock()
        self.budget = ModelCallBudget(5000, before_request=self.guard)

    def recognize(self):
        return self.worker._request_analysis(b'TEST_IMAGE', 'image/png', mode='detail', question='')

    async def test_gateway_receipt_is_charged_to_parent(self):
        with model_budget_scope(self.budget):
            result = await self.recognize()
        self.assertEqual(result.summary, "test image")
        self.assertEqual(self.budget.summary()["charged_tokens"], 40)
        self.assertEqual(self.budget.summary()["requests"], 1)
        self.assertIsNone(active_model_budget())

    async def test_insufficient_budget_never_reaches_provider(self):
        with model_budget_scope(ModelCallBudget(1000)), self.assertRaises(ModelBudgetExceeded):
            await self.recognize()
        self.provider.create_completion.assert_not_awaited()

    async def test_cancelled_owner_stops_before_model_request(self):
        self.guard.side_effect = LeaseLost("cancelled")
        with model_budget_scope(self.budget), self.assertRaises(LeaseLost):
            await self.recognize()
        self.provider.create_completion.assert_not_awaited()

    async def test_cancellation_stops_provider_before_budget_settlement(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def provider(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.provider.create_completion.side_effect = provider
        with model_budget_scope(self.budget):
            task = asyncio.create_task(self.recognize())
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.budget.summary()["usage_known"])
        self.assertEqual(self.budget.charged_tokens, 2100)

if __name__ == "__main__":
    unittest.main()
