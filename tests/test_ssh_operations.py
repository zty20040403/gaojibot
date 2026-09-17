from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from src import ssh_operations as target
from src.host_control import CheckError
from src.ssh_ops_protocol import PROTOCOL, handle
from src.cluster_control.adapters.ops import OpsError
from src.cluster_control.adapters.ssh import SSHOperationsClient, parse_targets
from src.cluster_control.ops_management import OpsManagementService
from tests.test_ops_management import MemoryStore


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gaoji-ssh-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        boot = root / "boot-id"
        boot.write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.config = {"host_id": "h610", "jobs_directory": str(root / "jobs"), "boot_id_file": str(boot),
            "systemd_run": "/test/systemd-run", "systemctl": "/test/systemctl", "busctl": "/test/busctl",
            "entry": "/test/helper", "shell": "/bin/bash", "path": "/usr/bin:/bin"}
        self.op = "op_" + "a" * 32
        self.params = {"host": "h610", "profile": "operator", "command": {"argv": ["/usr/bin/true"]}}

    def test_target_queries_bound_untrusted_output(self):
        with self.assertRaisesRegex(CheckError, "output limit"):
            target.command({**self.config, "test": sys.executable}, "test", ["-c", "print('x' * (3 * 1024 * 1024))"])

    def test_failed_units_keep_count_and_structured_evidence(self):
        with patch.object(target, "command", return_value="example.service loaded failed failed Example\n"):
            result = target.observation(self.config, "fleet.overview", {})
        self.assertEqual(result["agent"]["failed_units"], 1)
        self.assertEqual(result["units"][0]["unit"], "example.service")
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["resource_observation"]["source"], "ssh")
        self.assertNotIn("exporter", result)
        self.assertGreater(result["pressure"]["filesystem_available_bytes"]["samples"][0]["value"], 0)

    def test_metrics_expose_actual_cpu_window_and_memory_samples(self):
        with patch.object(Path, "read_text", return_value="MemTotal: 100 kB\nMemAvailable: 25 kB\n"), \
             patch.object(target, "cpu_counters", side_effect=[{"0": (100, 20)}, {"0": (120, 25)}]), \
             patch.object(target.time, "sleep"), patch.object(target.time, "monotonic", side_effect=[1, 1.2]):
            result = target.observation(self.config, "host.metrics", {})
        metrics = result["observation"]["metrics"]
        self.assertEqual(result["observation"]["source"], "ssh")
        self.assertEqual(metrics["memory_total_bytes"]["samples"][0]["value"], 102400)
        self.assertEqual(metrics["memory_available_bytes"]["samples"][0]["value"], 25600)
        self.assertAlmostEqual(metrics["cpu_idle_seconds_per_second"]["window_seconds"], 0.2)
        self.assertEqual(metrics["cpu_idle_seconds_per_second"]["samples"][0]["value"], 0.25)

    def test_cpu_counter_excludes_guest_time_and_aggregate_row(self):
        with patch.object(Path, "read_text", return_value="cpu 100 1 2 3 4 5 6 7 8 9\ncpu0 100 1 2 3 4 5 6 7 8 9\nintr 8\n"):
            self.assertEqual(target.cpu_counters(), {"0": (128, 3)})

    def test_submission_is_durable_and_idempotent(self):
        def dispatch(config, program, args, **kwargs):
            self.assertEqual(program, "systemd_run")
            self.assertEqual(target.JobStore(config).read(self.op)["handle"]["state"], "dispatching")
            return ""
        with patch.object(target, "command", side_effect=dispatch) as run:
            first = target.submit(self.config, "exec.run", self.params, self.op)
            second = target.submit(self.config, "exec.run", self.params, self.op)
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)
        with self.assertRaisesRegex(CheckError, "different command"):
            target.submit(self.config, "exec.run", {**self.params, "cwd": "/"}, self.op)

    def test_lost_dispatch_ack_never_resubmits(self):
        with patch.object(target, "command", side_effect=subprocess.TimeoutExpired("systemd-run", 20)) as run:
            first = target.submit(self.config, "exec.run", self.params, self.op)
            self.assertEqual(target.submit(self.config, "exec.run", self.params, self.op), first)
        self.assertEqual(run.call_count, 1)
        stored = target.JobStore(self.config).read(self.op)
        self.assertIn("dispatch_error", stored)

    def test_worker_runs_once_and_preserves_stdout(self):
        params = {**self.params, "command": {"argv": [sys.executable, "-c", "print('SSH evidence')"]}}
        with patch.object(target, "command", return_value=""):
            target.submit(self.config, "exec.run", params, self.op)
        target.run_job(self.config, self.op)
        stored = target.JobStore(self.config).read(self.op)
        self.assertEqual(stored["handle"]["state"], "succeeded")
        self.assertEqual(stored["result"]["exit_code"], 0)
        self.assertIn("SSH evidence", (target.JobStore(self.config).directory(self.op) / "stdout").read_text())
        with self.assertRaisesRegex(CheckError, "Never restart"):
            target.run_job(self.config, self.op)

    def test_log_limits_are_reported_in_receipts_and_reads(self):
        params = {**self.params, "command": {"argv": [sys.executable, "-c", "print('x' * 100)"]}}
        with patch.object(target, "command", return_value=""):
            target.submit(self.config, "exec.run", params, self.op)
        with patch.object(target, "LOG_LIMIT", 64):
            target.run_job(self.config, self.op)
        stored = target.JobStore(self.config).read(self.op)
        self.assertEqual(stored["result"]["logs"]["stdout"],
            {"observed_bytes": 101, "stored_bytes": 64, "truncated": True})
        request = {"protocol": PROTOCOL, "host": "h610", "op": "jobs.logs",
            "params": {"job_id": handle("h610", self.op), "limit": 65536}}
        logs = target.dispatch(self.config, request)
        self.assertTrue(logs["stdout_truncated"])
        self.assertTrue(logs["stdout_capture_finished"])
        self.assertFalse(logs["stderr_truncated"])
        stored["result"]["logs"]["stdout"]["truncated"] = False
        target.JobStore(self.config).write(self.op, stored)
        request["params"]["limit"] = 8
        self.assertTrue(target.dispatch(self.config, request)["stdout_truncated"])

    def test_command_timeout_still_applies_after_output_streams_close(self):
        directory = Path(self.tmp.name) / "closed-output"
        directory.mkdir()
        params = {"timeout_seconds": 0.2, "command": {"argv": [sys.executable, "-c",
            "import os,time; os.close(1); os.close(2); time.sleep(10)"]}}
        started = time.monotonic()
        code, timed_out, _ = target.execute_command(self.config, params, directory)
        self.assertTrue(timed_out)
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 3)

    def test_native_job_runs_existing_host_checks_without_legacy_job_directory(self):
        root = Path(self.tmp.name)
        cgroup = root / "cgroup"
        cgroup.write_text(f"0::/system.slice/gaoji-ssh-{self.op}.service\n")
        config = root / "host-control.json"
        config.write_text(json.dumps({**self.config, "default_cwd": str(root),
            "receipt_directory": str(root / "host-receipts"), "cgroup_file": str(cgroup),
            "job_state_root": str(root / "retired-jobs")}))
        intent = {"host": "h610", "action": "exec", "command": {"argv": [sys.executable, "-c", "print('checked native command')"]}}
        params = {**self.params, "command": {"argv": [sys.executable, "-I",
            str(Path(target.__file__).with_name("host_control.py")), "--config", str(config),
            "--request-json", json.dumps({"phase": "run", "intent": intent, "operation_id": self.op})]}}
        with patch.object(target, "command", return_value=""):
            target.submit(self.config, "exec.run", params, self.op)
        target.run_job(self.config, self.op)
        stored = target.JobStore(self.config).read(self.op)
        self.assertEqual(stored["handle"]["state"], "succeeded")
        stdout = (target.JobStore(self.config).directory(self.op) / "stdout").read_text()
        from src.cluster_control.host_operations import preflight_from_logs
        proof = preflight_from_logs(stdout, {"operation_id": self.op, "arguments": {"host_control": {"intent": intent}}})
        self.assertTrue(proof["ok"])
        self.assertIn("checked native command", stdout)
        self.assertTrue((root / "host-receipts" / (self.op + ".json")).is_file())
        self.assertFalse((root / "retired-jobs").exists())

    def test_reboot_or_dead_executor_is_unknown_not_success(self):
        with patch.object(target, "command", return_value=""):
            target.submit(self.config, "exec.run", self.params, self.op)
        store = target.JobStore(self.config)
        value = store.read(self.op)
        value["created_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        store.write(self.op, value)
        with patch.object(target, "unit_status", return_value={"active_state": "inactive"}):
            result = target.status(self.config, self.op)
        self.assertEqual(result["handle"]["state"], "outcome_unknown")
        with patch.object(target, "command", side_effect=AssertionError("No retry")):
            self.assertEqual(target.submit(self.config, "exec.run", self.params, self.op)["state"], "outcome_unknown")

    def test_host_path_and_operation_injection_rejected(self):
        for key in ("../x", "op_x", "op_" + "a" * 32 + "/x"):
            with self.assertRaises(CheckError):
                target.submit(self.config, "exec.run", self.params, key)
        with self.assertRaises(CheckError):
            target.submit(self.config, "exec.run", {**self.params, "host": "tank"}, self.op)
        with self.assertRaises(CheckError):
            target.dispatch(self.config, {"host": "tank", "protocol": PROTOCOL, "op": "host.facts", "params": {}})
        with self.assertRaises(CheckError):
            target.submit(self.config, "units.restart", {"host": "h610", "unit": "a.service; reboot"}, self.op)

    def test_service_receipt_requires_baseline_and_successful_systemctl(self):
        before = {"active_state": "active", "sub_state": "running", "details": {"invocation_id": "a" * 32}}
        after = {**before, "details": {"invocation_id": "b" * 32}}
        params = {"host": "h610", "unit": "example.service", "expected_invocation_id": "a" * 32}
        with patch.object(target, "unit_status", side_effect=[before, after]), \
             patch.object(target, "command", return_value="") as execute:
            receipt = target.service_action(self.config, "units.restart", params)
        execute.assert_called_once_with(self.config, "systemctl", ["restart", "example.service"], timeout=90)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["before"]["invocation_id"], "a" * 32)
        self.assertEqual(receipt["after"]["invocation_id"], "b" * 32)
        from src.cluster_control.service_verification import receipt as verify_receipt
        record = {"backend_ref": "ssh-management-v1", "backend_operation_id": handle("h610", self.op),
            "created_at": 1, "arguments": {"op": "units.restart", "params": params}}
        job = {"handle": {"job_id": handle("h610", self.op), "host": "h610",
            "operation": "units.restart", "state": "succeeded"}, "spec": params, "updated_at": target.now(), "result": receipt}
        self.assertFalse(verify_receipt(record, job, time.time())["verified"])
        for change in ({"exit_code": 1}, {"argv": ["/test/systemctl", "restart", "other.service"]},
                       {"attribution": "systemd_job_accepted_and_target_observed"}):
            with self.assertRaises(ValueError):
                verify_receipt(record, {**job, "result": {**receipt, **change}}, time.time())
        with patch.object(target, "unit_status", return_value=after), patch.object(target, "command") as run:
            with self.assertRaisesRegex(CheckError, "changed"):
                target.service_action(self.config, "units.restart", params)
            run.assert_not_called()


class SSHClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gaoji-ssh-client-test-")
        self.addCleanup(self.tmp.cleanup)
        self.pins = Path(self.tmp.name) / "known_hosts"
        self.pins.write_text("example pinned host key")
        self.targets = {"h610": {"destination": "gaoji-operator@h610.test", "helper": "/run/current-system/sw/bin/gaoji-ssh-operations"}}
        self.client = SSHOperationsClient(self.targets, known_hosts_file=str(self.pins), writable=True)

    def executable(self, body):
        path = Path(self.tmp.name) / "fake-ssh"
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        self.client.ssh_binary = str(path)

    async def test_subprocess_receipt_and_wrong_host_rejection(self):
        self.executable('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"protocol":r["protocol"],"host":r["host"],"ok":True,"data":{"host":r["host"]}}))\n')
        value = await self.client.call("host.facts", {"host": "h610"})
        self.assertEqual(value.data["host"], "h610")
        self.executable('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"protocol":r["protocol"],"host":"wrong","ok":True,"data":{}}))\n')
        with self.assertRaisesRegex(OpsError, "different target"):
            await self.client.call("host.facts", {"host": "h610"})

    async def test_subprocess_timeout_and_output_flood_are_bounded(self):
        self.executable('import sys,time\nsys.stdin.read()\ntime.sleep(10)\n')
        self.client.timeout_seconds = 0.2
        with self.assertRaises(OpsError) as failure:
            await self.client.call("host.facts", {"host": "h610"})
        self.assertEqual(failure.exception.code, "timeout")
        self.executable('import sys\nsys.stdin.read()\nsys.stdout.write("x" * (3 * 1024 * 1024))\n')
        self.client.timeout_seconds = 5
        with self.assertRaises(OpsError) as failure:
            await self.client.call("host.facts", {"host": "h610"})
        self.assertEqual(failure.exception.code, "response_too_large")

    async def test_disconnect_during_request_is_an_uncertain_transport_failure(self):
        self.executable('import os\nos.close(0)\n')
        with self.assertRaises(OpsError) as failure:
            await self.client._remote("h610", {"op": "exec.run", "params": {
                "host": "h610", "command": {"script": "#" * 200000}}})
        self.assertEqual(failure.exception.code, "transport_unavailable")
        self.assertIn("may still be running", str(failure.exception))

    async def test_success_envelope_without_receipt_is_invalid(self):
        self.executable('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"protocol":r["protocol"],"host":r["host"],"ok":True}))\n')
        with self.assertRaises(OpsError) as failure:
            await self.client.call("host.facts", {"host": "h610"})
        self.assertEqual(failure.exception.code, "invalid_response")

    async def test_fixed_identity_and_no_credentials_in_remote_command(self):
        command = self.client.command("h610")
        for item in ("-oStrictHostKeyChecking=yes", "-oForwardAgent=no", "-oIdentityAgent=none", "-oIdentityFile=none"):
            self.assertIn(item, command)
        with self.assertRaises(OpsError):
            self.client.command("h310")
        for destination in ("-oProxyCommand=evil", "root@host;evil", "root@$(whoami)"):
            with self.assertRaises(ValueError):
                parse_targets(json.dumps({"h610": {"destination": destination, "helper": "/helper"}}))
        client = SSHOperationsClient({"h610": {**self.targets["h610"], "port": 2224}}, known_hosts_file=str(self.pins))
        argv = client.command("h610")
        self.assertEqual(argv[argv.index("-p") + 1], "2224")
        for port in (0, 65536, True, "22 -oProxyCommand=evil"):
            with self.assertRaises(ValueError):
                parse_targets(json.dumps({"h610": {**self.targets["h610"], "port": port}}))

    async def test_readonly_client_cannot_mutate(self):
        readonly = SSHOperationsClient(self.targets, known_hosts_file=str(self.pins))
        with patch.object(readonly, "_remote", new_callable=AsyncMock) as remote:
            with self.assertRaises(OpsError):
                await readonly.call("units.restart", {"host": "h610", "unit": "example.service"})
            remote.assert_not_called()

    async def test_unknown_job_and_invalid_schema_never_reach_ssh(self):
        with patch.object(self.client, "_remote", new_callable=AsyncMock) as remote:
            with self.assertRaises(OpsError):
                await self.client.call("jobs.status", {"job_id": "old-maxops-job"})
            with self.assertRaises(OpsError):
                await self.client.call("units.restart", {"host": "h610", "unit": "example.service", "script": "reboot"})
            remote.assert_not_called()

    async def test_fleet_failure_is_unknown_not_healthy(self):
        with patch.object(self.client, "_remote", side_effect=OpsError("timeout", "offline", retryable=True)):
            result = await self.client.execute("fleet.overview", {})
        self.assertTrue(result.data["partial"])
        self.assertEqual(result.data["hosts"][0]["status"], "unknown")

    async def test_old_approval_cannot_cross_backend(self):
        manager = OpsManagementService(self.client, MemoryStore(), hosts=("h610",), actors=("admin:test",))
        with self.assertRaisesRegex(PermissionError, "Legacy"):
            await manager.validate_binding({"actor_id": "admin:test", "backend_ref": "ops-management-v2"})

    async def test_ssh_approval_binding_and_uncertain_submission_recovery(self):
        store = MemoryStore()
        def checkpoint(key, **kwargs):
            store.records[key]["result"] = copy.deepcopy(kwargs["result"])
        store.checkpoint_managed_operation = checkpoint
        manager = OpsManagementService(self.client, store, hosts=("h610",), actors=("admin:test",))
        proposal = await manager.call("units.restart", {"host": "h610", "unit": "example.service"},
            actor="admin:test", origin="group:test", idempotency_key="same-intent")
        record = proposal["operation"]
        self.assertEqual(record["operation"], "ssh.execute")
        self.assertEqual(record["status"], "awaiting_approval")
        await manager.approve(record["operation_id"], actor="admin:test", expected_hash=record["contract_hash"], expected_version=1)
        with patch.object(self.client, "_remote", side_effect=OpsError("timeout", "lost ack", retryable=True)) as remote:
            await manager.run_once()
            self.assertEqual(store.records[record["operation_id"]]["status"], "reconciling")
            await manager.run_once()
        self.assertEqual([call.args[1]["op"] for call in remote.call_args_list], ["units.restart", "jobs.status"])
        self.assertEqual(remote.call_args_list[-1].args[1]["params"]["job_id"], handle("h610", record["operation_id"]))
        self.pins.write_text("changed host key")
        with self.assertRaisesRegex(PermissionError, "changed"):
            await manager.validate_binding(record)


