from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import AsyncMock, Mock

import httpx
import nonebot

nonebot.init()

from src.cluster_control.guardian import (
    GuardianService,
    guardian_status_after_check,
    materialize_guardian_action,
    require_guardian_mode_capability,
    resolve_guardian_target,
    validate_guardian_action,
)
from src.cluster_control.execution_contracts import WorkerJobProposal
from src.cluster_control.reliability import ReliabilityStore
from src.cluster_control.scheduling import (
    ResourceRequest,
    eligibility_reason,
    settle_reported_cost,
)
from src.cluster_worker.service import ClusterWorker
from src.plugins.ai_chat.fleet_case_recall import semantic_runbook_scores
from src.plugins.ai_chat.semantic_recall import SemanticHit


def _worker() -> dict:
    return {
        "worker_id": "b650-worker",
        "availability": "available",
        "runtime_json": json.dumps({"system": "linux", "machine": "x86_64"}),
    }


def _grant() -> dict:
    return {
        "grant_id": "grant_" + "a" * 32,
        "status": "available",
        "valid_from": 100,
        "valid_until": 1000,
        "allowed_kinds_json": json.dumps(["media.inspect"]),
        "max_priority": 80,
        "cpu_limit_millis": 4000,
        "memory_limit_bytes": 4 * 1024**3,
        "gpu_limit_slots": 1,
        "budget_limit_microunits": 1000,
        "budget_reserved_microunits": 0,
        "budget_spent_microunits": 0,
    }


