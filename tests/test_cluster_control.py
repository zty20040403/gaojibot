from __future__ import annotations

import json
import os
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import nonebot

nonebot.init()

from src.cluster_control.api import create_app
from src.cluster_control.capabilities import capability_manifest
from src.cluster_control.config import (
    ClusterControlSettings,
    _diagnostic_targets,
    _inventory,
)
from src.cluster_control.diagnostics import (
    IncidentDiagnosticService,
    _summarize_model_routes,
)
from src.cluster_control.adapters.ops import (
    OpsClient,
    OpsError,
    OpsOperation,
    OpsResponse,
)
from src.cluster_control.service import FleetControlService
from src.plugins.ai_chat.fleet_client import FleetControlClient


class FakeStore:
    def __init__(self) -> None:
        self.backends: list[dict[str, object]] = []
        self.observations: list[dict[str, object]] = []
        self.closed = False

    def record_backend(self, **payload: object) -> None:
        self.backends.append(payload)

    def record_observation(self, **payload: object) -> int:
        self.observations.append(payload)
        return len(self.observations)

    def backend_snapshot(self) -> dict[str, object] | None:
        return self.backends[-1] if self.backends else None

    def recent(self, *, limit: int) -> list[dict[str, object]]:
        return self.observations[-limit:]

    def latest(
        self, *, operation: str, params: dict[str, object]
    ) -> dict[str, object] | None:
        for item in reversed(self.observations):
            if (
                item["operation"] == operation
                and item["params"] == params
                and not item["sensitive"]
                and item["payload"] is not None
                and item["status"] in {"fresh", "stale"}
            ):
                return {
                    "operation": operation,
                    "status": item["status"],
                    "data": item["payload"],
                    "sensitive": False,
                    "observed_at": item["observed_at"],
                    "received_at": item["received_at"],
                    "expires_at": item["expires_at"],
                    "duration_ms": item["duration_ms"],
                    "error_code": item["error_code"],
                }
        return None

    def close(self) -> None:
        self.closed = True


class FakeOps:
    def __init__(self) -> None:
        self.fail = False
        self.execute_calls = 0
        self.closed = False
        self.failure_code = "timeout"

    async def operations(self) -> tuple[OpsOperation, ...]:
        if self.fail:
            raise OpsError(
                self.failure_code,
                "upstream request failed",
                retryable=self.failure_code == "timeout",
            )
        schemas = {
            "fleet.overview": {},
            "units.failed": {},
            "alerts.active": {},
            "host.facts": {"host": "string"},
            "units.status": {"host": "string", "unit": "string"},
            "units.logs": {
                "host": "string",
                "unit": "string",
                "lines": "integer",
                "since_seconds": "integer",
            },
        }
        return tuple(
            OpsOperation(
                name=name,
                params_schema={
                    "type": "object",
                    "properties": {
                        key: {"type": value} for key, value in properties.items()
                    },
                    "required": [
                        key for key in ("host", "unit") if key in properties
                    ],
                },
            )
            for name, properties in schemas.items()
        )

    async def execute(
        self,
        operation: str,
        params: dict[str, object],
    ) -> OpsResponse:
        self.execute_calls += 1
        if self.fail:
            raise OpsError(
                self.failure_code,
                "upstream request failed",
                retryable=self.failure_code == "timeout",
            )
        if operation in {"fleet.overview", "units.failed"}:
            data: dict[str, object] = {"hosts": [], "observed_at": 123}
        elif operation == "alerts.active":
            data = {"alerts": [], "observed_at": 123}
        elif operation == "host.facts":
            data = {"host": params["host"], "facts": {}, "observed_at": 123}
        elif operation == "units.status":
            data = {
                "host": params["host"],
                "unit": {"unit": params["unit"]},
                "observed_at": 123,
            }
        else:
            data = {
                "host": params["host"],
                "unit": params["unit"],
                "entries": [],
                "observed_at": 123,
            }
        return OpsResponse(data=data, elapsed_ms=1)

    async def close(self) -> None:
        self.closed = True


class HealthyOps(FakeOps):
    async def execute(
        self,
        operation: str,
        params: dict[str, object],
    ) -> OpsResponse:
        if operation == "units.status":
            self.execute_calls += 1
            return OpsResponse(
                data={
                    "host": params["host"],
                    "unit": {
                        "unit": params["unit"],
                        "load_state": "loaded",
                        "active_state": "active",
                        "sub_state": "running",
                    },
                    "observed_at": 123,
                },
                elapsed_ms=1,
            )
        return await super().execute(operation, params)


