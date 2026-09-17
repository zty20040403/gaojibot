from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.cluster_control.execution_contracts import content_hash
from src.cluster_control.guardian import guardian_target_snapshot
from src.cluster_control.guardian_ops import GuardianOpsBridge, service_operation
from src.cluster_control.ops_management import OpsManagementService


HOSTS = ("h310", "h610", "tank")
TARGETS = {f"{host}-worker": {"target_id": f"{host}-worker", "kind": "service",
    "host_id": host, "observer_host": "h610", "service_ref": "gaoji-cluster-worker.service",
    "url": f"http://{host}.test/health"} for host in HOSTS}
INVENTORY = tuple({"host_id": host, "operate": True,
    "operable_units": ["gaoji-cluster-worker.service"]} for host in HOSTS)
DEFINITIONS = [{"name": f"units.{action}", "read_only": False, "kind": "job_submission",
    "idempotency": "required", "params_schema": {"type": "object",
    "required": ["host", "unit"], "properties": {"host": {"type": "string"}, "unit": {"type": "string"}},
    "additionalProperties": False}} for action in ["start", "restart"]]


def policy(host="h610", max_actions=2):
    target = TARGETS[f"{host}-worker"]
    return {"target_id": target["target_id"], "mode": "remediate",
        "expires_at": int(time.time()) + 600, "max_actions": max_actions,
        "interval_seconds": 15, "failure_threshold": 1, "confirm_remediation": True,
        "expected_target_hash": content_hash(guardian_target_snapshot(target)),
        "authorized_action": {"host_id": host, "resource_ref": target["service_ref"], "operation": "service.restart"}}


def manager(store):
    client = SimpleNamespace(base_url="http://ops.test", _credential=lambda: b"test",
        _request=AsyncMock(return_value=SimpleNamespace(data={"job_id": "test-job", "state": "queued"})))
    client.backend_name = "ops"
    client.authorization_binding = lambda: {"url": client.base_url, "identity": client._credential().hex()}
    client.call = client._request
    result = OpsManagementService(client, store, hosts=HOSTS, actors=("admin:kenneth", "qq:3526452465"))
    result.definitions = AsyncMock(return_value=copy.deepcopy(DEFINITIONS))
    return result


class GuardianOpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Mock()
        self.store.host_under_maintenance.return_value = False
        self.management = manager(Mock())
        self.bridge = GuardianOpsBridge(self.management, self.store, TARGETS, INVENTORY)

    async def test_creation_requires_explicit_current_target_review(self):
        for fields, actor in [({"confirm_remediation": False}, "admin:kenneth"),
                ({}, "qq:3526452465"), ({}, "admin:other"),
                ({"expected_target_hash": "old"}, "admin:kenneth")]:
            with self.subTest(actor=actor, fields=fields), self.assertRaises(PermissionError):
                await self.bridge.create({**policy(), **fields}, actor=actor, origin="admin-console")
        self.store.create_guardian.assert_not_called()

    async def test_all_three_hosts_bind_exact_service_and_catalog(self):
        for host in HOSTS:
            await self.bridge.create(policy(host), actor="admin:kenneth", origin="admin-console")
            raw = self.store.create_guardian.call_args.args[0]
            self.assertEqual(raw["authorized_action"]["host_id"], host)
            self.assertEqual(len(raw["probe_policy"]["ops_binding"]), 64)
            self.assertNotIn("confirm_remediation", raw)

    async def test_repair_does_not_silently_drop_conditions_or_stop_service(self):
        action = policy()["authorized_action"]
        for changes in [{"operation": "service.stop"}, {"verification": {"custom": True}},
                {"arguments": {"shell": "anything"}}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                service_operation({**action, **changes})

    async def test_dispatch_rechecks_cancellation_and_binding(self):
        raw = policy()
        target = TARGETS[raw["target_id"]]
        guardian = {**raw, "status": "active", "starts_at": int(time.time()) - 1,
            "actor_id": "admin:kenneth", "actions_used": 1, "host_id": target["host_id"],
            "probe_policy": {"registered_target": guardian_target_snapshot(target),
                "ops_binding": await self.bridge._binding(raw["authorized_action"], "admin:kenneth")}}
        guardian["probe_policy"]["authority_hash"] = content_hash({
            "target": guardian_target_snapshot(target), "action": raw["authorized_action"],
            "expires_at": raw["expires_at"], "max_actions": raw["max_actions"],
        })
        operation = {"arguments": {"guardian_id": "guardian_test"}, "approval_ref": "approval_test"}
        self.store.guardian.return_value = guardian
        await self.bridge.validate_dispatch(operation)
        self.store.host_under_maintenance.return_value = True
        with self.assertRaises(PermissionError):
            await self.bridge.validate_dispatch(operation)
        self.store.host_under_maintenance.return_value = False
        guardian["status"] = "cancelled"
        with self.assertRaises(PermissionError):
            await self.bridge.validate_dispatch(operation)
        guardian["status"] = "active"
        self.management.client._credential = lambda: b"rotated"
        with self.assertRaises(PermissionError):
            await self.bridge.validate_dispatch(operation)

    async def test_manual_approval_cannot_bypass_guardian_budget(self):
        self.store.guardian.return_value = None
        self.management.store.get_operation.return_value = {
            "operation": "maxops.execute", "arguments": {"guardian_id": "guardian_test"}}
        with self.assertRaises(PermissionError):
            await self.management.approve("op_test", actor="admin:kenneth", expected_hash="a"*64, expected_version=1)
        self.management.store.approve_operation.assert_not_called()


@unittest.skipUnless(os.getenv("TEST_OPS_POSTGRES_DSN"), "isolated PostgreSQL test not configured")
class GuardianOpsPostgresTests(unittest.TestCase):
    def test_atomic_budget_approval_scope_and_cancel_on_three_hosts(self):
        from alembic import command
        from alembic.config import Config
        import psycopg
        from psycopg import sql
        from src.bot_storage.database import PostgresDatabase
        from src.cluster_control.execution_storage import ClusterExecutionStore
        from src.cluster_control.reliability import ReliabilityStore
        dsn = os.environ["TEST_OPS_POSTGRES_DSN"]
        schema = "gaoji_guardian_test_" + uuid.uuid4().hex[:12]
        db = None
        with tempfile.TemporaryDirectory() as directory:
            try:
                with patch.dict(os.environ, AI_POSTGRES_DSN=dsn, AI_POSTGRES_SCHEMA=schema):
                    command.upgrade(Config("alembic.ini"), "head")
                db = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=5)
                operations = ClusterExecutionStore(db, Path(directory))
                reliability = ReliabilityStore(db)
                management = manager(operations)
                bridge = GuardianOpsBridge(management, reliability, TARGETS, INVENTORY)
                async def exercise():
                    for host in HOSTS:
                        created = await bridge.create(policy(host), actor="admin:kenneth", origin="test")
                        claimed = reliability.claim_due_guardians(owner="test-owner", limit=1)[0]
                        self.assertEqual(claimed["guardian_id"], created["guardian_id"])
                        with self.assertRaises(PermissionError):
                            await bridge.submit(claimed, "stale-owner")
                        await bridge.submit(claimed, "test-owner")
                        self.assertEqual(reliability.guardian(created["guardian_id"])["actions_used"], 0)
                        prepared = operations.find_operation("admin:kenneth",
                            f"guardian:{created['guardian_id']}", f"guardian:{created['guardian_id']}:1")
                        self.assertEqual(prepared["status"], "awaiting_approval")
                        self.assertIsNone(operations.claim_managed_operation("executor"))
                        with patch.object(operations, "_event", side_effect=RuntimeError("simulate commit interruption")):
                            with self.assertRaises(RuntimeError):
                                await management.approve(prepared["operation_id"], actor="admin:kenneth",
                                    expected_hash=prepared["contract_hash"], expected_version=prepared["resource_version"])
                        self.assertEqual(reliability.guardian(created["guardian_id"])["actions_used"], 0)
                        self.assertEqual(operations.get_operation(prepared["operation_id"])["status"], "awaiting_approval")
                        result = await bridge.submit(claimed, "test-owner")
                        self.assertEqual(result["status"], "awaiting_approval")
                        self.assertEqual(reliability.guardian(created["guardian_id"])["actions_used"], 0)
                        result = await management.approve(prepared["operation_id"], actor="admin:kenneth",
                            expected_hash=prepared["contract_hash"], expected_version=prepared["resource_version"])
                        self.assertEqual(result["status"], "queued")
                        self.assertEqual(reliability.guardian(created["guardian_id"])["actions_used"], 1)
                        duplicate = await bridge.submit(claimed, "test-owner")
                        self.assertEqual(duplicate["operation_id"], result["operation_id"])
                        self.assertEqual(reliability.guardian(created["guardian_id"])["actions_used"], 1)
                        current = reliability.guardian(created["guardian_id"])
                        pending = await bridge.submit(current, "test-owner")
                        self.assertEqual(pending["operation_id"], result["operation_id"])
                        self.assertIsNone(operations.find_operation("admin:kenneth",
                            f"guardian:{created['guardian_id']}", f"guardian:{created['guardian_id']}:2"))
                        reliability.set_guardian_status(created["guardian_id"], actor_id="admin:kenneth",
                            status="cancelled", expected_version=current["resource_version"])
                        await management.run_once()
                        self.assertEqual(operations.get_operation(result["operation_id"])["status"], "needs_attention")
                    management.client._request.assert_not_called()
                asyncio.run(exercise())
            finally:
                if db:
                    db.close()
                with psycopg.connect(dsn) as connection:
                    connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()