class SchedulingPolicyTests(unittest.TestCase):
    def test_resource_request_rejects_ambiguous_boolean_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe_rerun must be a boolean"):
            ResourceRequest.parse({"safe_rerun": "false"})
        with self.assertRaisesRegex(ValueError, "priority must be an integer"):
            ResourceRequest.parse({"priority": True})
        self.assertFalse(ResourceRequest.parse({"safe_rerun": False}).safe_rerun)

    def test_job_kind_catalog_controls_rerun_and_checkpoint_policy(self) -> None:
        common = {
            "payload": {"artifact_id": "artifact_" + "a" * 32},
            "idempotency_key": "job-policy-test",
            "constraints": {"safe_rerun": False, "checkpointable": False},
        }
        inspection = WorkerJobProposal.parse(
            {**common, "kind": "artifact.inspect"}, now=100
        )
        self.assertTrue(inspection.constraints["safe_rerun"])
        self.assertTrue(inspection.constraints["checkpointable"])

        preview = WorkerJobProposal.parse(
            {
                **common,
                "kind": "preview.static",
                "payload": {
                    "artifact_id": "artifact_" + "a" * 32,
                    "ttl_seconds": 3600,
                },
                "constraints": {"safe_rerun": False, "checkpointable": False},
            },
            now=100,
        )
        self.assertTrue(preview.constraints["safe_rerun"])
        self.assertTrue(preview.constraints["checkpointable"])

    def test_missing_owner_policy_fails_closed(self) -> None:
        self.assertEqual(
            eligibility_reason(
                ResourceRequest.parse({}),
                worker=_worker(),
                host={"gpu_compute": False},
                policy=None,
                grant=None,
                job_kind="media.inspect",
                now=200,
            ),
            "owner_policy_missing",
        )

    def test_gpu_needs_host_and_owner_authorization(self) -> None:
        request = ResourceRequest.parse(
            {
                "gpu_slots": 1,
                "borrow_required": True,
                "expected_cost_microunits": 100,
                "max_cost_microunits": 100,
            }
        )
        policy = {
            "desired_availability": "available",
            "allow_gpu": True,
        }
        self.assertEqual(
            eligibility_reason(
                request, worker=_worker(), host={"site": "home", "gpu_compute": False},
                policy=policy, grant=_grant(), job_kind="media.inspect", now=200,
            ),
            "gpu_not_authorized",
        )
        self.assertEqual(
            eligibility_reason(
                request, worker=_worker(), host={"site": "home", "gpu_compute": True},
                policy=policy, grant=_grant(), job_kind="media.inspect", now=200,
            ),
            "",
        )

    def test_draining_owner_state_stops_new_claims(self) -> None:
        self.assertEqual(
            eligibility_reason(
                ResourceRequest.parse({}), worker=_worker(),
                host={"gpu_compute": False},
                policy={"desired_availability": "draining"}, grant=None,
                job_kind="probe.http", now=200,
            ),
            "owner_draining",
        )

    def test_cost_reservation_cannot_exceed_grant(self) -> None:
        grant = _grant()
        grant["budget_reserved_microunits"] = 850
        request = ResourceRequest.parse(
            {
                "borrow_required": True,
                "expected_cost_microunits": 50,
                "max_cost_microunits": 200,
            }
        )
        self.assertEqual(
            eligibility_reason(
                request, worker=_worker(), host={"gpu_compute": True},
                policy={"desired_availability": "available"}, grant=grant,
                job_kind="media.inspect", now=200,
            ),
            "cost_budget_exhausted",
        )

    def test_grant_resource_limits_include_existing_reservations(self) -> None:
        request = ResourceRequest.parse(
            {
                "borrow_required": True,
                "cpu_millis": 1000,
                "memory_bytes": 512 * 1024**2,
            }
        )
        self.assertEqual(
            eligibility_reason(
                request,
                worker=_worker(),
                host={"gpu_compute": False},
                policy={"desired_availability": "available"},
                grant=_grant(),
                grant_usage={
                    "cpu_millis": 3500,
                    "memory_bytes": 512 * 1024**2,
                    "gpu_slots": 0,
                },
                job_kind="media.inspect",
                now=200,
            ),
            "grant_cpu_limit",
        )

    def test_cost_settlement_enforces_the_hard_limit(self) -> None:
        request = ResourceRequest.parse(
            {"expected_cost_microunits": 100, "max_cost_microunits": 200}
        )
        accepted = settle_reported_cost(request, 150)
        self.assertEqual(accepted.settled_microunits, 150)
        self.assertFalse(accepted.limit_exceeded)
        self.assertFalse(accepted.invalid_report)

        exceeded = settle_reported_cost(request, 250)
        self.assertEqual(exceeded.settled_microunits, 200)
        self.assertTrue(exceeded.limit_exceeded)

        invalid = settle_reported_cost(request, True)
        self.assertTrue(invalid.invalid_report)

    def test_external_borrow_always_requires_a_matching_grant(self) -> None:
        request = ResourceRequest.parse({})
        policy = {"desired_availability": "available"}
        self.assertEqual(
            eligibility_reason(
                request, worker=_worker(), host={"gpu_compute": False},
                policy=policy, grant=None, job_kind="media.inspect", now=200,
                external_borrow=True,
            ),
            "borrow_grant_required",
        )
        self.assertEqual(
            eligibility_reason(
                request, worker=_worker(), host={"gpu_compute": False},
                policy=policy, grant=_grant(), job_kind="media.inspect", now=200,
                external_borrow=True,
            ),
            "",
        )

    def test_owner_work_does_not_require_a_borrow_grant(self) -> None:
        self.assertEqual(
            eligibility_reason(
                ResourceRequest.parse({}), worker=_worker(),
                host={"gpu_compute": False},
                policy={"desired_availability": "available"}, grant=None,
                job_kind="media.inspect", now=200, external_borrow=False,
            ),
            "",
        )

    def test_case_applicability_is_revalidated(self) -> None:
        ok, reasons = ReliabilityStore.validate_applicability(
            {"runtime.system_closure": "/nix/store/new"},
            {"runtime": {"system_closure": "/nix/store/old"}},
        )
        self.assertFalse(ok)
        self.assertEqual(reasons, ["runtime.system_closure changed"])

    def test_guardian_target_binding_is_server_authoritative(self) -> None:
        targets = {
            "admin": {
                "target_id": "admin",
                "observer_host": "h610",
                "host_id": "tank",
                "service_ref": "nginx.service",
            }
        }
        self.assertEqual(
            resolve_guardian_target({"target_id": "admin"}, targets),
            ("tank", "nginx.service"),
        )
        with self.assertRaises(PermissionError):
            resolve_guardian_target(
                {"target_id": "admin", "host_id": "h610"}, targets
            )
        with self.assertRaises(PermissionError):
            resolve_guardian_target(
                {"target_id": "admin", "service_ref": "ssh.service"}, targets
            )

    def test_guardian_attention_state_can_recover_or_expire(self) -> None:
        common = {
            "failure_threshold": 3,
            "mode": "observe",
            "actions_used": 0,
            "max_actions": 0,
            "requires_attention": False,
        }
        self.assertEqual(
            guardian_status_after_check(
                outcome="failed", failures=3, expired=False, **common
            ),
            "needs_attention",
        )
        self.assertEqual(
            guardian_status_after_check(
                outcome="passed", failures=0, expired=False, **common
            ),
            "active",
        )
        self.assertEqual(
            guardian_status_after_check(
                outcome="failed", failures=4, expired=True, **common
            ),
            "completed",
        )

    def test_remediation_requires_an_available_write_backend(self) -> None:
        with self.assertRaises(PermissionError):
            require_guardian_mode_capability(
                "remediate", remediation_available=False
            )
        require_guardian_mode_capability("observe", remediation_available=False)
        require_guardian_mode_capability("remediate", remediation_available=True)

    def test_verified_runbook_requires_replay_evidence(self) -> None:
        store = ReliabilityStore(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "verified runbook cases require"):
            store.create_case(
                {
                    "title": "service outage",
                    "symptoms": "HTTP 503",
                    "confirmed_cause": "",
                    "resolution": ["restart the approved service"],
                    "applicability": {},
                    "evidence_refs": [],
                    "status": "verified",
                    "confidence": "confirmed",
                },
                actor_id="admin:kenneth",
            )

    def test_incident_key_rejects_silent_truncation(self) -> None:
        store = ReliabilityStore(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "incident_key"):
            store.observe_incident(
                incident_key="x" * 241,
                host_id="h610",
                service_ref="nginx.service",
                severity="warning",
                summary="failed",
                event_type="probe_failed",
                source_ref="probe",
                confidence="confirmed",
                payload={},
            )

    def test_incident_recovery_lock_is_bound_to_the_incident_key(self) -> None:
        cursor = Mock()
        cursor.execute.return_value = cursor
        cursor.fetchone.return_value = {
            "incident_id": "incident_" + "a" * 32,
            "incident_key": "guardian:admin",
            "status": "open",
            "resource_version": 1,
        }
        connection = Mock()
        connection.cursor.return_value = cursor
        database = Mock()
        database.store_connection.return_value = connection
        store = ReliabilityStore(database)
        store.incident = Mock(return_value={"status": "resolved"})  # type: ignore[method-assign]

        store.recover_incident(
            incident_key="guardian:admin", source_ref="guardian", payload={}
        )

        queries = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertIn("pg_advisory_xact_lock", queries[0])
        self.assertIn("incident_key = ?", queries[1])

    def test_guardian_action_is_bound_and_materialized_at_execution_time(self) -> None:
        template = validate_guardian_action(
            {
                "host_id": "h610",
                "resource_ref": "nginx.service",
                "operation": "service.restart",
                "verification": {"probe": "admin"},
            },
            mode="remediate",
            max_actions=1,
            host_id="h610",
            service_ref="nginx.service",
        )
        action = materialize_guardian_action(
            template,
            {
                "guardian_id": "guardian_" + "8" * 32,
                "host_id": "h610",
                "service_ref": "nginx.service",
                "actions_used": 0,
                "expires_at": 500,
            },
            now=100,
        )
        self.assertEqual(action["deadline_at"], 400)
        self.assertEqual(
            action["idempotency_key"], "guardian:guardian_" + "8" * 32 + ":1"
        )
        with self.assertRaises(PermissionError):
            validate_guardian_action(
                {
                    "host_id": "tank",
                    "resource_ref": "nginx.service",
                    "operation": "service.restart",
                },
                mode="remediate",
                max_actions=1,
                host_id="h610",
                service_ref="nginx.service",
            )
        with self.assertRaises(ValueError):
            validate_guardian_action(
                {
                    "host_id": "h610",
                    "resource_ref": "nginx.service",
                    "operation": "service.restart",
                    "deadline_at": 200,
                },
                mode="remediate",
                max_actions=1,
                host_id="h610",
                service_ref="nginx.service",
            )


