from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI


from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.observability import (
    BotTelemetry,
    current_trace_id,
    register_metrics_endpoint,
    telemetry,
)


class EmptyTasks:
    def list_all(self) -> list[object]:
        return []


class EmptyDeliveries:
    def stats(self) -> dict[str, int]:
        return {"pending": 2, "ambiguous": 1}


class ObservabilityTests(unittest.TestCase):
    def test_admin_snapshot_groups_process_metrics(self) -> None:
        metrics = BotTelemetry()
        metrics.observe_model(
            requested_profile="main",
            actual_profile="fallback",
            provider="test-provider",
            status="succeeded",
            duration=1.2,
        )
        metrics.observe_tokens("fallback", 120, 30)
        metrics.turns.labels(
            platform="onebot-v11",
            kind="group",
            status="succeeded",
        ).inc()
        metrics.turn_duration.labels(
            platform="onebot-v11",
            kind="group",
            status="succeeded",
        ).observe(2.0)

        async def call_tool() -> None:
            async with metrics.tool("web_search"):
                return None

        import asyncio

        asyncio.run(call_tool())
        with metrics.delivery("onebot-v11"):
            pass
        snapshot = metrics.admin_snapshot(
            running_tasks=EmptyTasks(),
            delivery_store=EmptyDeliveries(),
        )

        self.assertEqual(snapshot["totals"]["turns"], 1)
        self.assertEqual(snapshot["totals"]["model_requests"], 1)
        self.assertEqual(snapshot["totals"]["fallbacks"], 1)
        self.assertEqual(snapshot["totals"]["input_tokens"], 120)
        self.assertEqual(snapshot["totals"]["output_tokens"], 30)
        self.assertEqual(snapshot["tools"][0]["tool"], "web_search")
        self.assertEqual(snapshot["outbox"]["pending"], 2)

    def test_trace_id_is_attached_to_archived_model_trace(self) -> None:
        telemetry.configure("gaoji-test", service_version="test")
        with telemetry.span("test.turn") as trace_id:
            self.assertEqual(current_trace_id(), trace_id)
            payload = AgentTrace(profile="test").to_payload()
        self.assertEqual(payload["trace_id"], trace_id)
        self.assertEqual(len(trace_id), 32)

    def test_prometheus_endpoint_exports_runtime_gauges(self) -> None:
        app = FastAPI()
        register_metrics_endpoint(
            app,
            path="/metrics",
            service_name="gaoji-test",
            service_version="test",
            running_tasks=EmptyTasks(),
            delivery_store=EmptyDeliveries(),
        )

        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                return await client.get("/metrics")

        import asyncio

        response = asyncio.run(run())
        self.assertEqual(response.status_code, 200)
        self.assertIn("gaoji_runtime_tasks 0.0", response.text)
        self.assertIn(
            'gaoji_outbox_deliveries{status="pending"} 2.0',
            response.text,
        )


class EventLoopTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sampler_reports_delay_and_can_be_cancelled(self):
        metrics = BotTelemetry()
        sampled = asyncio.Event()
        original = metrics.event_loop_lag_distribution.observe

        def observe(lag):
            original(lag)
            sampled.set()

        with patch.object(metrics.event_loop_lag_distribution, "observe", observe):
            sampler = asyncio.create_task(metrics.monitor_event_loop(0.005))
            try:
                await asyncio.sleep(0)
                time.sleep(0.03)
                await asyncio.wait_for(sampled.wait(), 1)
                snapshot = metrics.admin_snapshot()
                self.assertGreater(snapshot["totals"]["event_loop_lag_seconds"], 0.015)
                rendered = metrics.render().decode()
                self.assertIn("gaoji_event_loop_lag_seconds", rendered)
                self.assertIn(
                    "gaoji_event_loop_lag_observed_seconds_count 1.0", rendered
                )
            finally:
                sampler.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await sampler

    async def test_sampler_rejects_busy_loop_interval(self):
        with self.assertRaises(ValueError):
            await BotTelemetry().monitor_event_loop(0)


if __name__ == "__main__":
    unittest.main()
