from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI


from src.plugins.ai_chat.admin import AdminServices
from tests.admin_session_fixture import ApprovedClient, register_admin
from src.plugins.ai_chat.agent_loop import AgentTrace, _invoke_completion
from src.plugins.ai_chat.llm_gateway import (
    LLMConnectionError, LLMGateway, LLMRateLimitError, LLMUnavailableError,
)
from src.plugins.ai_chat.local_model import LocalModelRuntime
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile


QWEN = ModelProfile(name="qwen-local", provider="qwen", protocol="openai-chat", model="qwen3.8-27b", base_url="http://qwen.test/v1", api_key="", api_key_required=False, fallback_profiles=("deepseek",))
DEEPSEEK = replace(QWEN, name="deepseek", provider="deepseek", model="deepseek-test", fallback_profiles=())


class LocalModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.model_status = 200
        self.models = []
        self.requests = []
        self.control_requests = []

        async def respond(request):
            self.requests.append(str(request.url))
            return httpx.Response(self.model_status, json={"data": self.models})

        self.local = LocalModelRuntime(QWEN, client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        self.addAsyncCleanup(self.local.close)
        self.catalog = ModelCatalog({QWEN.name: QWEN, DEEPSEEK.name: DEEPSEEK}, default_profile="deepseek")
        self.provider = Mock()

        async def completion(profile, **kwargs):
            self.control_requests.append((profile.name, kwargs))
            return "ok"

        self.provider.create_completion = completion
        self.gateway = LLMGateway({"openai-chat": self.provider}, catalog=self.catalog, local_model=self.local, failure_threshold=1)

    async def test_offline_skips_request_records_reason_and_recovers(self):
        await self.local.probe_once()
        calls = len(self.requests)
        result = await self.gateway.create_completion_with_profile(QWEN, messages=[])
        self.assertEqual(result.profile.name, "deepseek")
        self.assertEqual(result.routing["reason_code"], "model_not_loaded")
        self.assertEqual(result.routing["requested_profile"], "qwen-local")
        self.assertEqual(result.routing["actual_profile"], "deepseek")
        self.assertTrue(result.routing["fallback"])
        self.assertEqual(len(self.requests), calls, "chat must not perform health I/O")
        self.assertEqual([item[0] for item in self.control_requests], ["deepseek"])
        self.models = [{"id": QWEN.model}]
        await self.local.probe_once()
        result = await self.gateway.create_completion_with_profile(QWEN, messages=[])
        self.assertEqual(result.profile.name, "qwen-local")
        self.assertFalse(result.routing["fallback"])
        metrics = self.gateway.health_snapshot()[QWEN.name]
        self.assertEqual(metrics["request_count"], 1)
        self.assertIsNotNone(metrics["average_latency_ms"])

    async def test_initial_stale_missing_model_and_stopping_are_not_ready(self):
        self.assertEqual(self.local.unavailable_reason(QWEN.name)[0], "health_pending")
        self.assertIsNone(self.local.unavailable_reason(DEEPSEEK.name))
        self.models = [{"id": "different-model"}]
        await self.local.probe_once()
        self.assertEqual(self.local.snapshot()["state"], "loading")
        self.models = [{"id": QWEN.model}]
        await self.local.probe_once()
        self.local._checked_monotonic = time.monotonic() - 100
        self.assertEqual(self.local.unavailable_reason(QWEN.name)[0], "health_stale")
        self.models = []
        await self.local.probe_once()
        self.assertFalse(self.local.snapshot()["ready"])

    async def test_network_status_does_not_claim_service_is_stopped(self):
        local = LocalModelRuntime(QWEN, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(503))))
        self.addAsyncCleanup(local.close)
        await local.probe_once()
        self.assertEqual(local.snapshot()["state"], "unavailable")

    async def test_background_worker_probes_without_console_subscribers(self):
        task = asyncio.create_task(self.local.run_forever())
        try:
            for _ in range(100):
                if self.local.snapshot()["checked_at"]:
                    break
                await asyncio.sleep(0.001)
            self.assertIsNotNone(self.local.snapshot()["checked_at"])
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_ready_cache_does_not_clear_inference_circuit_on_every_request(self):
        self.models = [{"id": QWEN.model}]
        await self.local.probe_once()

        async def fail_qwen(profile, **kwargs):
            self.control_requests.append((profile.name, kwargs))
            if profile.name == QWEN.name:
                raise LLMConnectionError("offline")
            return "ok"

        self.provider.create_completion = fail_qwen
        await self.gateway.create_completion_with_profile(QWEN, messages=[])
        await self.local.probe_once()
        result = await self.gateway.create_completion_with_profile(QWEN, messages=[])
        self.assertEqual(result.routing["reason_code"], "circuit_open")
        self.assertEqual([p for p, _ in self.control_requests].count(QWEN.name), 1)
        self.models = []
        await self.local.probe_once()
        self.models = [{"id": QWEN.model}]
        await self.local.probe_once()
        await self.gateway.create_completion_with_profile(QWEN, messages=[])
        self.assertEqual([p for p, _ in self.control_requests].count(QWEN.name), 2)

    async def test_disabled_qwen_circuit_retries_without_clearing_cloud_circuit(self):
        qwen = replace(QWEN, circuit_breaker_enabled=False)
        self.local.profile = qwen
        self.models = [{"id": qwen.model}]
        await self.local.probe_once()
        catalog = ModelCatalog(
            {qwen.name: qwen, DEEPSEEK.name: DEEPSEEK},
            default_profile=qwen.name,
        )
        gateway = LLMGateway(
            {"openai-chat": self.provider}, catalog=catalog,
            local_model=self.local, failure_threshold=1,
        )
        failing = True

        async def completion(profile, **kwargs):
            self.control_requests.append((profile.name, kwargs))
            if failing:
                raise LLMRateLimitError("busy")
            return "ok"

        self.provider.create_completion = completion
        for _ in range(2):
            with self.assertRaises(LLMRateLimitError):
                await gateway.create_completion_with_profile(qwen, messages=[])
        self.assertEqual(
            [name for name, _ in self.control_requests],
            [qwen.name, DEEPSEEK.name, qwen.name],
        )
        health = gateway.health_snapshot()
        self.assertFalse(health[qwen.name]["circuit_breaker_enabled"])
        self.assertEqual(health[qwen.name]["retry_after_seconds"], 0)
        self.assertEqual(health[qwen.name]["total_failures"], 2)
        self.assertEqual(health[qwen.name]["status"], "degraded")
        self.assertEqual(health[DEEPSEEK.name]["status"], "open")
        for _, kwargs in self.control_requests:
            self.assertNotIn("circuit_breaker_enabled", kwargs)

        failing = False
        result = await gateway.create_completion_with_profile(qwen, messages=[])
        self.assertEqual(result.profile.name, qwen.name)
        self.assertFalse(result.routing["fallback"])
        self.assertEqual(gateway.health_snapshot()[qwen.name]["status"], "healthy")
        self.assertEqual(gateway.health_snapshot()[DEEPSEEK.name]["status"], "open")

        app = FastAPI()
        register_admin(
            app,
            AdminServices(
                version="test",
                started_at=1,
                local_model=self.local,
                llm_gateway=gateway,
            ),
            test_case=self,
            token="",
        )
        async with ApprovedClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            status = (await client.get("/bot-admin/api/v1/local-model", headers={"X-Test-Login": "admin"})).json()
        self.assertFalse(status["circuit_breaker_enabled"])

        self.models = []
        await self.local.probe_once()
        calls_before = len(self.control_requests)
        with self.assertRaises(LLMUnavailableError):
            await gateway.create_completion_with_profile(qwen, messages=[])
        self.assertEqual(len(self.control_requests), calls_before)

    async def test_trace_records_health_fallback_and_no_private_request_options(self):
        module = __import__("src.plugins.ai_chat.agent_loop", fromlist=["_"])
        with patch.object(module, "_model_catalog", self.catalog), patch.object(module, "_llm_gateway", self.gateway):
            trace = AgentTrace()
            await _invoke_completion(QWEN, trace=trace, messages=[])
        decision = trace.to_payload()["model_routing"][0]
        self.assertEqual(decision["actual_profile"], "deepseek")
        self.assertEqual(decision["reason_code"], "health_pending")
        self.assertNotIn("route_sink", self.control_requests[0][1])
        self.assertNotIn("_trace", self.control_requests[0][1])

    async def test_failed_fallback_still_records_route(self):
        decisions = []

        async def fail(_profile, **_kwargs):
            raise LLMConnectionError("offline")

        self.provider.create_completion = fail
        with self.assertRaises(LLMConnectionError):
            await self.gateway.create_completion_with_profile(QWEN, route_sink=decisions.append, messages=[])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["actual_profile"], "")
        self.assertEqual([item["status"] for item in decisions[0]["outcomes"]], ["skipped", "failed"])
