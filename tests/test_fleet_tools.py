from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.ai_tools import CLUSTER_JOB_SUBMIT_TOOL, HOST_INSPECT_TOOL, SERVICE_INSPECT_TOOL
from src.plugins.ai_chat.fleet_client import FleetControlError
from src.plugins.ai_chat.fleet_tools import (
    inspect_host,
    fleet_overview,
    model_status,
    requires_local_model_status,
    summarize_fleet,
    summarize_resources,
)
from src.plugins.ai_chat.tool_policy import ToolCatalog


def fleet_payload(now: int) -> dict:
    hosts = []
    for name in ("h310", "h610", "r5s", "r5sjp", "r6s", "rpi4", "shanghai", "tank"):

        def metric(value: int) -> dict:
            return {
                "samples": [
                    {
                        "labels": {"mountpoint": mount},
                        "value": value,
                        "state": "available",
                    }
                    for mount in ("/", *(f"/run/{i}" for i in range(70)))
                ]
            }

        hosts.append(
            {
                "host": name,
                "agent": {"state": "reachable", "failed_units": 0},
                "exporter": {"state": "up", "sample_at_unix_seconds": now - 5},
                "pressure": {
                    "filesystem_size_bytes": metric(500 * 1024**3),
                    "filesystem_available_bytes": metric(76 * 1024**3),
                },
            }
        )
    return {
        "status": "fresh",
        "observed_at": now,
        "expires_at": now + 20,
        "data": {"hosts": hosts},
        "active_alerts": {"status": "fresh", "data": {"alerts": []}},
    }


class FleetProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_ssh_evidence_reaches_conversation_without_fake_exporter(self):
        from src import ssh_operations as target
        from src.cluster_control.adapters.ssh import SSHOperationsClient
        from src.cluster_control.service import FleetControlService
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="gaoji-projection-test-") as root:
            pins = Path(root) / "known_hosts"
            pins.write_text("pinned test identity")
            client = SSHOperationsClient({"h610": {"destination": "gaoji-operator@test.internal", "helper": "/test/helper"}},
                known_hosts_file=str(pins))
            service = FleetControlService(client, inventory=({"host_id": "h610", "observe": True,
                "readable_units": ["example.service"]},))
            async def observe(host, request):
                with patch.object(target, "command", return_value="example.service loaded failed failed Example\nprivate.service loaded failed failed Private\n"):
                    return target.observation({"host_id": host, "mounts": ["/"]}, request["op"], request["params"])
            with patch.object(client, "_remote", side_effect=observe):
                payload = await service.fleet_overview()
            timestamp = int(time.time())
            result = summarize_fleet(payload, now=timestamp)["hosts"][0]
            self.assertEqual(result["status"], "online")
            self.assertEqual(result["failed_service_count"], 1)
            self.assertEqual(result["failed_services"][0]["unit"], "example.service")
            self.assertNotIn("private.service", json.dumps(payload))
            self.assertEqual(result["resource_source"], "ssh")
            self.assertIsNotNone(result["root_disk"])
            self.assertEqual(result["exporter_state"], "unknown")
            self.assertIsNone(result["active_alert_count"])
            payload["data"]["hosts"][0]["resource_observation"]["sample_at_unix_seconds"] = timestamp - 120
            self.assertIsNone(summarize_fleet(payload, now=timestamp)["hosts"][0]["root_disk"])

    def test_native_cpu_window_is_not_misreported_as_five_minutes(self):
        def metric(value, labels=None):
            return {"samples": [{"state": "available", "value": value, "labels": labels or {}, "sample_at_unix_seconds": 1000}]}
        metrics = {"memory_total_bytes": metric(1024), "memory_available_bytes": metric(256),
            "cpu_idle_seconds_per_second": {**metric(0.25, {"cpu": "0"}), "window_seconds": 0.2}}
        payload = {"status": "fresh", "data": {"host": "h610", "observation": {"source": "ssh", "metrics": metrics}}}
        result = summarize_resources(payload, "h610", now=1000)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["cpu_busy_percent"], 75)
        self.assertEqual(result["cpu_window_seconds"], 0.2)

    def test_resource_metrics_require_fresh_unambiguous_host_samples(self):
        def metric(value, labels=None, at=1000):
            return {"samples": [{"labels": labels or {}, "state": "available", "value": value, "sample_at_unix_seconds": at}]}
        metrics = {"memory_total_bytes": metric(1024), "memory_available_bytes": metric(256),
            "cpu_idle_seconds_per_second": metric(0.25, {"cpu": "0"}), "load1": metric(0.8)}
        payload = {"status": "fresh", "data": {"host": "h610", "observation": {"metrics": metrics}}}
        result = summarize_resources(payload, "h610", now=1010)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["cpu_busy_percent"], 75)
        self.assertEqual(result["memory_available_bytes"], 256)
        self.assertEqual(summarize_resources(payload, "tank", now=1010)["status"], "unavailable")
        self.assertEqual(summarize_resources(payload, "h610", now=1200)["status"], "partial")
        metrics["cpu_idle_seconds_per_second"]["samples"] *= 2
        self.assertNotIn("cpu_busy_percent", summarize_resources(payload, "h610", now=1010))

    def test_typed_host_actions_never_generate_shell_commands(self) -> None:
        from src.plugins.ai_chat.ai_tools import HOST_REBOOT_TOOL, SERVICE_CONTROL_TOOL, host_operation_call
        from src.plugins.ai_chat.tool_policy import policy_for_tool
        catalog = ToolCatalog([HOST_REBOOT_TOOL, SERVICE_CONTROL_TOOL])
        for action in ("start", "stop", "restart", "reload"):
            args = {"host_id": "h310", "unit": "example.service", "action": action, "idempotency_key": "service-test"}
            self.assertTrue(catalog.validate("service_control", args).ok)
            call = host_operation_call("service_control", args)
            self.assertEqual(call["operation"], "units." + action)
            self.assertEqual(call["params"], {"host": "h310", "unit": "example.service"})
            self.assertNotIn("command", json.dumps(call))
        args = {"host_id": "tank", "reason": "Maintenance", "idempotency_key": "host-reboot-test"}
        self.assertTrue(catalog.validate("host_reboot", args).ok)
        self.assertEqual(host_operation_call("host_reboot", args)["operation"], "host.reboot")
        self.assertFalse(catalog.validate("host_reboot", {**args, "command": "reboot"}).ok)
        for name in ("service_control", "host_reboot"):
            self.assertEqual(policy_for_tool(name).approval, "task")

    def test_fractional_samples_in_capture_second_are_not_future_or_stale(self) -> None:
        for sample_at, accepted in ((1000.193, True), (1000.999, True), (910, True),
                                    (909.999, False), (1001, False), (1005, False),
                                    (float("nan"), False), (float("inf"), False), (True, False)):
            with self.subTest(sample_at=sample_at):
                payload = fleet_payload(1000)
                payload["data"]["hosts"][-1]["exporter"]["sample_at_unix_seconds"] = sample_at
                host = summarize_fleet(payload, host_id="tank", now=1000)["hosts"][0]
                self.assertEqual(host["root_disk"] is not None, accepted)
                metrics = {name: {"samples": [{"state": "available", "value": value,
                    "sample_at_unix_seconds": sample_at, "labels": labels}]}
                    for name, value, labels in (("memory_total_bytes", 1024, {}),
                        ("memory_available_bytes", 256, {}),
                        ("cpu_idle_seconds_per_second", 0.75, {"cpu": "0"}))}
                resources = summarize_resources({"status": "fresh", "data": {
                    "host": "tank", "observation": {"metrics": metrics}}}, "tank", now=1000)
                self.assertEqual(resources["status"] == "available", accepted)
                if accepted:
                    self.assertEqual(resources["cpu_observed_at"], sample_at)
                    self.assertEqual(resources["memory_observed_at"], sample_at)

    def test_job_tool_accepts_exact_worker_selection(self) -> None:
        catalog = ToolCatalog([CLUSTER_JOB_SUBMIT_TOOL])
        args = {"kind": "probe.http", "target_id": "h610-worker", "idempotency_key": "worker-selection"}
        self.assertTrue(catalog.validate("cluster_job_submit", args).ok)
        for host in ("h310", "h610", "tank"):
            self.assertTrue(catalog.validate("cluster_job_submit", {**args, "worker_id": host + "-worker"}).ok)
        for invalid in ("", "../tank", "tank;reboot", "*", "x" * 65):
            self.assertFalse(catalog.validate("cluster_job_submit", {**args, "worker_id": invalid}).ok)

    async def test_worker_overview_distinguishes_stale_and_draining(self) -> None:
        workers = [{"worker_id": host + "-worker", "host_id": host, "last_seen_at": seen,
                    "availability": "available", "capabilities": ["probe.http"],
                    "capacity": {"cpu_millis": 2000, "secret": "not-for-model"}}
                   for host, seen in (("h310", 995), ("h610", 900), ("tank", 995))]
        policies = [{"worker_id": host + "-worker", "desired_availability": state}
                    for host, state in (("h310", "available"), ("h610", "available"), ("tank", "draining"))]
        client = SimpleNamespace(fleet=AsyncMock(return_value=fleet_payload(1000)),
            workers=AsyncMock(return_value={"items": workers}),
            resource_policies=AsyncMock(return_value={"items": policies}))
        result = await fleet_overview(client, now=1000)
        items = result["workers"]["items"]
        self.assertEqual([x["ready_for_scheduling"] for x in items], [True, False, False])
        self.assertEqual([x["host_id"] for x in items], ["h310", "h610", "tank"])
        self.assertNotIn("not-for-model", json.dumps(result))
        client.resource_policies.side_effect = FleetControlError("unavailable", "not available")
        result = await fleet_overview(client, now=1000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["workers"]["status"], "partial")
        self.assertTrue(all(not x["ready_for_scheduling"] for x in result["workers"]["items"]))
        client.workers.side_effect = FleetControlError("timeout", "not available")
        result = await fleet_overview(client, now=1000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["workers"]["status"], "unavailable")
        self.assertEqual(result["workers"]["items"], [])

    def test_qwen_status_follow_up_requires_a_fresh_read(self) -> None:
        self.assertTrue(requires_local_model_status("现在千问呢"))
        self.assertTrue(requires_local_model_status("看下千问寄了吗"))
        self.assertFalse(requires_local_model_status("千问模型是谁研发的"))

    def test_all_eight_hosts_survive_tool_budget(self) -> None:
        payload = fleet_payload(1000)
        self.assertGreater(len(json.dumps(payload)), 12000)
        summary = summarize_fleet(payload, now=1000)
        self.assertLess(len(json.dumps(summary, ensure_ascii=False)), 8000)
        tank = summary["hosts"][-1]
        self.assertEqual(tank["host_id"], "tank")
        self.assertEqual(tank["status"], "online")
        self.assertEqual(tank["root_disk"]["available_gib"], 76)
        self.assertEqual(tank["failed_service_count"], 0)

    def test_stale_data_cannot_be_reported_as_online(self) -> None:
        summary = summarize_fleet(fleet_payload(1000), now=1100)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["hosts"][-1]["status"], "stale")
        self.assertIsNone(summary["hosts"][-1]["root_disk"])
        self.assertIsNone(summary["hosts"][-1]["failed_service_count"])

    def test_receipt_time_does_not_revalidate_expired_or_aged_samples(self) -> None:
        payload = fleet_payload(1000)
        for captured, assembled, disk_available in ((1005, 1025, True),
                (1021, 1025, False), (1005, 1096, False), (1030, 1025, False)):
            with self.subTest(captured=captured, assembled=assembled):
                host = summarize_fleet(payload, host_id="tank", now=assembled,
                    acquired_at=captured)["hosts"][0]
                self.assertEqual(host["root_disk"] is not None, disk_available)
        payload["status"] = "stale"
        self.assertFalse(summarize_fleet(payload, now=1025, acquired_at=1005)["ok"])

    async def test_slow_system_facts_do_not_expire_a_received_fleet_snapshot(self) -> None:
        clock = [1005]
        metrics_started = asyncio.Event()
        fleet_returned = asyncio.Event()

        async def facts(_host):
            await metrics_started.wait()
            await fleet_returned.wait()
            await asyncio.sleep(0)
            clock[0] = 1025
            return {"status": "fresh", "data": {"host": "tank", "facts": {}}}

        async def fleet():
            fleet_returned.set()
            return fleet_payload(1000)

        async def metrics(_host):
            metrics_started.set()
            return {"status": "unavailable"}

        client = SimpleNamespace(host=facts, fleet=fleet, host_metrics=metrics)
        with patch("src.plugins.ai_chat.fleet_tools.time.time", side_effect=lambda: clock[0]):
            result = await asyncio.wait_for(inspect_host(client, "tank"), timeout=1)
        self.assertEqual(result["acquired_at"], 1005)
        self.assertEqual(result["assembled_at"], 1025)
        self.assertEqual(result["hosts"][0]["root_disk"]["available_gib"], 76)
        self.assertEqual(result["hosts"][0]["status"], "online")

    def test_unavailable_alerts_are_not_zero_alerts(self) -> None:
        payload = fleet_payload(1000)
        payload["active_alerts"] = {"status": "unavailable"}
        summary = summarize_fleet(payload, host_id="tank", now=1000)
        self.assertEqual(summary["hosts"][0]["status"], "online")
        self.assertIsNone(summary["hosts"][0]["active_alert_count"])

    def test_single_host_inspection_does_not_inherit_overview_detail_limits(self) -> None:
        payload = fleet_payload(1000)
        payload["active_alerts"]["data"]["alerts"] = [
            {"labels": {"instance": "tank", "alertname": f"Alert{index}", "severity": "warning"},
             "annotations": {"summary": f"Finding {index}"}}
            for index in range(4)
        ]
        units = [{"unit": f"test{index}.service", "active_state": "failed"} for index in range(6)]
        payload["failed_units"] = {"status": "fresh", "observed_at": 1000,
            "data": {"hosts": [{"host": "tank", "state": "available", "units": units}]}}
        overview = summarize_fleet(payload, now=1000)["hosts"][-1]
        detail = summarize_fleet(payload, host_id="tank", now=1000)["hosts"][0]
        self.assertEqual(len(overview["alerts"]), 3)
        self.assertTrue(overview["alerts_truncated"])
        self.assertEqual(len(overview["failed_services"]), 5)
        self.assertTrue(overview["failed_services_truncated"])
        self.assertEqual(detail["active_alert_count"], 4)
        self.assertEqual([item["name"] for item in detail["alerts"]], [f"Alert{index}" for index in range(4)])
        self.assertFalse(detail["alerts_truncated"])
        self.assertEqual(detail["failed_service_count"], 6)
        self.assertEqual(len(detail["failed_services"]), 6)
        self.assertFalse(detail["failed_services_truncated"])
        self.assertEqual(detail["alerts_observed_at"], payload["active_alerts"].get("observed_at"))

    def test_host_query_has_no_dummy_service_argument(self) -> None:
        registry = ToolCatalog([HOST_INSPECT_TOOL, SERVICE_INSPECT_TOOL])
        self.assertTrue(registry.validate("host_inspect", {"host_id": "tank"}).ok)
        self.assertFalse(
            registry.validate("host_inspect", {"host_id": "tank", "unit": "q"}).ok
        )
        self.assertFalse(
            registry.validate(
                "service_inspect", {"host_id": "tank", "unit": "systemd"}
            ).ok
        )
        self.assertTrue(
            registry.validate(
                "service_inspect", {"host_id": "tank", "unit": "nginx.service"}
            ).ok
        )

    async def test_system_query_failure_does_not_erase_online_evidence(self) -> None:
        client = SimpleNamespace(
            host=AsyncMock(side_effect=FleetControlError("timeout", "query timed out")),
            fleet=AsyncMock(return_value=fleet_payload(int(time.time()))),
        )
        result = await inspect_host(client, "tank")
        self.assertTrue(result["ok"])
        self.assertEqual([h["host_id"] for h in result["hosts"]], ["tank"])
        self.assertEqual(result["hosts"][0]["status"], "online")
        self.assertEqual(result["system"]["status"], "unavailable")

    async def test_fleet_failure_does_not_erase_live_system_response(self) -> None:
        client = SimpleNamespace(
            host=AsyncMock(
                return_value={
                    "status": "fresh",
                    "data": {"host": "tank", "facts": {"kernel": "test"}},
                }
            ),
            fleet=AsyncMock(
                side_effect=FleetControlError("timeout", "query timed out")
            ),
        )
        result = await inspect_host(client, "tank")
        self.assertTrue(result["ok"])
        self.assertIn("tank 在线", result["summary"])

    async def test_qwen_listing_and_generation_reported_separately(self) -> None:
        profile = SimpleNamespace(
            name="qwen-local",
            model="qwen-test",
            circuit_breaker_enabled=False,
            api_key="SECRET",
        )
        runtime = SimpleNamespace(
            profile=profile,
            probe_once=AsyncMock(),
            snapshot=lambda: {
                "ready": True,
                "reason": "就绪",
                "state": "ready",
                "control_configured": False,
            },
        )
        context = SimpleNamespace(
            local_model=runtime,
            model_catalog=SimpleNamespace(profiles=[profile]),
            settings=SimpleNamespace(model_simple_chat_profile="qwen-local"),
            llm_gateway=SimpleNamespace(
                health_snapshot=lambda: {
                    "qwen-local": {
                        "last_failure_at": 1000,
                        "last_success_at": 900,
                        "last_error_kind": "empty_response",
                        "last_error": "SECRET",
                    }
                }
            ),
        )
        result = await model_status(context)
        runtime.probe_once.assert_awaited_once()
        self.assertTrue(result["readiness"]["ready"])
        self.assertEqual(result["requests"]["latest_result"], "failed")
        self.assertIn("最近一次实际请求失败", result["summary"])
        self.assertNotIn("SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