class FakeDiagnosticStore:
    def __init__(self) -> None:
        self.evidence: list[object] = []
        self.finished: dict[str, object] = {}

    def create_run(self, **_payload: object) -> int:
        return 7

    def add_evidence(self, _run_id: int, item: object) -> int:
        self.evidence.append(item)
        return len(self.evidence)

    def finish_run(self, _run_id: int, **payload: object) -> None:
        self.finished = payload

    def get(self, _run_id: int) -> dict[str, object]:
        return {
            "run_id": 7,
            "handle": "diagnostic#7",
            "status": self.finished.get("status", "running"),
            "confidence": self.finished.get("confidence", "unknown"),
            "summary": self.finished.get("summary", ""),
            "probe_count": self.finished.get("probe_count", 0),
            "evidence": [
                {**asdict(item), "evidence_id": index, "handle": f"evidence#{index}"}
                for index, item in enumerate(self.evidence, 1)
            ],
        }

    def recent(self, *, limit: int) -> list[dict[str, object]]:
        return [self.get(7)][:limit]

    def runtime_snapshot(self, kind: str) -> dict[str, object]:
        return {"status": "passed", "facts": {"kind": kind}}

    def database_snapshot(self) -> dict[str, object]:
        return {"status": "passed", "facts": {"overall": "healthy"}}


class OpsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_filters_mutations_and_execute_checks_catalog(self) -> None:
        with TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("x" * 40, encoding="ascii")

            async def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.headers["authorization"], "Bearer " + "x" * 40)
                if request.url.path == "/v1/operations":
                    return httpx.Response(
                        200,
                        json={
                            "version": 2,
                            "operations": [
                                {
                                    "name": "fleet.overview",
                                    "read_only": True,
                                    "params_schema": {},
                                },
                                {
                                    "name": "units.restart",
                                    "read_only": False,
                                    "params_schema": {},
                                },
                            ],
                        },
                    )
                self.assertEqual(
                    json.loads(request.content),
                    {"op": "fleet.overview", "params": {}},
                )
                return httpx.Response(200, json={"hosts": []})

            client = OpsClient(
                "http://ops.test",
                token_file,
                transport=httpx.MockTransport(handler),
            )
            try:
                operations = await client.operations()
                self.assertEqual([item.name for item in operations], ["fleet.overview"])
                self.assertEqual(client.catalog_version, 2)
                response = await client.execute("fleet.overview", {})
                self.assertEqual(response.data, {"hosts": []})
                with self.assertRaises(OpsError) as caught:
                    await client.execute("units.restart", {})
                self.assertEqual(caught.exception.code, "unsupported")
            finally:
                await client.close()


class FleetControlClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_authenticated_log_query(self) -> None:
        with TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("c" * 40, encoding="ascii")

            async def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.headers["authorization"], "Bearer " + "c" * 40)
                self.assertEqual(
                    request.url.path,
                    "/v1/hosts/h610/units/nginx.service/logs",
                )
                self.assertEqual(request.url.params["lines"], "200")
                self.assertEqual(request.url.params["since_seconds"], "86400")
                return httpx.Response(200, json={"ok": True, "data": {"entries": []}})

            client = FleetControlClient(
                "http://control.test",
                token_file,
                transport=httpx.MockTransport(handler),
            )
            try:
                payload = await client.logs(
                    "h610",
                    "nginx.service",
                    lines=999,
                    since_seconds=999999,
                )
                self.assertTrue(payload["ok"])
            finally:
                await client.close()


