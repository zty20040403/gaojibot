from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.test_subagent_v2 import decision
from src.plugins.ai_chat.agent.evidence import MAX_EVIDENCE_BYTES, read_evidence
from src.plugins.ai_chat.agent.execution import EntryDecision
from src.plugins.ai_chat.agent.outcomes import (
    acceptance_blocks_completion, evaluate_acceptance, normalize_checks,
    outcome_report, validate_report,
)
from src.plugins.ai_chat.subagents import SubAgentStore, TaskStep
from src.plugins.ai_chat.agent.progress import task_progress


def review(refs, index=0):
    return {"metadata": {"criterion_reviews": [{"criterion_index": index, "status": "passed",
        "reason": "已读取实际工具证据并核对目标", "evidence_refs": refs}]}}


def contract(kind="evidence", **params):
    return {"version": 2, "acceptance": ["验证目标"],
        "outcome_checks": [{"criterion_index": 0, "kind": kind, **params}]}


def observation(at, free=200, *, host="h610"):
    return {"ok": True, "status": "fresh", "hosts": [{"host_id": host, "status": "online",
        "exporter_sample_at": at, "failed_service_count": 2,
        "resources": {"status": "available", "cpu_observed_at": at, "memory_observed_at": at,
            "cpu_busy_percent": 10, "memory_total_bytes": 1000, "memory_available_bytes": 500},
        "root_disk": {"mountpoint": "/", "available_bytes": free, "total_bytes": 1000, "device": "/dev/test"}}]}


def metrics_receipt(at, *, host="h610"):
    def sample(value, labels=None):
        return {"samples": [{"value": value, "labels": labels or {}, "state": "available", "sample_at_unix_seconds": at}]}
    return {"ok": True, "operation": "host.metrics", "result": {"host": host, "observed_at": at,
        "observation": {"state": "available", "metrics": {
            "cpu_idle_seconds_per_second": sample(0.9, {"cpu": "0"}),
            "memory_total_bytes": sample(1000), "memory_available_bytes": sample(500)}}}}


class TaskEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tasks.sqlite3"
        self.store = SubAgentStore(self.path)
        self.task = self.new_task("group:a")
        self.run = self.store.create_run(self.task.task_id, TaskStep("inspect", "operator", "inspect", "report"),
                                         allowed_tools=[], model_profile="test")

    def new_task(self, scope):
        return self.store.create_task(scope_key=scope, conversation_id=scope + ":user:2",
            requester_user_id=2, trigger_message_id=None, objective="inspection", max_parallelism=3, max_steps=5, now=1000)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, payload, tool="host_inspect", at=1010, args=None):
        return self.store.record_evidence(self.task.task_id, self.run.run_id, tool,
                                         args or {"host_id": "h610"}, payload, now=at)

    def evaluate(self, plan, refs):
        return evaluate_acceptance(plan, self.store.task_evidence(self.task.task_id), review(refs), task_created_at=1000)

    def test_receipt_is_immutable_deduplicated_and_recovers(self):
        first = self.record(observation(1010))
        duplicate = self.record(observation(1010), at=1020)
        self.assertEqual(first, duplicate)
        changed = self.record(observation(1020, 210), at=1020)
        self.assertNotEqual(first["ref"], changed["ref"])
        self.store.close()
        self.store = SubAgentStore(self.path)
        items = self.store.task_evidence(self.task.task_id)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["payload"]["hosts"][0]["root_disk"]["available_bytes"], 200)

    def test_scope_run_and_revision_are_hard_boundaries(self):
        first = self.record(observation(1010))
        other = self.new_task("group:b")
        self.assertEqual(self.store.task_evidence(other.task_id), [])
        with self.assertRaises(PermissionError):
            self.store.record_evidence(other.task_id, self.run.run_id, "host_inspect", {}, observation(1010))
        self.assertFalse(json.loads(read_evidence([], {"ref": first["ref"]}))["ok"])
        self.store.update_control(self.task.task_id, expected_version=0, revision=2)
        self.assertEqual(self.store.task_evidence(self.task.task_id), [])
        with self.assertRaises(PermissionError):
            self.store.record_evidence(self.task.task_id, self.run.run_id, "host_inspect", {}, observation(1010), revision=1)

    def test_overlarge_evidence_cannot_pass_and_secrets_are_redacted(self):
        ref = self.record({"ok": True, "api_key": "secret", "headers": {"authorization": "Bearer xyz"},
                           "body": "sk-12345678901234567890", "command": "df  -B1\ntrue"})
        item = self.store.task_evidence(self.task.task_id)[0]
        self.assertNotIn("sk-123", item["payload_json"])
        self.assertEqual(item["payload"]["api_key"], "[REDACTED]")
        self.assertEqual(item["payload"]["command"], "df  -B1\ntrue")
        ref = self.record({"content": "a" * (MAX_EVIDENCE_BYTES + 1)})
        self.assertFalse(ref["complete"])
        self.assertEqual(self.evaluate(contract(), [ref["ref"]])["status"], "unverified")

    def test_inspection_can_finish_with_findings_but_not_wrong_host_or_stale_data(self):
        ref = self.record(observation(1010))
        self.assertEqual(self.evaluate(contract("host_inspection", host_id="h610"), [ref["ref"]])["status"], "passed")
        self.assertEqual(self.evaluate(contract("host_inspection", host_id="tank"), [ref["ref"]])["status"], "unverified")
        stale = self.record(observation(500))
        self.assertEqual(self.evaluate(contract("host_inspection", host_id="h610"), [stale["ref"]])["status"], "unverified")

    def test_disk_comparison_requires_completed_action_between_samples(self):
        before = self.record(observation(1010, 200))
        after = self.record(observation(1040, 300), at=1040)
        plan = contract("disk_delta", host_id="h610", mountpoint="/", minimum_delta_bytes=50)
        refs = [before["ref"], after["ref"]]
        self.assertEqual(self.evaluate(plan, refs)["status"], "unverified")
        op = self.record({"operation_id": "op_one", "host_id": "h610", "status": "succeeded",
                          "created_at": 1020, "updated_at": 1030}, tool="operation_status", at=1030)
        refs.append(op["ref"])
        result = self.evaluate(plan, refs)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["criteria"][0]["detail"]["delta_bytes"], 100)
        self.assertEqual(result["criteria"][0]["detail"]["operation_ids"], ["op_one"])
        plan["outcome_checks"][0]["minimum_delta_bytes"] = 101
        self.assertEqual(self.evaluate(plan, refs)["status"], "failed")

    def test_service_needs_verified_native_receipt_not_just_exit_zero(self):
        plan = contract("service_effect", host_id="h610", unit="test.service", action="restart")
        payload = {"operation_id": "op_one", "host_id": "h610", "status": "succeeded",
                   "created_at": 1005,
                   "result": {"verification": {"level": "command_exit", "verified": False}}}
        ref = self.record(payload, tool="operation_status")
        self.assertEqual(self.evaluate(plan, [ref["ref"]])["status"], "unverified")
        payload["result"]["verification"] = {"level": "service_state", "verified": True,
            "host": "h610", "unit": "test.service", "action": "restart", "current": {"observed_at": 1010}}
        ref = self.record(payload, tool="operation_status")
        self.assertEqual(self.evaluate(plan, [ref["ref"]])["status"], "passed")
        forged = self.record(payload, tool="sandbox_exec")
        self.assertEqual(self.evaluate(plan, [forged["ref"]])["status"], "unverified")
        payload["created_at"] = 900
        old_operation = self.record(payload, tool="operation_status")
        self.assertEqual(self.evaluate(plan, [old_operation["ref"]])["status"], "unverified")

    def test_flat_and_wrapped_native_service_receipts_have_the_same_effect(self):
        plan = contract("service_effect", host_id="h610", unit="test.service", action="restart")
        payload = {"operation": "maxops.execute", "operation_id": "op_native",
            "host_id": "h610", "status": "succeeded", "created_at": 1005,
            "result": {"verification": {"level": "service_state", "verified": True,
                "host": "h610", "unit": "test.service", "action": "restart",
                "current": {"observed_at": 1010}}}}
        for body in (payload, {"ok": True, "operation": payload}):
            ref = self.record(body, tool="ops_call", args={"operation": "units.restart",
                "params": {"host": "h610", "unit": "test.service"}})
            result = self.evaluate(plan, [ref["ref"]])
            self.assertEqual(result["status"], "passed")
            self.assertTrue(all(row["status"] == "passed" for row in result["criteria"]))
        self.record({**payload, "status": "needs_attention"}, tool="operation_status", at=1020)
        self.assertEqual(self.evaluate(plan, [ref["ref"]])["status"], "unverified")

    def test_flat_failed_operation_is_not_successful_generic_evidence(self):
        for state in ("failed", "awaiting_approval", "running", "needs_attention"):
            with self.subTest(state=state):
                ref = self.record({"operation": "maxops.execute", "operation_id": "op_native",
                    "host_id": "h610", "created_at": 1005, "status": state}, tool="operation_status")
                self.assertEqual(self.evaluate(contract(), [ref["ref"]])["status"], "unverified")

    def test_disk_review_can_anchor_a_complete_task_evidence_bundle_with_one_sample(self):
        before = self.record(observation(1010, 200))
        after = self.record(observation(1040, 300), at=1040)
        plan = contract("disk_delta", host_id="h610", mountpoint="/", minimum_delta_bytes=50)
        self.assertEqual(self.evaluate(plan, [after["ref"]])["status"], "unverified")
        op = self.record({"operation": "maxops.execute", "operation_id": "op_cleanup",
            "host_id": "h610", "status": "succeeded", "created_at": 1020, "updated_at": 1030},
            tool="operation_status", at=1030)
        result = self.evaluate(plan, [after["ref"]])
        self.assertEqual(result["status"], "passed")
        row = result["criteria"][0]
        self.assertEqual(row["detail"]["delta_bytes"], 100)
        self.assertEqual(set(row["evidence_refs"]), {before["ref"], after["ref"], op["ref"]})
        self.assertEqual(row["detail"]["operation_ids"], ["op_cleanup"])
        for payload, tool, args in (
            (observation(1040, 300, host="tank"), "host_inspect", {"host_id": "tank"}),
            (observation(1040, 300), "sandbox_exec", {"host_id": "h610"}),
            ({"ok": True, "operation": "jobs.logs", "result": {"stdout": "freed 100 bytes"}}, "ops_call", {}),
        ):
            ref = self.record(payload, tool=tool, at=1040, args=args)
            self.assertEqual(self.evaluate(plan, [ref["ref"]])["status"], "unverified")
        plan["outcome_checks"][0]["minimum_delta_bytes"] = 101
        self.assertEqual(self.evaluate(plan, [after["ref"]])["status"], "failed")
        self.record({"operation": "maxops.execute", "operation_id": "op_cleanup",
            "host_id": "h610", "status": "failed", "created_at": 1020, "updated_at": 1041},
            tool="operation_status", at=1041)
        plan["outcome_checks"][0]["minimum_delta_bytes"] = 1
        self.assertEqual(self.evaluate(plan, [after["ref"]])["status"], "unverified")

    def test_planner_cannot_omit_the_effect_of_a_typed_mutation(self):
        read = self.record({"ok": True, "content": "read fine"}, tool="web_search")
        self.record({"operation_id": "op_unverified", "host_id": "h610", "created_at": 1005,
                     "status": "succeeded", "result": {"verification": {"verified": False}}},
            tool="service_control", args={"host_id": "h610", "unit": "test.service", "action": "restart"})
        result = self.evaluate(contract(), [read["ref"]])
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(len(result["criteria"]), 2)

    def test_missing_duplicate_and_foreign_reviews_never_pass(self):
        ref = self.record({"ok": True, "content": "evidence"})
        items = self.store.task_evidence(self.task.task_id)
        self.assertEqual(evaluate_acceptance(contract(), items, {}, task_created_at=1000)["status"], "unverified")
        value = review([ref["ref"]])
        value["metadata"]["criterion_reviews"] *= 2
        self.assertEqual(evaluate_acceptance(contract(), items, value, task_created_at=1000)["status"], "unverified")
        self.assertEqual(self.evaluate(contract(), ["evidence#invented"])["status"], "unverified")

    def test_reviewer_cannot_cherry_pick_an_old_success_over_a_later_failure(self):
        ref = self.record(observation(1010))
        self.record({"ok": False, "error": "host unreachable"}, at=1020)
        self.assertEqual(self.evaluate(contract("host_inspection", host_id="h610"), [ref["ref"]])["status"], "unverified")

    def test_missing_resource_samples_are_not_a_complete_host_inspection(self):
        data = observation(1010)
        del data["hosts"][0]["resources"]
        ref = self.record(data)
        self.assertEqual(self.evaluate(contract("host_inspection", host_id="h610"), [ref["ref"]])["status"], "unverified")

    def test_native_metrics_can_complete_the_same_host_inspection_with_explicit_evidence(self):
        data = observation(990)
        data["hosts"][0]["resources"] = {"status": "unavailable"}
        host_ref = self.record(data)
        metric_ref = self.record(metrics_receipt(1015), tool="ops_call", at=1015,
            args={"operation": "host.metrics", "params": {"host": "h610"}})
        refs = [host_ref["ref"], metric_ref["ref"]]
        result = self.evaluate(contract("host_inspection", host_id="h610"), refs)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["criteria"][0]["detail"]["resource_evidence_ref"], metric_ref["ref"])
        self.assertEqual(result["criteria"][0]["detail"]["observation"]["resources"]["cpu_busy_percent"], 10)
        self.record({"ok": False, "error": "metrics unavailable"}, tool="ops_call", at=1020,
            args={"operation": "host.metrics", "params": {"host": "h610"}})
        self.assertEqual(self.evaluate(contract("host_inspection", host_id="h610"), refs)["status"], "unverified")

    def test_current_inspection_accepts_complementary_sources_in_one_sample_cycle(self):
        data = observation(1010)
        data["hosts"][0]["resources"] = {"status": "unavailable"}
        host_ref = self.record(data)
        metric_ref = self.record(metrics_receipt(1010), tool="ops_call", at=1015,
            args={"operation": "host.metrics", "params": {"host": "h610"}})
        refs = [host_ref["ref"], metric_ref["ref"]]
        self.assertEqual(self.evaluate(contract("host_inspection", host_id="h610"), refs)["status"], "passed")
        self.assertEqual(self.evaluate(contract("disk_delta", host_id="h610", mountpoint="/",
            minimum_delta_bytes=1), refs)["status"], "unverified")

    def test_wrong_host_or_stale_native_metrics_cannot_supply_missing_resources(self):
        data = observation(1010)
        data["hosts"][0]["resources"] = {"status": "unavailable"}
        host_ref = self.record(data)
        for at, host in ((1015, "tank"), (900, "h610")):
            ref = self.record(metrics_receipt(at, host=host), tool="ops_call", at=1015,
                args={"operation": "host.metrics", "params": {"host": "h610"}})
            self.assertEqual(self.evaluate(contract("host_inspection", host_id="h610"),
                [host_ref["ref"], ref["ref"]])["status"], "unverified")

    def test_separated_observations_explain_what_to_refresh_without_relaxing_gate(self):
        host_ref = self.record(observation(1010))
        metric_ref = self.record(metrics_receipt(1260), tool="ops_call", at=1260,
            args={"operation": "host.metrics", "params": {"host": "h610"}})
        result = self.evaluate(contract("host_inspection", host_id="h610"),
            [host_ref["ref"], metric_ref["ref"]])
        self.assertEqual(result["status"], "unverified")
        row = result["criteria"][0]
        self.assertEqual(row["detail"]["observation_gap_seconds"], 250)
        self.assertEqual(row["detail"]["resource_evidence_ref"], metric_ref["ref"])
        self.assertIn("不必重跑目录扫描", row["reason"])

    def test_generic_review_cannot_hide_incomplete_host_coverage(self):
        data = observation(1010)
        del data["hosts"][0]["resources"]
        ref = self.record(data)
        result = self.evaluate(contract(), [ref["ref"]])
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["criteria"][-1]["criterion_index"], "inspection:h610")

    def test_report_and_final_message_cannot_hide_unverified_outcome(self):
        report = {"status": "success", "findings": [{"description": "all fixed", "evidence_refs": ["invented"]}],
                  "completed": [], "authorization": [], "next_verification": []}
        validated = validate_report(report, [], required=True)
        self.assertEqual(validated["status"], "partial")
        self.assertEqual(validated["findings"], [])
        matrix = self.evaluate(contract(), [])
        validation = {"status": "unverified", "task_outcome": matrix}
        self.assertTrue(acceptance_blocks_completion(validation))
        text = outcome_report(validation, "所有服务器都修好了")
        self.assertNotIn("所有服务器都修好了", text)
        self.assertIn("尚未验证", text)

    def test_file_report_does_not_repeat_stale_pre_delivery_claim(self):
        matrix = {"status": "passed", "criteria": [{"kind": "evidence",
            "description": "单页 PDF", "status": "passed", "reason": "页数已核实"}]}
        receipt = [{"ok": True, "state": "acknowledged", "filename": "report.pdf"}]
        stale_draft = "文件还不能发到群里"
        failed_review = {"status": "failed", "task_outcome": matrix,
            "unresolved": ["验收沙盒误产生了临时文件"]}
        text = outcome_report(failed_review, stale_draft, receipt)
        self.assertIn("任务尚未全部完成", text)
        self.assertIn("文件交付：1/1 个已确认送达", text)
        self.assertNotIn("已完成并核实", text)
        self.assertNotIn(stale_draft, text)

        verified = {"status": "passed", "task_outcome": matrix}
        text = outcome_report(verified, stale_draft, receipt)
        self.assertIn("已完成并核实", text)
        self.assertIn("文件交付：1/1 个已确认送达", text)
        self.assertNotIn(stale_draft, text)

        pending = [{"ok": False, "state": "queued", "filename": "report.pdf"}]
        text = outcome_report(verified, stale_draft, pending)
        self.assertIn("任务尚未全部完成", text)
        self.assertIn("文件交付：0/1 个已确认送达", text)

    def test_timeline_never_confuses_execution_with_delivery(self):
        self.store.set_task_state(self.task.task_id, "completed", plan={"contract": {"delivery_required": True}},
                                  result={"execution_state": "succeeded"})
        task = self.store.get(self.task.task_id)
        final = SimpleNamespace(status="committed", updated_at=1100)
        rows = [{"revision": 1, "state": "unknown", "updated_at": 1100}]
        progress = task_progress(task, [], [], [], rows, final, revision=1)
        self.assertEqual(progress["execution_status"], "completed")
        self.assertEqual(progress["delivery_status"], "partial")
        self.assertEqual(progress["stages"][4]["status"], "unverified")
        rows[0]["state"] = "acknowledged"
        self.assertEqual(task_progress(task, [], [], [], rows, final, revision=1)["delivery_status"], "committed")
        final.status = "ambiguous"
        self.assertEqual(task_progress(task, [], [], [], rows, final, revision=1)["delivery_status"], "ambiguous")

    def test_timeline_accepts_native_read_receipts_without_requiring_approval(self):
        self.store.set_task_state(self.task.task_id, "running")
        task = self.store.get(self.task.task_id)
        external = [{"run_id": self.run.run_id, "call_id": "metrics", "status": "resolved",
            "updated_at": 1010, "response": metrics_receipt(1010), "remote_path": "",
            "request": {"tool_arguments": {"operation": "host.metrics", "params": {"host": "h610"}}}}]
        result = task_progress(task, [], [], external, [], None, revision=1)
        self.assertEqual(result["stages"][2]["status"], "not_required")
        self.assertEqual(result["operations"][0]["host_id"], "h610")
        self.assertEqual(result["operations"][0]["arguments"]["operation"], "host.metrics")
        self.assertEqual(result["operations"][0]["tool_name"], "host.metrics")
        self.assertEqual(result["operations"][0]["status"], "resolved")
        self.assertIn("不代表业务目标已完成", result["operations"][0]["summary"])
        self.assertNotIn("等待", result["operations"][0]["summary"])

    def test_timeline_preserves_managed_operation_approval_and_verification(self):
        task = self.store.get(self.task.task_id)
        record = {"operation_id": "op_test", "host_id": "h610", "status": "awaiting_approval"}
        external = [{"run_id": self.run.run_id, "call_id": "restart", "status": "pending",
            "updated_at": 1010, "response": {"operation": record}}]
        self.assertEqual(task_progress(task, [], [], external, [], None, revision=1)["stages"][2]["status"], "waiting")
        record.update(status="succeeded", approval_ref="approval_test",
            result={"verification": {"level": "service_state", "verified": True}})
        external[0]["response"] = record
        result = task_progress(task, [], [], external, [], None, revision=1)
        self.assertEqual(result["stages"][2]["status"], "completed")
        self.assertTrue(result["operations"][0]["verification"]["verified"])

    def test_terminal_timeline_does_not_offer_stale_approval_as_waiting(self):
        record = {"operation_id": "op_test", "host_id": "h610", "status": "awaiting_approval"}
        external = [{"run_id": self.run.run_id, "call_id": "scan", "status": "pending",
            "updated_at": 1010, "response": {"operation": record}}]
        for status in ("completed", "partial", "failed", "cancelled"):
            with self.subTest(status=status):
                self.store.set_task_state(self.task.task_id, status)
                result = task_progress(self.store.get(self.task.task_id), [], [], external, [], None, revision=1)
                self.assertEqual(result["stages"][2]["status"], "unverified")
                self.assertIn("本轮已结束", result["stages"][2]["detail"])
                self.assertEqual(result["operations"][0]["status"], "unverified")
                self.assertEqual(result["operations"][0]["last_observed_status"], "awaiting_approval")
                self.assertIn("不代表现在仍可批准或已经执行", result["operations"][0]["summary"])
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertNotIn("approval_ref", record)

    def test_timeline_excludes_superseded_repair_findings(self):
        old = SimpleNamespace(run_id=10, step_key="report__repair_1", result={
            "metadata": {"superseded_by_revision": 2}, "findings": ["旧结论"],
            "completed": ["旧工作"], "authorization": ["旧授权"], "next_verification": ["旧计划"]})
        current = SimpleNamespace(run_id=11, step_key="inspect-tank", result={
            "findings": ["本轮结论"], "completed": [], "authorization": [], "next_verification": []})
        result = task_progress(self.store.get(self.task.task_id), [old, current], [], [], [], None, revision=2)
        self.assertEqual([item["value"] for item in result["findings"]], ["本轮结论"])
        self.assertEqual(result["completed"], [])
        self.assertEqual(result["authorization"], [])
        self.assertEqual(result["next_verification"], [])


