from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from tests.test_llm_gateway import profile
from src.plugins.ai_chat.execution_control import LeaseLost
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.model_budget import (
    ModelBudgetExceeded, ModelCallBudget, active_model_budget, model_budget_scope,
)
from src.plugins.ai_chat.model_catalog import ModelCapabilities, ModelCatalog
from src.plugins.ai_chat.vision_worker import VisionJob, VisionJobError, VisionWorker


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
        self.worker = VisionWorker(Mock(),
            model_catalog=ModelCatalog({selected.name: selected}, default_profile=selected.name),
            llm_gateway=LLMGateway({"openai-chat": self.provider}), vision_profile=selected.name)
        self.worker._download = AsyncMock(return_value=(b"image", "image/png"))
        self.worker._prepare_for_vision = Mock(side_effect=lambda content, mime: (content, mime))
        self.worker.submit = Mock(side_effect=AssertionError("Budgeted work must not escape to a queue"))
        self.guard = AsyncMock()
        self.budget = ModelCallBudget(5000, before_request=self.guard)
        self.arguments = dict(scope_key="onebot-v11:group:123", native_message_id="99", segment_index=0,
                              requester_native_user_id="456", source_url="https://gchat.qpic.cn/test")

    async def test_gateway_receipt_is_charged_to_parent_without_queueing(self):
        with model_budget_scope(self.budget):
            result = await self.worker.submit_and_wait(**self.arguments)
        self.assertEqual(result.summary, "test image")
        self.assertEqual(self.budget.summary()["charged_tokens"], 40)
        self.assertEqual(self.budget.summary()["requests"], 1)
        self.worker.submit.assert_not_called()
        self.worker.database.store_connection.assert_not_called()
        self.assertFalse(self.worker._analysis_inflight)
        self.assertIsNone(active_model_budget())

    async def test_insufficient_budget_never_reaches_provider(self):
        with model_budget_scope(ModelCallBudget(1000)), self.assertRaises(ModelBudgetExceeded):
            await self.worker.submit_and_wait(**self.arguments)
        self.provider.create_completion.assert_not_awaited()
        self.worker.submit.assert_not_called()

    async def test_cancelled_owner_stops_before_download_even_if_result_cached(self):
        with model_budget_scope(self.budget):
            await self.worker.submit_and_wait(**self.arguments)
        self.worker._download.reset_mock()
        self.guard.side_effect = LeaseLost("cancelled")
        with model_budget_scope(self.budget), self.assertRaises(LeaseLost):
            await self.worker.submit_and_wait(**self.arguments)
        self.worker._download.assert_not_awaited()
        self.assertEqual(self.provider.create_completion.await_count, 1)

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
            task = asyncio.create_task(self.worker.submit_and_wait(**self.arguments))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.budget.summary()["usage_known"])
        self.assertEqual(self.budget.charged_tokens, 2100)
        self.assertFalse(self.worker._analysis_inflight)

    async def test_parent_budget_does_not_join_an_unrelated_inflight_call(self):
        job = VisionJob(3, "99", 0, self.arguments["source_url"], "summary", "", 1)
        key = self.worker._analysis_cache_key(job, b"image")
        unrelated = asyncio.create_task(asyncio.Event().wait())
        self.worker._analysis_inflight[key] = unrelated
        try:
            with model_budget_scope(self.budget):
                result = await self.worker.submit_and_wait(**self.arguments)
            self.assertEqual(result.summary, "test image")
            self.assertEqual(self.budget.summary()["requests"], 1)
            self.assertFalse(unrelated.done())
        finally:
            unrelated.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await unrelated

    async def test_completed_cache_can_be_reused_without_another_model_call(self):
        with model_budget_scope(self.budget):
            first = await self.worker.submit_and_wait(**self.arguments)
            second = await self.worker.submit_and_wait(**self.arguments)
        self.assertEqual(first, second)
        self.assertEqual(self.budget.summary()["requests"], 1)
        self.assertEqual(self.worker._cache_hits, 1)

    async def test_scoped_refresh_does_not_update_an_unrelated_queue_record(self):
        self.worker._download.side_effect = [VisionJobError("expired"), (b"image", "image/png")]
        self.worker._source_resolver = AsyncMock(return_value="https://gchat.qpic.cn/fresh")
        self.worker._update_source_url = Mock(side_effect=AssertionError("No persisted child job"))
        with model_budget_scope(self.budget):
            await self.worker.submit_and_wait(**self.arguments)
        self.worker._source_resolver.assert_awaited_once_with("99", 0)
        self.worker._update_source_url.assert_not_called()

    async def test_invalid_mode_and_private_source_still_fail_closed(self):
        for changes in ({"mode": "invented"}, {"source_url": "http://127.0.0.1/image"}):
            with self.subTest(changes=changes), model_budget_scope(self.budget), self.assertRaises(VisionJobError):
                await self.worker.submit_and_wait(**{**self.arguments, **changes})
        self.worker._download.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