class ClusterControlConfigTests(unittest.TestCase):
    def test_defaults_are_local_and_observer_follows_configured_host(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ClusterControlSettings.from_env().local_host_id, "local")
        with patch.dict(os.environ, {"KC_LOCAL_HOST_ID": "worker-a",
            "KC_DIAGNOSTIC_TARGETS_JSON": json.dumps([{"target_id": "model", "kind": "model",
                                                     "url": "http://127.0.0.1:8000/health"}])}, clear=True):
            settings = ClusterControlSettings.from_env()
        self.assertEqual(settings.local_host_id, "worker-a")
        self.assertEqual(settings.diagnostic_targets[0]["observer_host"], "worker-a")
        self.assertEqual(settings.diagnostic_targets[0]["host_id"], "worker-a")

    def test_inventory_is_normalized_and_requires_explicit_observation(self) -> None:
        inventory = _inventory(json.dumps([
            {
                "host_id": "h610",
                "roles": ["bot"],
                "readable_units": ["nginx.service", "nginx.service"],
            }
        ]))
        self.assertFalse(inventory[0]["observe"])
        self.assertEqual(inventory[0]["readable_units"], ["nginx.service"])
        self.assertEqual(inventory[0]["permission_source"], "unconfirmed")

    def test_inventory_rejects_implicit_boolean_and_non_service_unit(self) -> None:
        with self.assertRaises(ValueError):
            _inventory(json.dumps([{"host_id": "h610", "observe": "true"}]))
        with self.assertRaises(ValueError):
            _inventory(json.dumps([
                {"host_id": "h610", "readable_units": ["/bin/sh"]}
            ]))

    def test_diagnostic_target_binds_host_and_service(self) -> None:
        targets = _diagnostic_targets(json.dumps([
            {
                "target_id": "admin",
                "kind": "admin",
                "url": "http://127.0.0.1:8080/health",
                "observer_host": "h610",
                "host_id": "tank",
                "service_ref": "nginx.service",
            }
        ]))
        self.assertEqual(targets[0]["host_id"], "tank")
        self.assertEqual(targets[0]["service_ref"], "nginx.service")

    def test_settings_reject_unreadable_diagnostic_service(self) -> None:
        environment = {
            "KC_LOCAL_HOST_ID": "h610",
            "KC_API_TOKEN_FILE": "/run/credentials/control-token",
            "AI_POSTGRES_DSN": "postgresql://localhost/gaoji",
            "KC_INVENTORY_JSON": json.dumps([
                {
                    "host_id": "h610",
                    "observe": True,
                    "readable_units": ["gaoji.service"],
                }
            ]),
            "KC_DIAGNOSTIC_TARGETS_JSON": json.dumps([
                {
                    "target_id": "admin",
                    "kind": "admin",
                    "url": "http://127.0.0.1:8080/health",
                    "observer_host": "h610",
                    "service_ref": "nginx.service",
                }
            ]),
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = ClusterControlSettings.from_env()
        with self.assertRaisesRegex(ValueError, "unreadable service"):
            settings.validate()


class OpsCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_upstream_schema_is_not_advertised(self) -> None:
        operation = OpsOperation(
            name="host.facts",
            params_schema={
                "type": "object",
                "properties": {"host": {"type": "integer"}},
                "required": ["host"],
            },
        )
        manifest = capability_manifest({operation.name: operation})
        host_capability = next(
            item for item in manifest if item["name"] == "host.facts.read"
        )
        self.assertFalse(host_capability["available"])
        self.assertEqual(
            host_capability["reason"], "upstream_schema_incompatible"
        )

    async def test_rejects_invalid_url_and_credential(self) -> None:
        with TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("short", encoding="ascii")
            with self.assertRaises(ValueError):
                OpsClient("file:///tmp/socket", token_file)
            client = OpsClient(
                "http://ops.test",
                token_file,
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(500)
                ),
            )
            try:
                with self.assertRaises(OpsError) as caught:
                    await client.operations()
                self.assertEqual(caught.exception.code, "credential_invalid")
            finally:
                await client.close()