class _GuardianStore:
    def __init__(self, guardian: dict) -> None:
        self.items = [guardian]
        self.finished: list[dict] = []
        self.incidents: list[dict] = []
        self.recoveries: list[dict] = []

    def claim_due_guardians(self, **_kwargs: object) -> list[dict]:
        items, self.items = self.items, []
        return items

    def finish_guardian_check(self, _guardian_id: str, **kwargs: object) -> dict:
        self.finished.append(dict(kwargs))
        return {}

    def observe_incident(self, **kwargs: object) -> dict:
        self.incidents.append(dict(kwargs))
        return {"incident_id": "incident_" + "b" * 32}

    def recover_incident(self, **kwargs: object) -> dict:
        self.recoveries.append(dict(kwargs))
        return {"incident_id": "incident_" + "b" * 32}


class GuardianRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_check_does_not_request_model_or_action(self) -> None:
        store = _GuardianStore(
            {
                "guardian_id": "guardian_" + "a" * 32,
                "target_id": "admin",
                "host_id": "h610",
                "service_ref": "",
                "mode": "observe",
                "consecutive_failures": 0,
                "failure_threshold": 3,
                "actions_used": 0,
                "max_actions": 0,
                "authorized_action": {},
                "actor_id": "admin:kenneth",
                "origin_scope": "admin-console",
            }
        )
        actions: list[dict] = []
        service = GuardianService(
            store,  # type: ignore[arg-type]
            ({"target_id": "admin", "url": "http://admin.test/health"},),
            operation_factory=AsyncMock(side_effect=lambda payload, _owner: actions.append(payload) or {}),
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
            trust_env=False,
        )
        try:
            self.assertEqual(await service.tick(), 1)
        finally:
            await service.close()
        self.assertEqual(actions, [])
        self.assertEqual(store.incidents, [])
        self.assertEqual(store.finished[0]["outcome"], "passed")

    async def test_redirect_is_not_treated_as_healthy(self) -> None:
        store = _GuardianStore(
            {
                "guardian_id": "guardian_" + "f" * 32,
                "target_id": "admin",
                "host_id": "h610",
                "service_ref": "",
                "mode": "observe",
                "consecutive_failures": 0,
                "failure_threshold": 3,
                "actions_used": 0,
                "max_actions": 0,
                "authorized_action": {},
                "actor_id": "admin:kenneth",
                "origin_scope": "admin-console",
            }
        )
        service = GuardianService(
            store,  # type: ignore[arg-type]
            ({"target_id": "admin", "url": "http://admin.test/health"},),
            operation_factory=AsyncMock(return_value={}),
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(302, headers={"location": "/login"})
            ),
            trust_env=False,
        )
        try:
            self.assertEqual(await service.tick(), 1)
        finally:
            await service.close()
        self.assertEqual(store.finished[0]["outcome"], "failed")

    async def test_recovery_closes_the_guardian_incident(self) -> None:
        store = _GuardianStore(
            {
                "guardian_id": "guardian_" + "9" * 32,
                "target_id": "admin",
                "host_id": "h610",
                "service_ref": "nginx.service",
                "mode": "observe",
                "consecutive_failures": 3,
                "failure_threshold": 3,
                "actions_used": 0,
                "max_actions": 0,
                "authorized_action": {},
                "actor_id": "admin:kenneth",
                "origin_scope": "admin-console",
            }
        )
        service = GuardianService(
            store,  # type: ignore[arg-type]
            ({"target_id": "admin", "url": "http://admin.test/health"},),
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
            trust_env=False,
        )
        try:
            await service.tick()
        finally:
            await service.close()
        self.assertEqual(len(store.recoveries), 1)
        self.assertEqual(
            store.finished[0]["incident_id"], "incident_" + "b" * 32
        )

    async def test_action_limit_prevents_more_remediation(self) -> None:
        store = _GuardianStore(
            {
                "guardian_id": "guardian_" + "c" * 32,
                "target_id": "admin",
                "host_id": "h610",
                "service_ref": "nginx.service",
                "mode": "remediate",
                "consecutive_failures": 2,
                "failure_threshold": 3,
                "actions_used": 1,
                "max_actions": 1,
                "authorized_action": {"operation": "service.restart"},
                "actor_id": "admin:kenneth",
                "origin_scope": "admin-console",
            }
        )
        actions: list[dict] = []
        service = GuardianService(
            store,  # type: ignore[arg-type]
            ({"target_id": "admin", "url": "http://admin.test/health"},),
            operation_factory=AsyncMock(side_effect=lambda payload, _owner: actions.append(payload) or {}),
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
            trust_env=False,
        )
        try:
            await service.tick()
        finally:
            await service.close()
        self.assertEqual(actions, [])
        self.assertEqual(len(store.incidents), 1)
        self.assertFalse(store.finished[0]["action_used"])

    async def test_waiting_approval_does_not_consume_action_budget(self) -> None:
        store = _GuardianStore(
            {
                "guardian_id": "guardian_" + "d" * 32,
                "target_id": "admin",
                "host_id": "h610",
                "service_ref": "nginx.service",
                "mode": "remediate",
                "consecutive_failures": 0,
                "failure_threshold": 1,
                "actions_used": 0,
                "max_actions": 1,
                "authorized_action": {"operation": "service.restart"},
                "actor_id": "admin:kenneth",
                "origin_scope": "admin-console",
            }
        )
        service = GuardianService(
            store,  # type: ignore[arg-type]
            ({"target_id": "admin", "url": "http://admin.test/health"},),
            operation_factory=AsyncMock(return_value={
                "operation_id": "op_" + "e" * 32,
                "status": "awaiting_approval",
                "executable": True,
            }),
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
            trust_env=False,
        )
        try:
            await service.tick()
        finally:
            await service.close()
        self.assertFalse(store.finished[0]["action_used"])
        self.assertTrue(store.finished[0]["requires_attention"])


class CheckpointResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_checkpoint_is_returned_without_rerunning_job(self) -> None:
        state = {"result": {"valid": True, "sha256": "abc"}}
        encoded = json.dumps(
            state, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        worker = object.__new__(ClusterWorker)
        for checkpoint_format in ("gaoji-result-v1", "kennethbot-result-v1"):
            result = await worker._execute(
                {
                    "job_id": "job_" + "a" * 32,
                    "fence": 2,
                    "kind": "document.verify",
                    "constraints": {
                        "checkpoint_format": checkpoint_format,
                        "executor_version": "worker-v2",
                    },
                    "resume_checkpoint": {
                        "phase": "completed",
                        "format_version": 1,
                        "executor_version": "worker-v2",
                        "state": state,
                        "state_hash": hashlib.sha256(encoded).hexdigest(),
                    },
                }
            )
            self.assertEqual(result, state["result"])


class _IndexState:
    def __init__(self) -> None:
        self.marked = []

    def changed(self, documents: list) -> list:
        return documents

    def mark(self, documents: list) -> None:
        self.marked.extend(documents)


class _Recall:
    def __init__(self) -> None:
        self.indexed = []

    async def index(self, documents: list) -> int:
        self.indexed.extend(documents)
        return len(documents)

    async def search(self, scope_keys: list[str], _query: str, *, limit: int) -> list[SemanticHit]:
        self.scope_keys = scope_keys
        self.limit = limit
        return [
            SemanticHit(
                scope_key="fleet:runbooks",
                source_type="fleet_runbook_case",
                source_handle="case_" + "a" * 32,
                content="PostgreSQL 复制延迟",
                score=0.91,
                metadata={},
            )
        ]


class RunbookSemanticRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_verified_cases_enter_semantic_index(self) -> None:
        recall = _Recall()
        state = _IndexState()
        cases = [
            {
                "case_id": "case_" + "a" * 32,
                "status": "verified",
                "title": "PostgreSQL 复制延迟",
                "symptoms": "备用库落后",
                "confirmed_cause": "网络抖动",
                "resolution": ["恢复链路"],
            },
            {
                "case_id": "case_" + "b" * 32,
                "status": "draft",
                "title": "未经验证的做法",
                "symptoms": "未知",
                "confirmed_cause": "猜测",
                "resolution": ["重启全部服务"],
            },
        ]
        scores = await semantic_runbook_scores(  # type: ignore[arg-type]
            recall, state, cases, "数据库为什么变慢"
        )
        self.assertEqual(scores, {"case_" + "a" * 32: 0.91})
        self.assertEqual(len(recall.indexed), 1)
        self.assertEqual(recall.indexed[0].source_type, "fleet_runbook_case")


if __name__ == "__main__":
    unittest.main()