class OutcomeContractTests(unittest.TestCase):
    def test_entry_persists_typed_requirements(self):
        raw = decision("delegate")
        raw["acceptance"] = ["检查 h610", "检查 tank"]
        raw["outcome_checks"] = [{"criterion_index": i, "kind": "host_inspection", "host_id": host}
                                  for i, host in enumerate(("h610", "tank"))]
        parsed = EntryDecision.parse(raw)
        restored = EntryDecision.from_payload(parsed.as_payload())
        self.assertEqual(restored.contract, parsed.contract)
        self.assertEqual(parsed.contract.as_payload()["version"], 3)

    def test_new_entry_rejects_crossed_and_grouped_target_checks(self):
        raw = decision("delegate")
        raw["acceptance"] = ["检查 node-a", "检查 node-b"]
        raw["outcome_checks"] = [
            {"criterion_index": 0, "kind": "host_inspection", "host_id": "node-a"},
            {"criterion_index": 1, "kind": "host_inspection", "host_id": "node-b"}]
        for criteria in (["检查 node-a、node-b", "检查 node-b"],
                         ["检查 node-b", "检查 node-a"],
                         ["检查 node-a2", "检查 node-b"],
                         ["检查 node-a", "node-a 的授权是否批准"]):
            with self.subTest(criteria=criteria), self.assertRaisesRegex(ValueError, "must name only target host"):
                EntryDecision.parse({**raw, "acceptance": criteria})
        EntryDecision.parse({**raw, "acceptance": ["NODE-A（磁盘/服务）", "node-b 的资源状态"]})

    def test_old_contract_keeps_its_version_and_binding_compatibility(self):
        raw = decision("delegate")
        raw["acceptance"] = ["验证目标"]
        raw["outcome_checks"] = [{"criterion_index": 0, "kind": "host_inspection", "host_id": "node-a"}]
        old = EntryDecision.from_payload({**raw, "contract": {"version": 2}})
        self.assertEqual(old.contract.as_payload()["version"], 2)
        self.assertEqual(EntryDecision.from_payload(old.as_payload()).contract, old.contract)
        with self.assertRaisesRegex(ValueError, "must name only target host"):
            EntryDecision.from_payload({**raw, "contract": {"version": 3}})

    def test_invalid_checks_are_rejected_and_omissions_still_need_review(self):
        self.assertEqual(normalize_checks([], ["prove it"])[0]["kind"], "evidence")
        for value in (
            [{"criterion_index": 1, "kind": "evidence"}],
            [{"criterion_index": 0, "kind": "host_inspection", "host_id": "h610;rm"}],
            [{"criterion_index": 0, "kind": "service_effect", "host_id": "h610", "unit": "x"}],
            [{"criterion_index": 0, "kind": "disk_delta", "host_id": "h610", "minimum_delta_bytes": 0}],
        ):
            with self.assertRaises(ValueError):
                normalize_checks(value, ["prove it"])


if __name__ == "__main__":
    unittest.main()