class FleetControlServiceTests(unittest.IsolatedAsyncioTestCase):
    inventory = (
        {
            "host_id": "h610",
            "observe": True,
            "readable_units": ["nginx.service"],
        },
    )

    def test_observed_at_accepts_ops_rfc3339_timestamp(self) -> None:
        self.assertEqual(
            FleetControlService._observed_at(
                {"observed_at": "2026-09-06T07:00:00Z"}
            ),
            1788678000,
        )
        self.assertIsNone(
            FleetControlService._observed_at({"observed_at": "not-a-time"})
        )

    async def test_deduplicates_fresh_queries_and_uses_stale_evidence(self) -> None:
        ops = FakeOps()
        store = FakeStore()
        service = FleetControlService(
            ops,
            store=store,
            inventory=self.inventory,
            cache_seconds=20,
        )
        first = await service.query_capability("fleet.read", {})
        second = await service.query_capability("fleet.read", {})
        self.assertEqual(first.status.value, "fresh")
        self.assertTrue(second.cached)
        self.assertEqual(ops.execute_calls, 1)

        key = service._cache_key("fleet.overview", {})
        service._cache[key] = service._cache[key].__class__(
            result=service._cache[key].result,
            stored_at=time.monotonic() - 30,
        )
        ops.fail = True
        stale = await service.query_capability("fleet.read", {})
        self.assertEqual(stale.status.value, "stale")
        self.assertEqual(stale.data, first.data)
        self.assertEqual(stale.error.code, "timeout")
        self.assertEqual(len(store.observations), 2)

        await service.close()
        self.assertTrue(ops.closed)
        self.assertTrue(store.closed)

    async def test_authorization_failure_never_returns_stale_data(self) -> None:
        ops = FakeOps()
        service = FleetControlService(
            ops,
            store=FakeStore(),
            inventory=self.inventory,
            cache_seconds=20,
        )
        await service.query_capability("fleet.read", {})
        key = service._cache_key("fleet.overview", {})
        service._cache[key] = service._cache[key].__class__(
            result=service._cache[key].result,
            stored_at=time.monotonic() - 30,
        )
        ops.fail = True
        ops.failure_code = "forbidden"
        denied = await service.query_capability("fleet.read", {})
        self.assertEqual(denied.status.value, "forbidden")
        self.assertIsNone(denied.data)
        self.assertNotIn(key, service._cache)
        await service.close()

    async def test_cache_ttl_begins_at_response_not_before_slow_request(self) -> None:
        clock = [1000]

        class SlowOps(FakeOps):
            async def execute(self, *args, **kwargs):
                clock[0] += 25
                return await super().execute(*args, **kwargs)

        service = FleetControlService(SlowOps(), store=FakeStore(), inventory=self.inventory,
                                      cache_seconds=20)
        with patch('src.cluster_control.service.time.time', side_effect=lambda: clock[0]):
            result = await service.query_capability('fleet.read', {})
        self.assertEqual(result.received_at, 1025)
        self.assertEqual(result.expires_at, 1045)
        await service.close()

    async def test_expired_payload_is_not_reused_with_recent_cache_insertion(self) -> None:
        ops = FakeOps()
        service = FleetControlService(ops, store=FakeStore(), inventory=self.inventory,
                                      cache_seconds=20)
        await service.query_capability('fleet.read', {})
        key = service._cache_key('fleet.overview', {})
        entry = service._cache[key]
        service._cache[key] = replace(entry, result=replace(entry.result, expires_at=int(time.time()) - 1))
        result = await service.query_capability('fleet.read', {})
        self.assertEqual(ops.execute_calls, 2)
        self.assertFalse(result.cached)
        self.assertGreater(result.expires_at, int(time.time()))
        await service.close()

    async def test_sensitive_log_is_marked_before_persistence(self) -> None:
        ops = FakeOps()
        store = FakeStore()
        service = FleetControlService(ops, store=store, inventory=self.inventory)
        result = await service.unit_logs("h610", "nginx.service", 20)
        self.assertTrue(result["ok"])
        self.assertTrue(store.observations[-1]["sensitive"])
        await service.close()

    async def test_local_inventory_denies_unregistered_hosts_and_units(self) -> None:
        ops = FakeOps()
        service = FleetControlService(ops, inventory=self.inventory)
        host = await service.host_facts("tank")
        unit = await service.unit_status("h610", "postgresql.service")
        self.assertEqual(host["status"], "forbidden")
        self.assertEqual(unit["status"], "forbidden")
        self.assertEqual(ops.execute_calls, 0)
        await service.close()

    async def test_aggregate_results_are_scoped_to_local_inventory(self) -> None:
        class AggregateOps(FakeOps):
            async def execute(
                self, operation: str, params: dict[str, object]
            ) -> OpsResponse:
                self.execute_calls += 1
                if operation in {"fleet.overview", "units.failed"}:
                    return OpsResponse(
                        data={
                            "hosts": [
                                {"host": "h610", "state": "available"},
                                {"host": "tank", "state": "available"},
                            ]
                        },
                        elapsed_ms=1,
                    )
                return OpsResponse(
                    data={
                        "alerts": [
                            {"labels": {"instance": "h610"}},
                            {"labels": {"instance": "tank"}},
                        ]
                    },
                    elapsed_ms=1,
                )

        service = FleetControlService(AggregateOps(), inventory=self.inventory)
        fleet = await service.fleet_overview()
        self.assertEqual(
            [item["host"] for item in fleet["data"]["hosts"]], ["h610"]
        )
        self.assertEqual(
            [item["host"] for item in fleet["failed_units"]["data"]["hosts"]],
            ["h610"],
        )
        self.assertEqual(
            len(fleet["active_alerts"]["data"]["alerts"]), 1
        )
        await service.close()

    async def test_incompatible_schema_is_blocked_before_execution(self) -> None:
        class IncompatibleOps(FakeOps):
            async def operations(self) -> tuple[OpsOperation, ...]:
                operations = list(await super().operations())
                operations[0] = OpsOperation(
                    name="fleet.overview",
                    params_schema={"type": "array"},
                )
                return tuple(operations)

        ops = IncompatibleOps()
        service = FleetControlService(ops, inventory=self.inventory)
        result = await service.query_capability("fleet.read", {})
        self.assertEqual(result.status.value, "unsupported")
        self.assertEqual(result.error.code, "incompatible_catalog")
        self.assertEqual(ops.execute_calls, 0)
        await service.close()

    async def test_cross_host_response_is_rejected(self) -> None:
        class CrossHostOps(FakeOps):
            async def execute(
                self, operation: str, params: dict[str, object]
            ) -> OpsResponse:
                self.execute_calls += 1
                return OpsResponse(
                    data={"host": "tank", "facts": {}, "observed_at": 123},
                    elapsed_ms=1,
                )

        service = FleetControlService(CrossHostOps(), inventory=self.inventory)
        result = await service.query_capability("host.facts.read", {"host": "h610"})
        self.assertEqual(result.status.value, "unavailable")
        self.assertEqual(result.error.code, "invalid_response")
        await service.close()

    async def test_restart_can_use_durable_non_sensitive_projection(self) -> None:
        store = FakeStore()
        first_service = FleetControlService(
            FakeOps(), store=store, inventory=self.inventory
        )
        original = await first_service.query_capability("fleet.read", {})
        first_service.store = None
        await first_service.close()

        unavailable = FakeOps()
        unavailable.fail = True
        restarted = FleetControlService(
            unavailable, store=store, inventory=self.inventory
        )
        result = await restarted.query_capability("fleet.read", {})
        self.assertEqual(result.status.value, "stale")
        self.assertEqual(result.data, original.data)
        await restarted.close()