@unittest.skipUnless(os.getenv("TEST_OPS_POSTGRES_DSN"), "isolated PostgreSQL test not configured")
class SSHPostgresTests(unittest.TestCase):
    def test_migration_operation_claim_and_backend_isolation(self):
        from alembic import command
        from alembic.config import Config
        from src.bot_storage import PostgresDatabase
        from src.bot_storage.schema import HEAD_REVISION
        from src.cluster_control.execution_storage import ClusterExecutionStore
        from src.cluster_control.storage import FleetProjectionStore
        import psycopg
        import uuid
        schema = "gaoji_ssh_test_" + uuid.uuid4().hex[:12]
        dsn = os.environ["TEST_OPS_POSTGRES_DSN"]
        database = None
        with tempfile.TemporaryDirectory(prefix="gaoji-ssh-pg-") as root:
            try:
                with patch.dict(os.environ, AI_POSTGRES_DSN=dsn, AI_POSTGRES_SCHEMA=schema):
                    command.upgrade(Config("alembic.ini"), "head")
                database = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=3)
                database.require_revision(HEAD_REVISION)
                store = ClusterExecutionStore(database, Path(root))
                pins = Path(root) / "pins"
                pins.write_text("test pinned identity")
                backend = SSHOperationsClient({"h610": {"destination": "gaoji-operator@test.internal", "helper": "/test/helper"}},
                    known_hosts_file=str(pins), writable=True)
                manager = OpsManagementService(backend, store, hosts=("h610",), actors=("admin:test",))
                proposal = asyncio.run(manager.call("units.restart", {"host": "h610", "unit": "example.service"},
                    actor="admin:test", origin="group:test", idempotency_key="pg-native-ssh"))["operation"]
                self.assertEqual(proposal["operation"], "ssh.execute")
                self.assertIsNone(store.claim_managed_operation("test-worker"))
                asyncio.run(manager.approve(proposal["operation_id"], actor="admin:test",
                    expected_hash=proposal["contract_hash"], expected_version=proposal["resource_version"]))
                claimed = store.claim_managed_operation("test-worker")
                self.assertEqual(claimed["operation_id"], proposal["operation_id"])
                self.assertIsNone(store.claim_managed_operation("other-worker"))
                old, native = FleetProjectionStore(database), FleetProjectionStore(database, backend_name="ssh")
                args = dict(operation="host.facts", params={"host": "h610"}, target_key="h610", status="fresh",
                    payload={"host": "h610"}, observed_at=1, received_at=2, expires_at=3, duration_ms=1)
                old.record_observation(**args)
                self.assertIsNone(native.latest(operation="host.facts", params={"host": "h610"}))
                native.record_observation(**args)
                self.assertIsNotNone(native.latest(operation="host.facts", params={"host": "h610"}))
                native.record_backend(state="unknown", catalog_version=2, operations=["host.facts"], checked_at=1, last_success_at=None)
                self.assertEqual(native.backend_snapshot()["backend_name"], "ssh")
            finally:
                if database:
                    database.close()
                with psycopg.connect(dsn, autocommit=True) as connection:
                    connection.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()
