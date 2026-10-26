from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from src import host_control
from src.host_control import REPORT_PREFIX, digest
from src.cluster_control.adapters.ops import OpsClient
from src.cluster_control.host_operations import execution_params
from src.cluster_control.ops_management import OpsManagementService
from tests.test_ops_management import MemoryStore


EXEC_DEFINITION = {"name": "exec.run", "read_only": False, "kind": "job_submission", "idempotency": "required",
    "params_schema": {"type": "object", "properties": {
        "host": {"type": "string"}, "profile": {"type": "string"}, "command": {"type": "object"},
        "cwd": {"type": "string"}, "env": {"type": "object"}, "credential_refs": {"type": "array"},
        "timeout_seconds": {"type": "integer", "minimum": 1}},
        "required": ["host", "profile", "command"], "additionalProperties": False}}
BEFORE_BOOT = "8d526a9e-ee0b-4ae2-ab51-1e5462c3f99d"
AFTER_BOOT = "9d526a9e-ee0b-4ae2-ab51-1e5462c3f99d"


class CheckpointStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.checkpoints = []

    def checkpoint_managed_operation(self, key, **kwargs):
        record = self.records[key]
        if record["status"] != "running" or record["fence"] != kwargs["fence"]:
            raise PermissionError("Cancelled or stale lease")
        record["result"] = copy.deepcopy(kwargs["result"])
        self.checkpoints.append(copy.deepcopy(record))


class HostOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token = Path(self.temp.name) / "token"
        self.token.write_text("test-token-" + "a" * 40)
        self.store = CheckpointStore()
        self.jobs = {}
        self.posts = []
        self.fail_submit = False
        self.fail_read = False
        self.fail_job_read = False
        self.boot = BEFORE_BOOT
        self.facts_host = "h310"
        self.facts_age = 0
        self.definitions = [copy.deepcopy(EXEC_DEFINITION)]
        self.client = OpsClient("http://ops.test", self.token, transport=httpx.MockTransport(self.handle))
        self.manager = OpsManagementService(self.client, self.store, hosts=("h310", "h610", "tank"),
            actors=("qq:3526452465", "admin:kenneth"), host_helpers={"h310": "/run/current-system/sw/bin/gaoji-host-control"})
        self.addAsyncCleanup(self.manager.close)

    def handle(self, request):
        if request.method == "GET":
            return httpx.Response(200, json={"version": 2, "operations": self.definitions})
        payload = json.loads(request.content)
        self.posts.append(payload)
        op, params = payload["op"], payload["params"]
        if op == "exec.run":
            self.assertEqual(len(self.store.checkpoints), 1, "Dispatch must follow the committed checkpoint")
            key = str(uuid.uuid4())
            handle = {"job_id": key, "host": params["host"], "operation": "exec.run", "state": "queued", "revision": 1}
            self.jobs[key] = {"handle": handle, "spec": params, "result": None, "stdout": "", "stderr": ""}
            self.assertEqual(request.headers["Idempotency-Key"], self.store.checkpoints[-1]["operation_id"])
            if self.fail_submit:
                raise httpx.ReadTimeout("Lost response after acceptance", request=request)
            return httpx.Response(200, json=handle)
        if self.fail_read:
            raise httpx.ReadTimeout("Target disconnected", request=request)
        if op == "jobs.list":
            return httpx.Response(200, json={"jobs": list(self.jobs.values()), "next_cursor": None})
        if op == "jobs.status":
            if self.fail_job_read:
                raise httpx.ReadTimeout("Job receipt endpoint unavailable", request=request)
            return httpx.Response(200, json=self.jobs[params["job_id"]])
        if op == "jobs.logs":
            job = self.jobs[params["job_id"]]
            return httpx.Response(200, json={"job_id": params["job_id"], "encoding": "base64",
                "stdout_base64": base64.b64encode(job["stdout"].encode()).decode(),
                "stderr_base64": base64.b64encode(job["stderr"].encode()).decode(), "complete": True, "truncated": False})
        if op == "jobs.cancel":
            self.jobs[params["job_id"]]["handle"]["state"] = "cancelled"
            return httpx.Response(200, json=self.jobs[params["job_id"]])
        if op == "host.facts":
            return httpx.Response(200, json={"host": self.facts_host,
                "observed_at": datetime.fromtimestamp(time.time() - self.facts_age, timezone.utc).isoformat(),
                "facts": {"boot_id": self.boot}})
        raise AssertionError(op)

    async def propose(self, operation="exec.run", **overrides):
        params = {"host": "h310", "profile": "operator", "command": {"argv": ["df", "-h"]}}
        if operation == "host.reboot":
            params = {"host": "h310", "reason": "Authorized maintenance"}
        params.update(overrides)
        result = await self.manager.call(operation, params, actor="qq:3526452465", origin="group:123",
                                         idempotency_key="host-operation-test")
        return result["operation"]

    async def start(self, operation="exec.run", **overrides):
        record = await self.propose(operation, **overrides)
        await self.manager.approve(record["operation_id"], actor="admin:kenneth",
            expected_hash=record["contract_hash"], expected_version=record["resource_version"])
        await self.manager.run_once()
        return self.store.get_operation(record["operation_id"])

    def record(self):
        return self.store.get_operation(next(iter(self.store.records)))

    def submission_count(self):
        return sum(item["op"] == "exec.run" for item in self.posts)

    def report(self, *, state="succeeded", exit_code=0):
        record = self.record()
        evidence = {"protocol": 1, "host": "h310", "boot_id": BEFORE_BOOT,
            "intent_hash": digest(record["arguments"]["host_control"]["intent"])}
        proof = {"ok": True, "evidence": evidence, "fingerprint": digest(evidence)}
        report = {"operation_id": record["operation_id"], "preflight": proof}
        job = next(iter(self.jobs.values()))
        job.update(stdout=REPORT_PREFIX + json.dumps(report) + "\n", result={"exit_code": exit_code})
        job["handle"]["state"] = state
        return proof

    async def test_catalog_and_target_binding_fail_closed(self):
        catalog = await self.manager.catalog("admin:kenneth")
        self.assertIn("host.reboot", [item["name"] for item in catalog["operations"]])
        self.assertEqual(catalog["checked_execution_hosts"], ["h310"])
        with self.assertRaises(PermissionError):
            await self.propose(host="h610")
        with self.assertRaises(PermissionError):
            await self.manager.call("host.reboot", {"host": "h310", "reason": "test"}, actor="qq:other", origin="group:123")
        self.assertFalse(self.posts)

    async def test_approval_then_checked_execution_and_verified_completion(self):
        proposal = await self.propose()
        self.assertFalse(await self.manager.run_once())
        self.assertEqual(self.submission_count(), 0)
        await self.manager.approve(proposal["operation_id"], actor="admin:kenneth",
            expected_hash=proposal["contract_hash"], expected_version=1)
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "running")
        self.report()
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertIn("preflight", self.record()["result"])
        self.assertEqual(self.submission_count(), 1)

    async def test_environment_is_not_injected_before_preflight(self):
        record = await self.start(env={"BASH_ENV": "/danger", "PYTHONPATH": "/untrusted"}, cwd="/tmp",
                                  credential_refs=["credential-one"], timeout_seconds=120)
        params = next(iter(self.jobs.values()))["spec"]
        self.assertNotIn("env", params)
        self.assertEqual(params["cwd"], "/tmp")
        self.assertEqual(params["credential_refs"], ["credential-one"])
        self.assertEqual(params["timeout_seconds"], 120)
        self.assertEqual(params, execution_params(record))
        self.assertIn("BASH_ENV", params["command"]["script"])

    async def test_lost_submission_response_recovers_existing_job_without_resubmit(self):
        self.fail_submit = True
        await self.start()
        self.assertEqual(self.record()["status"], "reconciling")
        self.assertIsNone(self.record().get("backend_operation_id"))
        # A new manager has no process-local state from the original submission.
        self.manager = OpsManagementService(self.client, self.store, hosts=("h310", "h610", "tank"),
            actors=("qq:3526452465", "admin:kenneth"), host_helpers={"h310": "/run/current-system/sw/bin/gaoji-host-control"})
        self.report()
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertEqual(self.submission_count(), 1)

    async def test_crash_after_checkpoint_before_network_never_replays(self):
        proposal = await self.propose()
        raw = self.store.records[proposal["operation_id"]]
        raw.update(status="running", result={"submission_started": True, "submitted_at": int(time.time()), "phase": "submitting"})
        for _ in range(3):
            await self.manager.run_once()
        self.assertEqual(self.submission_count(), 0)
        self.assertEqual(self.record()["status"], "reconciling")
        raw["deadline_at"] = int(time.time()) - 1
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")

    async def test_disconnection_is_not_failure_and_does_not_reboot_again(self):
        await self.start("host.reboot")
        self.report(state="running", exit_code=None)
        await self.manager.run_once()
        self.fail_read = True
        for _ in range(3):
            await self.manager.run_once()
            self.assertEqual(self.record()["status"], "reconciling")
        self.fail_read = False
        self.boot = AFTER_BOOT
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertTrue(self.record()["result"]["verification"]["verified"])
        self.assertEqual(self.submission_count(), 1)

    async def test_exit_zero_does_not_prove_reboot(self):
        await self.start("host.reboot")
        self.report()
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "reconciling")
        self.assertFalse(self.record()["result"]["verification"]["verified"])

    async def test_new_boot_can_be_confirmed_while_job_receipt_endpoint_is_down(self):
        await self.start("host.reboot")
        self.report(state="running", exit_code=None)
        await self.manager.run_once()
        self.boot = AFTER_BOOT
        self.fail_job_read = True
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertEqual(self.record()["result"]["verification"]["after_boot_id"], AFTER_BOOT)
        self.assertIn("已确认重启", self.record()["result"]["summary"])
        self.assertEqual(self.submission_count(), 1)

    async def test_missing_reboot_baseline_cannot_use_current_boot_as_success(self):
        await self.start("host.reboot")
        self.boot = AFTER_BOOT
        self.fail_job_read = True
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "reconciling")
        self.assertNotIn("verification", self.record()["result"])
        self.assertFalse(any(post["op"] == "host.facts" for post in self.posts))
        self.assertEqual(self.submission_count(), 1)

    async def test_reboot_baseline_can_be_recovered_from_original_job_logs(self):
        await self.start("host.reboot")
        self.report(state="running", exit_code=None)
        self.boot, self.fail_job_read = AFTER_BOOT, True
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertIn("preflight", self.record()["result"])
        self.assertEqual(self.submission_count(), 1)

    async def test_job_read_outage_does_not_accept_stale_new_boot(self):
        await self.start("host.reboot")
        self.report(state="running", exit_code=None)
        await self.manager.run_once()
        self.boot, self.facts_age, self.fail_job_read = AFTER_BOOT, 120, True
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "reconciling")
        self.assertFalse(self.record()["result"]["verification"]["verified"])
        self.assertEqual(self.submission_count(), 1)

    async def test_reboot_timeout_replaces_pending_summary(self):
        await self.start("host.reboot")
        self.report(state="running", exit_code=None)
        await self.manager.run_once()
        self.store.records[self.record()["operation_id"]]["deadline_at"] = int(time.time()) - 1
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertIn("验收超时", self.record()["result"]["summary"])
        self.assertEqual(self.submission_count(), 1)

    async def test_stale_or_wrong_host_observation_never_proves_reboot(self):
        await self.start("host.reboot")
        self.report()
        self.boot = AFTER_BOOT
        for host, age in [("tank", 0), ("h310", 120), ("h310", -60)]:
            self.facts_host, self.facts_age = host, age
            await self.manager.run_once()
            self.assertNotEqual(self.record()["status"], "succeeded")

    async def test_explicit_reboot_failure_is_reported(self):
        await self.start("host.reboot")
        self.report(state="running", exit_code=None)
        await self.manager.run_once()
        self.assertIn("尚未确认", self.record()["result"]["summary"])
        self.report(state="failed", exit_code=1)
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(self.record()["error_code"], "reboot_command_failed")
        self.assertIn("退出码 1", self.record()["result"]["summary"])
        self.assertNotIn("尚未确认新的开机编号", self.record()["result"]["summary"])

    async def test_target_check_error_is_preserved_with_suggestion(self):
        await self.start()
        job = next(iter(self.jobs.values()))
        job["handle"]["state"] = "failed"
        job["stderr"] = json.dumps({"ok": False, "code": "program_unavailable", "command_started": False,
                                    "suggested_program": "/run/current-system/sw/bin/systemctl"})
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(self.record()["result"]["preflight_error"]["code"], "program_unavailable")

    async def test_success_without_target_evidence_is_unknown(self):
        await self.start()
        next(iter(self.jobs.values()))["handle"]["state"] = "succeeded"
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")

    async def test_cancellation_before_and_after_dispatch(self):
        proposal = await self.propose()
        self.store.records[proposal["operation_id"]]["status"] = "cancelling"
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "cancelled")
        self.assertEqual(self.submission_count(), 0)
        self.store.records.clear()
        await self.start()
        self.store.records[self.record()["operation_id"]]["status"] = "cancelling"
        await self.manager.run_once()
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "cancelled")
        self.assertEqual(self.submission_count(), 1)

    async def test_scope_change_invalidates_an_approved_command(self):
        proposal = await self.propose()
        await self.manager.approve(proposal["operation_id"], actor="admin:kenneth",
            expected_hash=proposal["contract_hash"], expected_version=1)
        self.manager.host_helpers["h310"] = "/different/helper"
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertEqual(self.submission_count(), 0)

    async def test_rejected_checkpoint_does_not_claim_a_command_was_sent(self):
        proposal = await self.propose()
        await self.manager.approve(proposal["operation_id"], actor="admin:kenneth",
            expected_hash=proposal["contract_hash"], expected_version=1)
        with patch.object(self.store, "checkpoint_managed_operation", side_effect=PermissionError("Approval expired")):
            await self.manager.run_once()
        self.assertEqual(self.submission_count(), 0)
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertFalse(self.record()["result"].get("submission_started"))
        self.assertFalse(self.record()["result"]["command_started"])
        self.assertEqual(self.record()["result"]["phase"], "submission_rejected")

    async def test_reboot_cannot_accept_arbitrary_commands(self):
        with self.assertRaises(ValueError):
            await self.propose("host.reboot", command={"argv": ["rm", "-rf", "/"]})
        self.assertFalse(self.store.records)

    async def test_request_size_is_checked_before_approval(self):
        with self.assertRaises(ValueError):
            await self.propose(command={"script": "'" * 64000})
        self.assertFalse(self.store.records)

    async def test_deadline_requests_cancellation_for_running_command(self):
        await self.start()
        self.report(state="running")
        raw = self.store.records[self.record()["operation_id"]]
        raw["deadline_at"] = int(time.time()) - 1
        await self.manager.run_once()
        self.assertTrue(any(post["op"] == "jobs.cancel" for post in self.posts))
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "cancelled")

    async def test_expiry_before_submission_is_not_reported_as_unknown(self):
        proposal = await self.propose("host.reboot")
        await self.manager.approve(proposal["operation_id"], actor="admin:kenneth",
            expected_hash=proposal["contract_hash"], expected_version=1)
        self.store.records[proposal["operation_id"]]["deadline_at"] = int(time.time()) - 1
        await self.manager.run_once()
        self.assertEqual(self.submission_count(), 0)
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(self.record()["error_code"], "submission_deadline")
        self.assertFalse(self.record()["result"]["command_started"])

    async def test_other_job_with_different_arguments_is_never_adopted(self):
        self.fail_submit = True
        await self.start()
        job = next(iter(self.jobs.values()))
        job["spec"]["command"] = {"argv": ["true"]}
        await self.manager.run_once()
        self.assertIsNone(self.record().get("backend_operation_id"))
        self.assertEqual(self.record()["status"], "reconciling")

    def install_isolated_helper(self):
        root = Path(self.temp.name).resolve()
        boot = root / "boot-id"
        boot.write_text(BEFORE_BOOT)
        reboot = root / "fake-systemctl"
        reboot.write_text(f"#!/bin/sh\nprintf '%s' {shlex.quote(AFTER_BOOT)} > {shlex.quote(str(boot))}\n")
        reboot.chmod(0o700)
        config = root / "host.json"
        config.write_text(json.dumps({"host_id": "h310", "path": "/usr/bin:/bin",
            "default_cwd": str(root), "boot_id_file": str(boot),
            "receipt_directory": str(root / "receipts"), "shell": shutil.which("bash"),
            "systemctl": str(reboot)}))
        helper = root / "host-helper"
        helper.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", host_control.__file__,
                          "--config", str(config), "--request-json"]) + ' "$1"\n')
        helper.chmod(0o700)
        self.manager.host_helpers["h310"] = str(helper)
        return root, boot

    def execute_isolated_job(self):
        job = next(iter(self.jobs.values()))
        completed = subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-c", job["spec"]["command"]["script"]],
            env={"PATH": "/usr/bin:/bin", "HOME": self.temp.name}, capture_output=True, text=True, timeout=20)
        job.update(stdout=completed.stdout, stderr=completed.stderr, result={"exit_code": completed.returncode})
        job["handle"]["state"] = "succeeded" if completed.returncode == 0 else "failed"
        return completed

    async def test_real_helper_missing_path_reaches_control_without_execution(self):
        self.install_isolated_helper()
        await self.start(command={"argv": ["/not-present/echo", "hello"]})
        result = self.execute_isolated_job()
        self.assertNotEqual(result.returncode, 0)
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(self.record()["result"]["preflight_error"]["code"], "program_unavailable")
        self.assertFalse(self.record()["result"]["command_started"])

    async def test_real_helper_reboot_evidence_survives_lost_submission_receipt(self):
        root, boot = self.install_isolated_helper()
        self.fail_submit = True
        await self.start("host.reboot")
        result = self.execute_isolated_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.boot = boot.read_text()
        await self.manager.run_once()
        record = self.record()
        self.assertEqual(record["status"], "succeeded")
        self.assertTrue(record["result"]["verification"]["verified"])
        receipt = json.loads((root / "receipts" / (record["operation_id"] + ".json")).read_text())
        self.assertEqual(receipt["preflight"], record["result"]["preflight"])
        self.assertEqual(self.submission_count(), 1)

    async def test_helper_scope_change_does_not_invalidate_typed_service_binding(self):
        definition = {**EXEC_DEFINITION, "name": "units.restart"}
        before = self.manager.binding_hash(definition)
        self.manager.host_helpers["tank"] = "/different/helper"
        self.assertEqual(before, self.manager.binding_hash(definition))
        self.assertEqual(before, self.manager.binding_hash(definition, legacy=True))

    async def test_accepted_legacy_exec_can_be_observed_but_not_resubmitted(self):
        await self.start()
        record = self.record()
        raw = self.store.records[record["operation_id"]]
        del raw["arguments"]["host_control"]
        raw["arguments"]["binding_hash"] = self.manager.binding_hash(EXEC_DEFINITION, legacy=True)
        await self.manager.validate_binding(raw)
        raw["backend_operation_id"] = None
        with self.assertRaises(PermissionError):
            await self.manager.validate_binding(raw)


if __name__ == "__main__":
    unittest.main()