class IncidentDiagnosticServiceTests(unittest.IsolatedAsyncioTestCase):
    inventory = (
        {
            "host_id": "h610",
            "observe": True,
            "readable_units": [
                "gaoji.service",
                "docker-napcat.service",
                "nginx.service",
                "gaoji-cluster-control.service",
            ],
        },
    )

    def test_model_route_summary_classifies_parameter_failures(self) -> None:
        import zlib

        payload = zlib.compress(
            json.dumps(
                {
                    "model_routing": [
                        {
                            "fallback": True,
                            "outcomes": [
                                {
                                    "status": "failed",
                                    "reason_code": "invalid_request",
                                },
                                {
                                    "status": "succeeded",
                                    "reason_code": "direct",
                                },
                            ],
                        }
                    ]
                }
            ).encode("utf-8")
        )
        facts = _summarize_model_routes([{"payload": payload}])
        self.assertEqual(facts["attempts"], 2)
        self.assertEqual(facts["failed"], 1)
        self.assertEqual(facts["fallbacks"], 1)
        self.assertEqual(facts["parameter_incompatible"], 1)

    async def test_qq_diagnostic_uses_bounded_independent_checks(self) -> None:
        fleet = FleetControlService(HealthyOps(), inventory=self.inventory)
        store = FakeDiagnosticStore()
        diagnostics = IncidentDiagnosticService(fleet, store)  # type: ignore[arg-type]
        try:
            result = await diagnostics.run(
                template_key="qq_no_reply",
                host_id="h610",
                requested_by="test",
            )
            self.assertEqual(result["handle"], "diagnostic#7")
            self.assertLessEqual(len(store.evidence), 6)
            self.assertTrue(all(item.phase <= 2 for item in store.evidence))
            self.assertEqual(store.finished["confidence"], "contradicted")
        finally:
            await diagnostics.close()
            await fleet.close()

    async def test_unconfigured_model_target_stays_unknown_and_adds_one_log_check(self) -> None:
        fleet = FleetControlService(HealthyOps(), inventory=self.inventory)
        store = FakeDiagnosticStore()
        diagnostics = IncidentDiagnosticService(fleet, store)  # type: ignore[arg-type]
        try:
            await diagnostics.run(
                template_key="model_connectivity",
                host_id="h610",
            )
            checks = [item.check_name for item in store.evidence]
            self.assertIn("fixed_target", checks)
            self.assertIn("service_log_signals", checks)
            self.assertEqual(max(item.phase for item in store.evidence), 2)
            self.assertEqual(store.finished["status"], "inconclusive")
        finally:
            await diagnostics.close()
            await fleet.close()

    async def test_fixed_probe_records_dns_and_http_without_accepting_a_url(self) -> None:
        fleet = FleetControlService(HealthyOps(), inventory=self.inventory)
        store = FakeDiagnosticStore()
        targets = _diagnostic_targets(
            json.dumps(
                [
                    {
                        "target_id": "qwen-local",
                        "kind": "model",
                        "url": "http://127.0.0.1:8000/v1/models",
                        "observer_host": "h610",
                    }
                ]
            )
        )
        diagnostics = IncidentDiagnosticService(
            fleet,
            store,  # type: ignore[arg-type]
            targets,
            local_host_id="h610",
        )
        await diagnostics._http.aclose()
        diagnostics._http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"data": [{"id": "qwen"}]})
            )
        )
        try:
            await diagnostics.run(
                template_key="model_connectivity",
                host_id="h610",
                target_id="qwen-local",
            )
            by_check = {item.check_name: item for item in store.evidence}
            self.assertEqual(by_check["dns_resolution"].status, "passed")
            self.assertEqual(by_check["http_request"].facts["item_count"], 1)
        finally:
            await diagnostics.close()
            await fleet.close()

    async def test_diagnostic_rejects_hosts_outside_inventory(self) -> None:
        fleet = FleetControlService(HealthyOps(), inventory=self.inventory)
        diagnostics = IncidentDiagnosticService(
            fleet,
            FakeDiagnosticStore(),  # type: ignore[arg-type]
        )
        try:
            with self.assertRaises(PermissionError):
                await diagnostics.run(
                    template_key="host_unreachable",
                    host_id="tank",
                )
        finally:
            await diagnostics.close()
            await fleet.close()

    async def test_probe_does_not_claim_a_different_observer_host(self) -> None:
        fleet = FleetControlService(HealthyOps(), inventory=self.inventory)
        store = FakeDiagnosticStore()
        targets = _diagnostic_targets(
            json.dumps(
                [
                    {
                        "target_id": "remote-model",
                        "kind": "model",
                        "url": "http://127.0.0.1:8000/v1/models",
                        "observer_host": "tank",
                    }
                ]
            )
        )
        diagnostics = IncidentDiagnosticService(
            fleet,
            store,  # type: ignore[arg-type]
            targets,
            local_host_id="h610",
        )
        try:
            await diagnostics.run(
                template_key="model_connectivity",
                host_id="h610",
                target_id="remote-model",
            )
            fixed = next(
                item for item in store.evidence if item.check_name == "fixed_target"
            )
            self.assertEqual(fixed.status, "unknown")
            self.assertEqual(fixed.facts["actual_observer"], "h610")
        finally:
            await diagnostics.close()
            await fleet.close()


class ClusterControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_requires_its_own_bearer_token(self) -> None:
        with TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("z" * 40, encoding="ascii")
            service = FleetControlService(
                None,
                inventory=({"host_id": "h610", "observe": True},),
            )
            app = create_app(service, api_token_file=token_file)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://control.test",
            ) as client:
                self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertEqual((await client.get("/v1/fleet")).status_code, 401)
                response = await client.get(
                    "/v1/fleet",
                    headers={"Authorization": "Bearer " + "z" * 40},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "unavailable")
                self.assertEqual(
                    response.json()["inventory"],
                    [{"host_id": "h610", "observe": True}],
                )
                metrics = await client.get("/metrics/")
                self.assertEqual(metrics.status_code, 200)
                self.assertIn(
                    "gaoji_cluster_queries_total",
                    metrics.text,
                )


if __name__ == "__main__":
    unittest.main()
