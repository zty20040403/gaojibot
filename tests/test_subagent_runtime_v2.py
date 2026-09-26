from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests.test_subagent_v2 import decision, profile
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence, active_model_policy
from src.plugins.ai_chat.agent.execution import DECISION_TOOL, EntryDecision, ExecutionEntryError, active_agent_step
from src.plugins.ai_chat.agent.model_routing import choose_agent_profile, model_scope_for_role, validate_model_policy
from src.plugins.ai_chat.agent.scheduling import SpecialistScheduler
from src.plugins.ai_chat.agent.workspaces import (
    ArtifactCaptureError,
    StepWorkspaces,
    prune_acknowledged_artifacts,
)
from src.plugins.ai_chat.agent.sessions import read_upstream_result, upstream_index
from src.plugins.ai_chat.agent_tools import AgentToolExecutor
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import (
    AgentExecutionHooks,
    SubAgentCoordinator,
    SubAgentStore,
    TaskStep,
    StepOutcome,
    _delivery_outcomes,
    _settled_task_status,
    _apply_completed_repairs,
    _interrupted_run_ids,
    _repair_with_evidence,
    _synthesis_input,
    _workflow_runs,
)
from src.plugins.ai_chat.deepseek import DeepSeekTrace


class RuntimeV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SubAgentStore(Path(self.tmp.name) / "agents.sqlite3")
        self.catalog = ModelCatalog({n: profile(n) for n in ("qwen-local", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")}, default_profile="qwen-local")
        self.coordinator = SubAgentCoordinator(self.store, self.catalog, logger=Mock())
        self.packet = ContextPacket("group:1", "group:1:user:2", 2, 3, "实现商城")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def submit(self):
        return self.coordinator.submit(packet=self.packet, decision=EntryDecision.parse(decision("workflow")),
            dispatch={"bot_id": "123", "event": {"user_id": 2}, "profile": "qwen-local"})

    async def test_delegate_submission_survives_into_resumable_workflow(self):
        entry = EntryDecision.parse(decision("delegate"))
        task = self.coordinator.submit(packet=self.packet, decision=entry,
            dispatch={"bot_id": "123", "event": {"user_id": 2}, "profile": "qwen-local"})
        self.assertEqual(task.status, "queued")
        self.assertEqual(self.store.control(task.task_id)["dispatch"]["bot_id"], "123")
        with patch.object(self.coordinator, "_execute_workflow", new=AsyncMock(return_value="done")) as execute:
            result = await self.coordinator._resume_task(task, context=self.packet,
                selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock(),
                parent_trace=None, progress=None, hooks=None)
        self.assertEqual(result, "done")
        self.assertEqual(len(self.store.runs(task.task_id)), 1)
        self.assertEqual(self.store.get(task.task_id).plan["contract"], entry.contract.as_payload())
        execute.assert_awaited_once()

    async def test_explicit_task_entry_gets_the_same_acceptance_contract(self):
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=decision("workflow"))) as planner:
            entry = await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(entry.contract.as_payload()["version"], 3)
        self.assertTrue(entry.contract.acceptance)
        planner.assert_awaited_once()
        self.assertIn(json.dumps(DECISION_TOOL["function"]["parameters"], ensure_ascii=False), planner.call_args.args[0])
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=decision("direct"))):
            with self.assertRaises(ExecutionEntryError):
                await self.coordinator.prepare_entry(self.packet, self.catalog.default)

    async def test_explicit_entry_repairs_missing_fields_once_without_starting_invalid_task(self):
        incomplete = decision("workflow")
        del incomplete["answer"]
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(
                side_effect=[incomplete, decision("workflow")])) as planner:
            entry = await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(entry.mode, "workflow")
        self.assertEqual(planner.await_count, 2)
        self.assertIn("answer must be a string", planner.call_args.args[0])
        self.assertEqual(self.store.recent(), [])
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=incomplete)) as planner:
            with self.assertRaises(ExecutionEntryError):
                await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(planner.await_count, 2)
        self.assertEqual(self.store.recent(), [])

    async def test_explicit_entry_repairs_crossed_host_bindings_before_submit(self):
        valid = decision("workflow")
        valid["acceptance"] = ["检查 node-a", "检查 node-b"]
        valid["outcome_checks"] = [
            {"criterion_index": 0, "kind": "host_inspection", "host_id": "node-a"},
            {"criterion_index": 1, "kind": "host_inspection", "host_id": "node-b"}]
        invalid = {**valid, "acceptance": ["检查 node-a、node-b", "node-a 容量报告"]}
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(side_effect=[invalid, valid])) as planner:
            entry = await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(planner.await_count, 2)
        self.assertIn("must name only target host", planner.call_args.args[0])
        self.assertEqual(entry.contract.acceptance, tuple(valid["acceptance"]))
        self.assertEqual(self.store.recent(), [])
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=invalid)) as planner:
            with self.assertRaises(ExecutionEntryError):
                await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(planner.await_count, 2)
        self.assertEqual(self.store.recent(), [])

    async def test_explicit_delegate_keeps_selected_role_and_no_retry_on_transport_error(self):
        payload = decision("delegate")
        role = payload["steps"][0]["agent"]
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=payload)):
            entry = await self.coordinator.prepare_entry(self.packet, self.catalog.default, role=role)
        self.assertEqual(entry.steps[0]["agent"], role)
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(side_effect=TimeoutError)) as planner:
            with self.assertRaises(TimeoutError):
                await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        planner.assert_awaited_once()

    async def test_entry_repairs_incapable_worker_before_task_submission(self):
        invalid = decision("workflow")
        invalid["steps"][0].update(agent="analyst", required_tools=["ops_call"])
        valid = json.loads(json.dumps(invalid))
        valid["steps"][0]["agent"] = "operator"
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(side_effect=[invalid, valid])) as planner:
            entry = await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(entry.steps[0]["agent"], "operator")
        self.assertEqual(planner.await_count, 2)
        self.assertIn("role_tools", planner.call_args.args[0])
        self.assertIn("analyst cannot use ops_call", planner.call_args.args[0])
        self.assertEqual(self.store.recent(), [])
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=invalid)) as planner:
            with self.assertRaises(ExecutionEntryError):
                await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(planner.await_count, 2)
        self.assertEqual(self.store.recent(), [])

    async def test_entry_repairs_say_delivery_step_before_task_submission(self):
        invalid = decision("workflow")
        invalid["steps"][-1].update(objective="发送最终报告", required_tools=["say"])
        valid = decision("workflow")
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(side_effect=[invalid, valid])) as planner:
            entry = await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(planner.await_count, 2)
        self.assertIn("say is progress-only", planner.call_args.args[0])
        self.assertEqual(entry.steps[-1]["objective"], valid["steps"][-1]["objective"])
        self.assertEqual(self.store.recent(), [])
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=invalid)) as planner:
            with self.assertRaises(ExecutionEntryError):
                await self.coordinator.prepare_entry(self.packet, self.catalog.default)
        self.assertEqual(planner.await_count, 2)
        self.assertEqual(self.store.recent(), [])


    async def test_queued_plan_reuses_entry_and_can_survive_reopen(self):
        task = self.submit()
        self.assertEqual(task.status, "queued")
        self.assertEqual(self.store.dispatchable_tasks(), [task.task_id])
        self.store.close()
        self.store = SubAgentStore(Path(self.tmp.name) / "agents.sqlite3")
        self.coordinator.store = self.store
        with patch.object(self.coordinator, "_execute_workflow", new=AsyncMock(return_value="done")) as execute, patch("src.plugins.ai_chat.subagents.ask_deepseek_json", new=AsyncMock()) as planner:
            await self.coordinator.resume(task.task_id, scope_key="group:1", requester_user_id=2,
                selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock())
        planner.assert_not_awaited()
        self.assertEqual(len(execute.call_args.kwargs["steps"]), 3)

    async def test_revision_is_scoped_versioned_and_only_invalidates_descendants(self):
        task = self.submit()
        for key, deps in (("frontend", ()), ("backend", ()), ("integration", ("frontend", "backend"))):
            run = self.store.create_run(task.task_id, TaskStep(key, "coder", key, "file", deps), allowed_tools=[], model_profile="qwen-local")
            self.store.finish_run(run.run_id, "succeeded", result={"status": "success", "summary": key})
        self.store.set_task_state(task.task_id, "completed")
        with self.assertRaises(ValueError):
            self.coordinator.revise(task.task_id, scope_key="group:2", requester_user_id=2, instruction="改颜色", step_keys=["frontend"], expected_version=1)
        with self.assertRaises(ValueError):
            self.coordinator.revise(task.task_id, scope_key="group:1", requester_user_id=2, instruction="改颜色", step_keys=["frontend"], expected_version=0)
        self.assertEqual(self.store.get(task.task_id).status, "completed")
        control = self.coordinator.revise(task.task_id, scope_key="group:1", requester_user_id=2, instruction="改颜色", step_keys=["frontend"], expected_version=1)
        self.assertEqual(control["revision"], 2)
        self.assertEqual({r.step_key: r.status for r in self.store.runs(task.task_id)}, {"frontend": "pending", "backend": "succeeded", "integration": "pending"})
        checkpoint = self.store.checkpoints(task.task_id)[-1]["state"]
        self.assertEqual(checkpoint["previous_runs"][0]["result"]["summary"], "frontend")

    async def test_revision_explicitly_corrects_file_delivery_without_disabling_text(self):
        task = self.submit()
        self.store.create_run(task.task_id, TaskStep("frontend", "analyst", "report", "text"),
            allowed_tools=[], model_profile="qwen-local")
        self.store.set_task_state(task.task_id, "partial")
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="只需群内文字报告，不需要文件", step_keys=["frontend"], expected_version=1,
            file_delivery_required=False)
        current = self.store.get(task.task_id)
        self.assertIs(current.plan["contract"]["delivery_required"], False)
        self.assertTrue(self.store.control(task.task_id)["dispatch"])
        checkpoint = self.store.checkpoints(task.task_id)[-1]["state"]
        self.assertIs(checkpoint["file_delivery_required"], False)
        self.assertIn("previous_contract", checkpoint)
        execute = AsyncMock()
        self.assertEqual(await self.coordinator._deliver_requested_artifacts(current, {},
            execute_tool=execute, delivered_artifacts=set(), progress=None), [])
        execute.assert_not_awaited()

    async def test_revision_archives_old_instructions_and_freezes_fresh_scoped_context(self):
        task = self.submit()
        runs = {}
        old_contexts = {}
        histories = {}
        for key, dependencies in (("frontend", ()), ("backend", ()), ("integration", ("frontend", "backend"))):
            run = self.store.create_run(task.task_id, TaskStep(key, "coder", key, "new file", dependencies),
                allowed_tools=[], model_profile="qwen-local")
            runs[key] = run
            self.store.finish_run(run.run_id, "succeeded", result={"status": "success", "summary": key,
                "artifacts": [{"handle": "s123abc:/workspace/old.txt", "snapshot": "a" * 64}]})
            histories[key] = [{"role": "user", "content": "legacy correction: do not generate files"},
                              {"role": "assistant", "content": key}]
            self.store.save_agent_session(task.task_id, run.run_id, histories[key], scope_key=task.scope_key,
                requester_user_id=2, model_profile="qwen-local", expected_version=0)
            old_contexts[key] = self.store.save_run_context(task.task_id, run.run_id,
                self.packet.for_agent(self.coordinator.registry.worker("coder"), upstream={"research": {"summary": "old research"}}))
        self.store.set_task_state(task.task_id, "completed")
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="generate a fresh report", step_keys=["frontend"], expected_version=1)
        archive = self.store.revision_checkpoints(task.task_id)[-1]["state"]["previous_sessions"]
        self.assertEqual({row["run_id"] for row in archive}, {runs["frontend"].run_id, runs["integration"].run_id})
        for key, run in runs.items():
            session = self.store.agent_session(task.task_id, run.run_id, scope_key=task.scope_key, requester_user_id=2)
            if key == "backend":
                self.assertEqual(session["messages"], histories[key])
                self.assertEqual(session["version"], 1)
                self.assertEqual(self.store.run_context(run.run_id), old_contexts[key])
            else:
                self.assertEqual(session["messages"], [])
                self.assertEqual(session["version"], 2)
                self.assertIsNone(self.store.run_context(run.run_id))
                snapshot = next(row for row in archive if row["run_id"] == run.run_id)
                self.assertEqual(snapshot["session"]["messages"], histories[key])
                self.assertEqual(snapshot["context"], old_contexts[key].as_payload())
                with self.assertRaises(RuntimeError):
                    self.store.save_agent_session(task.task_id, run.run_id, histories[key], scope_key=task.scope_key,
                        requester_user_id=2, model_profile="qwen-local", expected_version=1)
        self.store.close()
        self.store = SubAgentStore(Path(self.tmp.name) / "agents.sqlite3")
        self.coordinator.store = self.store
        async def worker(text, history, tools, execute, **kwargs):
            self.assertEqual(history, [])
            self.assertIn("new research", text)
            self.assertIn("previous_version", text)
            self.assertNotIn("legacy correction", text)
            previous = json.loads(await execute("read_agent_result", {"step_id": "previous_version", "section": "artifacts"}))
            self.assertEqual(previous["data"][0]["snapshot"], "a" * 64)
            return json.dumps({"status": "success", "summary": "new report", "findings": [], "completed": [],
                "authorization": [], "next_verification": [], "unresolved": [], "artifacts": []})
        run = next(row for row in self.store.runs(task.task_id) if row.step_key == "frontend")
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=worker):
            result = await self.coordinator._run_step(self.store.get(task.task_id),
                TaskStep("frontend", "coder", run.objective, "new file"), run, context=self.packet,
                upstream={"research": {"summary": "new research"}}, selected_profile=self.catalog.default,
                tools_by_name={}, execute_tool=AsyncMock())
        self.assertEqual(result.state, "success")
        self.assertNotEqual(self.store.run_context(run.run_id).context_hash, old_contexts["frontend"].context_hash)
        self.assertEqual(self.store.revision_checkpoints(task.task_id)[-1]["state"]["previous_sessions"], archive)

    async def test_lost_lease_fences_state_writes(self):
        task = self.submit()
        token = active_job_fence.set(JobFence(1, "worker", 1, lambda: False))
        try:
            with self.assertRaises(LeaseLost):
                self.store.set_task_state(task.task_id, "completed")
        finally:
            active_job_fence.reset(token)
        self.assertEqual(self.store.get(task.task_id).status, "queued")

    async def test_revision_resume_guard_only_checks_new_selected_execution(self):
        task = self.submit()
        runs = [self.store.create_run(task.task_id, TaskStep(key, "operator", key, "facts"),
            allowed_tools=[], model_profile="qwen-local") for key in ("disk", "other")]
        for run in runs:
            self.store.append_event(task.task_id, "agent.tool_started",
                {"idempotency": "keyed", "call_id": "old", "tool_name": "ops_call"}, run_id=run.run_id)
            self.store.finish_run(run.run_id, "failed", result={"status": "failed"})
        self.store.append_checkpoint(task.task_id, "process_interrupted", {"interrupted_runs": [runs[0].run_id]})
        self.store.set_task_state(task.task_id, "partial")
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="重新只读采样", step_keys=["disk"], expected_version=1)
        self.assertEqual(_interrupted_run_ids(self.store.checkpoints(task.task_id)), {runs[0].run_id})
        self.assertTrue(self.store.run_resume_safe(runs[0].run_id))
        self.assertFalse(self.store.run_resume_safe(runs[1].run_id))
        self.store.append_event(task.task_id, "agent.tool_started",
            {"idempotency": "keyed", "call_id": "new", "tool_name": "ops_call"}, run_id=runs[0].run_id)
        self.assertFalse(self.store.run_resume_safe(runs[0].run_id), "New unconfirmed side effects must still block")
        self.store.interrupt_task(task.task_id)

    async def test_another_wait_does_not_demote_a_completed_step(self):
        task = self.submit()
        run = self.store.create_run(task.task_id, TaskStep("disk", "operator", "disk", "facts"),
            allowed_tools=[], model_profile="qwen-local")
        self.store.append_checkpoint(task.task_id, "process_interrupted", {"interrupted_runs": [run.run_id]})
        self.store.finish_run(run.run_id, "succeeded", result={"status": "success", "facts": ["64%"]})
        with patch.object(self.store, "run_resume_safe", return_value=False), patch.object(
                self.coordinator, "_execute_workflow", new=AsyncMock(return_value="done")):
            await self.coordinator._resume_task(task, context=self.packet, selected_profile=self.catalog.default,
                tools=[], execute_tool=AsyncMock(), parent_trace=None, progress=None, hooks=None)
        self.assertEqual(self.store.runs(task.task_id)[0].status, "succeeded")

    async def test_revision_retires_old_repairs_instead_of_skipping_fresh_work(self):
        task = self.submit()
        for key in ("baseline", "baseline__repair_1", "acceptance_r1_test", "unrelated"):
            run = self.store.create_run(task.task_id, TaskStep(key, "operator", key, "facts"),
                allowed_tools=[], model_profile="qwen-local")
            self.store.finish_run(run.run_id, "succeeded", result={"status": "success", "summary": key})
        self.store.set_task_state(task.task_id, "completed")
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="重新采样", step_keys=["baseline"], expected_version=1)
        active = _workflow_runs(self.store.runs(task.task_id))
        self.assertEqual({r.step_key: r.status for r in active}, {"baseline": "pending", "unrelated": "succeeded"})
        retired = {r.step_key: r for r in self.store.runs(task.task_id) if r not in active}
        self.assertEqual(retired["baseline__repair_1"].result["summary"], "baseline__repair_1")
        self.assertEqual(retired["baseline__repair_1"].status, "skipped")
        self.assertEqual(retired["acceptance_r1_test"].status, "skipped")
        with patch.object(self.coordinator, "_execute_workflow", new=AsyncMock(return_value="done")) as execute:
            await self.coordinator._resume_task(self.store.get(task.task_id), context=self.packet,
                selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock(),
                parent_trace=None, progress=None, hooks=None)
        self.assertNotIn("baseline", execute.call_args.kwargs["initial_completed"])

    async def test_repair_keeps_baseline_evidence_without_reviving_stale_artifacts(self):
        task = self.submit()
        step = TaskStep("baseline", "operator", "检查磁盘和服务", "实际数据")
        repair_step = TaskStep("baseline__repair_1", "operator", "补查失败服务", "服务状态")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        repaired_run = self.store.create_run(task.task_id, repair_step, allowed_tools=[], model_profile="qwen-local")
        baseline = StepOutcome(step, run, {"status": "success", "summary": "磁盘已用282GiB；0失败服务",
            "facts": ["/nix/store 66GiB", "较早采样0个失败服务"], "artifacts": [{"handle": "old-artifact"}]}, DeepSeekTrace(), "success")
        repair = StepOutcome(repair_step, repaired_run, {"status": "success", "summary": "最新2个失败服务",
            "facts": ["当前2个失败服务"], "artifacts": []}, DeepSeekTrace(), "success")
        combined = _repair_with_evidence(baseline, repair)
        self.assertEqual(combined.result["facts"], ["当前2个失败服务"])
        self.assertEqual(combined.result["artifacts"], [])
        self.assertIn("/nix/store 66GiB", combined.result["previous_evidence"][0]["facts"])
        upstream = {"baseline": combined.result}
        self.assertIn("previous_evidence", upstream_index(upstream))
        response = json.loads(read_upstream_result(upstream, {"step_id": "baseline", "section": "previous_evidence"}))
        self.assertEqual(response["total"], 1)
        self.assertIn("/nix/store 66GiB", _synthesis_input("巡检", {"baseline": combined}))
        completed = {"baseline": baseline, "baseline__repair_1": repair}
        _apply_completed_repairs(completed)
        _apply_completed_repairs(completed)
        self.assertEqual(len(completed["baseline"].result["previous_evidence"]), 1)
        self.assertTrue(completed["baseline"].succeeded)

    async def test_revision_has_fresh_repair_budget_and_unique_repair_numbers(self):
        task = self.submit()
        step = TaskStep("baseline", "operator", "巡检", "数据")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        for number in (1, 2):
            old = self.store.create_run(task.task_id, TaskStep(f"baseline__repair_{number}", "operator", "old", "facts"),
                allowed_tools=[], model_profile="qwen-local")
            self.store.finish_run(old.run_id, "failed", result={"status": "failed"})
            self.store.append_checkpoint(task.task_id, "adaptive_repair_planned", {"repair_run_id": old.run_id})
        self.store.set_task_state(task.task_id, "partial")
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="重查", step_keys=["baseline"], expected_version=1)
        self.store.finish_run(run.run_id, "succeeded", result={"status": "success"})
        baseline = StepOutcome(step, run, {"status": "success"}, DeepSeekTrace(), "success")
        repaired = StepOutcome(TaskStep("baseline__repair_3", "operator", "补查", "数据"), run,
            {"status": "success"}, DeepSeekTrace(), "success")
        with patch.object(self.coordinator, "_validate_workflow", new=AsyncMock(side_effect=[
                {"status": "failed", "checks": [{"step": "baseline", "ok": False}]}, {"status": "passed"}])), patch.object(
                self.coordinator, "_attempt_adaptive_repair", new=AsyncMock(return_value=(True, repaired))) as repair, patch.object(
                self.coordinator, "_deliver_requested_artifacts", new=AsyncMock(return_value=[])), patch.object(
                self.coordinator, "_supervisor_text", new=AsyncMock(return_value="done")):
            await self.coordinator._execute_workflow(self.store.get(task.task_id), steps=[], runs={},
                context=self.packet, selected_profile=self.catalog.default, tools_by_name={}, execute_tool=AsyncMock(),
                parent_trace=None, progress=None, initial_completed={"baseline": baseline})
        repair.assert_awaited_once()
        self.assertEqual(repair.call_args.kwargs["repair_number"], 3)

    async def test_report_draft_is_persistent_and_invalidated_by_changed_evidence(self):
        task = self.submit()
        self.store.set_task_state(task.task_id, "running", plan={"contract": EntryDecision.parse(decision("workflow")).contract.as_payload()})
        task = self.store.get(task.task_id)
        step = TaskStep("inspect", "operator", "inspect", "facts")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        completed = {"inspect": StepOutcome(step, run, {"status": "success", "summary": "observed"},
                                            DeepSeekTrace(), "success")}
        ref = self.store.record_evidence(task.task_id, run.run_id, "host_inspect", {}, {"ok": True, "value": 1})
        args = (task, completed, task.plan["contract"])
        with patch.object(self.coordinator, "_supervisor_text", new=AsyncMock(return_value="保存的正文")) as model:
            draft = await self.coordinator._prepare_report_draft(*args, self.store.task_evidence(task.task_id),
                selected_profile=self.catalog.default, parent_trace=None)
            self.store.close()
            self.store = SubAgentStore(Path(self.tmp.name) / "agents.sqlite3")
            self.coordinator.store = self.store
            restored = await self.coordinator._prepare_report_draft(*args, self.store.task_evidence(task.task_id),
                selected_profile=self.catalog.default, parent_trace=None)
            self.assertEqual(draft, restored)
            model.assert_awaited_once()
            self.assertEqual([row["evidence_id"] for row in self.store.task_evidence(task.task_id)], [ref["ref"]])
            self.store.record_evidence(task.task_id, run.run_id, "host_inspect", {}, {"ok": True, "value": 2})
            changed = await self.coordinator._prepare_report_draft(*args, self.store.task_evidence(task.task_id),
                selected_profile=self.catalog.default, parent_trace=None)
        self.assertNotEqual(changed["source_hash"], draft["source_hash"])
        self.assertEqual(model.await_count, 2)

    async def test_reviewer_sees_exact_draft_and_final_does_not_rewrite_it(self):
        task = self.submit()
        self.store.set_task_state(task.task_id, "running", plan={"contract": EntryDecision.parse(decision("workflow")).contract.as_payload()})
        task = self.store.get(task.task_id)
        step = TaskStep("inspect", "operator", "inspect", "facts")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        ref = self.store.record_evidence(task.task_id, run.run_id, "host_inspect", {}, {"ok": True, "value": 1})
        source = StepOutcome(step, run, {"status": "success", "summary": "observed"}, DeepSeekTrace(), "success")
        async def review(task, step, run, **kwargs):
            self.assertIn("待验收的精确正文", step.objective)
            self.assertIn('"source_kind": "unverified_model_draft"', step.objective)
            return StepOutcome(step, run, {"status": "success", "summary": "checked",
                "metadata": {"criterion_reviews": [{"criterion_index": 0, "status": "passed",
                    "reason": "正文与实际证据一致", "evidence_refs": [ref["ref"]]}]}}, DeepSeekTrace(), "success")
        with patch.object(self.coordinator, "_supervisor_text", new=AsyncMock(return_value="待验收的精确正文")), \
                patch.object(self.coordinator, "_run_step_reliably", side_effect=review):
            validation = await self.coordinator._validate_workflow(task, {"inspect": source}, context=self.packet,
                selected_profile=self.catalog.default, tools_by_name={}, execute_tool=AsyncMock(),
                hooks=None, parent_trace=None, progress=None, prepare_draft=True)
        self.assertEqual(validation["status"], "passed")
        with patch.object(self.coordinator, "_validate_workflow", new=AsyncMock(return_value=validation)), \
                patch.object(self.coordinator, "_supervisor_text", new=AsyncMock(side_effect=AssertionError("rewrote draft"))) as model:
            answer = await self.coordinator._execute_workflow(task, steps=[], runs={}, context=self.packet,
                selected_profile=self.catalog.default, tools_by_name={}, execute_tool=AsyncMock(),
                parent_trace=None, progress=None, initial_completed={"inspect": source})
        model.assert_not_awaited()
        self.assertIn("待验收的精确正文", answer)
        self.assertEqual(self.store.get(task.task_id).result["report_narrative"], "待验收的精确正文")

    async def test_file_report_uses_verified_receipt_instead_of_stale_draft(self):
        task = self.submit()
        payload = decision("workflow")
        payload["delivery_required"] = True
        contract = EntryDecision.parse(payload).contract.as_payload()
        self.store.set_task_state(task.task_id, "running", plan={"contract": contract})
        task = self.store.get(task.task_id)
        step = TaskStep("inspect", "operator", "inspect", "facts")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        ref = self.store.record_evidence(task.task_id, run.run_id, "host_inspect", {}, {"ok": True, "value": 1})
        source = StepOutcome(step, run, {"status": "success", "summary": "observed"}, DeepSeekTrace(), "success")

        async def review(task, step, run, **kwargs):
            self.assertNotIn("report_draft.text", step.objective)
            return StepOutcome(step, run, {"status": "success", "summary": "checked",
                "metadata": {"criterion_reviews": [{"criterion_index": 0, "status": "passed",
                    "reason": "文件内容已核实", "evidence_refs": [ref["ref"]]}]}}, DeepSeekTrace(), "success")

        with patch.object(self.coordinator, "_supervisor_text", new=AsyncMock(side_effect=AssertionError("early draft"))), \
                patch.object(self.coordinator, "_run_step_reliably", side_effect=review):
            validation = await self.coordinator._validate_workflow(task, {"inspect": source}, context=self.packet,
                selected_profile=self.catalog.default, tools_by_name={}, execute_tool=AsyncMock(),
                hooks=None, parent_trace=None, progress=None, prepare_draft=True)
        self.assertEqual(validation["status"], "passed")
        self.assertNotIn("report_draft", validation)
        receipt = {"ok": True, "state": "acknowledged", "filename": "result.pdf"}
        with patch.object(self.coordinator, "_validate_workflow", new=AsyncMock(return_value=validation)), \
                patch.object(self.coordinator, "_deliver_requested_artifacts", new=AsyncMock(return_value=[receipt])), \
                patch.object(self.coordinator, "_supervisor_text", new=AsyncMock(side_effect=AssertionError("stale narrative"))) as model:
            answer = await self.coordinator._execute_workflow(task, steps=[], runs={}, context=self.packet,
                selected_profile=self.catalog.default, tools_by_name={}, execute_tool=AsyncMock(),
                parent_trace=None, progress=None, initial_completed={"inspect": source})
        model.assert_not_awaited()
        self.assertIn("文件交付：1/1 个已确认送达", answer)
        self.assertNotIn("尚未发送", answer)
        self.assertEqual(self.store.get(task.task_id).result["report_narrative"], "")
        self.assertEqual(self.store.get(task.task_id).status, "completed")

    async def test_resumed_step_sees_latest_upstream_and_own_previous_snapshot(self):
        task = self.submit()
        step = TaskStep("code", "coder", "new color", "code")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        self.store.save_run_context(task.task_id, run.run_id, self.packet.for_agent(self.coordinator.registry.worker("coder"), upstream={}))
        self.store.save_agent_session(task.task_id, run.run_id, [{"role": "assistant", "content": "old work"}],
            scope_key=task.scope_key, requester_user_id=task.requester_user_id, model_profile="qwen-local", expected_version=0)
        self.store.append_checkpoint(task.task_id, "revision_requested", {"previous_runs": [
            {"run_id": run.run_id, "result": {"summary": "old snapshot", "artifacts": [{"name": "old.zip"}]}}]})
        async def worker(text, history, *_args, **_kwargs):
            self.assertIn("new upstream result", text)
            self.assertIn("previous_version", text)
            self.assertIn("old snapshot", text)
            self.assertIn("旧容器已清理", text)
            self.assertEqual(history[0]["content"], "old work")
            return '{"status":"success","summary":"updated"}'
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=worker):
            outcome = await self.coordinator._run_step(task, step, run, context=self.packet,
                upstream={"research": {"summary": "new upstream result"}}, selected_profile=self.catalog.default,
                tools_by_name={}, execute_tool=AsyncMock())
        self.assertEqual(outcome.state, "success")

    async def test_resume_requires_tool_result_and_covered_event_sequence(self):
        task = self.submit()
        run = self.store.create_run(task.task_id, TaskStep("code", "coder", "write", "code"), allowed_tools=[], model_profile="qwen-local")
        event = {"idempotency": "non-idempotent", "call_id": "call1", "tool_name": "sandbox_create"}
        self.store.append_event(task.task_id, "agent.tool_started", event, run_id=run.run_id)
        self.assertFalse(self.store.run_resume_safe(run.run_id))
        self.store.append_event(task.task_id, "agent.tool_finished", {**event, "state": "succeeded"}, run_id=run.run_id)
        self.assertFalse(self.store.run_resume_safe(run.run_id))
        self.store.save_agent_session(task.task_id, run.run_id, [{"role": "tool", "tool_call_id": "call1", "content": "created"}],
            scope_key=task.scope_key, requester_user_id=task.requester_user_id, model_profile="qwen-local", expected_version=0)
        self.assertTrue(self.store.run_resume_safe(run.run_id))
        self.store.append_event(task.task_id, "agent.tool_started", event, run_id=run.run_id)
        self.store.append_event(task.task_id, "agent.tool_finished", {**event, "state": "succeeded"}, run_id=run.run_id)
        self.assertFalse(self.store.run_resume_safe(run.run_id), "A reused provider call id must not acknowledge a newer execution")

    async def test_committed_conversation_progress_is_safe_to_resume(self):
        task = self.submit()
        run = self.store.create_run(task.task_id, TaskStep("code", "coder", "write", "code"), allowed_tools=[], model_profile="qwen-local")
        event = {"idempotency": "non-idempotent", "call_id": "say1", "tool_name": "say"}
        self.store.append_event(task.task_id, "agent.tool_started", event, run_id=run.run_id)
        self.store.append_event(task.task_id, "agent.tool_finished", {**event, "state": "committed"}, run_id=run.run_id)
        self.store.save_agent_session(task.task_id, run.run_id, [
            {"role": "tool", "tool_call_id": "say1", "content": "sent"},
        ], scope_key=task.scope_key, requester_user_id=task.requester_user_id,
            model_profile="qwen-local", expected_version=0)
        self.assertTrue(self.store.run_resume_safe(run.run_id))

    async def test_cancel_queued_task_does_not_resurrect(self):
        task = self.submit()
        self.assertTrue(self.coordinator.cancel(task.task_id))
        self.assertEqual(self.store.get(task.task_id).status, "cancelled")
        self.assertEqual(self.store.dispatchable_tasks(), [])

    async def test_locked_model_cannot_fallback_even_to_non_sol(self):
        policy = validate_model_policy({"mode": "locked", "profile": "gpt-5.6-luna"}, self.catalog)
        token = active_model_policy.set(policy)
        try:
            chosen = choose_agent_profile("coder", self.catalog.default, self.catalog, {})
            with model_scope_for_role("coder", chosen, self.catalog, {}):
                candidates = LLMGateway(catalog=self.catalog)._completion_candidates(chosen, {})
            self.assertEqual([p.name for p in candidates], ["gpt-5.6-luna"])
        finally:
            active_model_policy.reset(token)
        with self.assertRaises(ValueError):
            validate_model_policy({"mode": "locked", "profile": "invented"}, self.catalog)

    async def test_delivery_unknown_never_blindly_reuploads(self):
        task = self.submit()
        self.store.set_task_state(task.task_id, "running", plan={"contract": {"delivery_required": True}})
        step = TaskStep("files", "document", "write", "pdf")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        outcome = StepOutcome(step, run, {"status": "success", "artifacts": [{"handle": "s123abc:/workspace/out.pdf", "name": "out.pdf"}]}, DeepSeekTrace(), "success")
        execute = AsyncMock(side_effect=TimeoutError("receipt unknown"))
        for _ in range(2):
            result = await self.coordinator._deliver_requested_artifacts(task, {"files": outcome}, execute_tool=execute, delivered_artifacts=set(), progress=None)
            self.assertFalse(result[0]["ok"])
        self.assertEqual(execute.await_count, 1)
        self.assertEqual(self.store.deliveries(task.task_id)[0]["state"], "unknown")

    async def test_delayed_group_receipt_cleans_workspace_without_reupload(self):
        from src.plugins.ai_chat.agent.background import SubAgentDispatcher
        from nonebot.adapters.onebot.v11 import GroupMessageEvent
        from src.plugins.ai_chat.onebot_codec import scope_from_event

        event = GroupMessageEvent(
            time=100,
            self_id=123,
            post_type="message",
            message_type="group",
            sub_type="normal",
            message_id=42,
            group_id=1,
            user_id=2,
            message=[{"type": "text", "data": {"text": "生成 PDF"}}],
            raw_message="生成 PDF",
            font=0,
            sender={"user_id": 2, "nickname": "Test", "role": "member"},
        )
        scope_key = scope_from_event(event).key
        packet = ContextPacket(
            scope_key,
            f"{scope_key}:user:2",
            2,
            3,
            "生成 PDF",
        )
        task = self.coordinator.submit(
            packet=packet,
            decision=EntryDecision.parse(decision("workflow")),
            dispatch={
                "bot_id": "123",
                "event": event.model_dump(mode="json"),
                "profile": "qwen-local",
            },
        )
        self.store.set_task_state(
            task.task_id,
            "running",
            plan={"contract": {"delivery_required": True}},
        )
        step = TaskStep("files", "document", "write", "pdf")
        run = self.store.create_run(
            task.task_id,
            step,
            allowed_tools=[],
            model_profile="qwen-local",
        )
        snapshot = "b" * 64
        self.store.finish_run(
            run.run_id,
            "succeeded",
            result={
                "status": "success",
                "artifacts": [
                    {
                        "handle": "s123abc:/workspace/out.pdf",
                        "name": "out.pdf",
                        "snapshot": snapshot,
                    }
                ],
            },
        )
        self.store.begin_delivery(task.task_id, snapshot, {"filename": "out.pdf"})
        self.store.finish_delivery(
            task.task_id,
            snapshot,
            "unknown",
            {
                "filename": "out.pdf",
                "size": 4,
                "upload_started_at": 100,
                "ok": False,
            },
        )
        self.store.set_task_state(task.task_id, "partial")
        artifact_path = Path(self.tmp.name) / "subagent_artifacts" / str(task.task_id) / snapshot
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_bytes(b"%PDF")

        manager = Mock()
        manager.list = AsyncMock(
            return_value=[{"sandbox_id": "s123abc", "purpose": "task"}]
        )
        manager.stop_owned = AsyncMock()
        jobs = DurableJobStore(Path(self.tmp.name) / "receipt-jobs.sqlite3")
        bot = Mock(self_id=123)
        bot.call_api = AsyncMock(
            side_effect=lambda action, **params: {"online": True, "good": True} if action == "get_status" else {
                "files": [
                    {
                        "file_name": "out.pdf",
                        "file_size": 4,
                        "uploader": 123,
                        "upload_time": 101,
                        "file_id": "qq-file-1",
                    }
                ]
            }
        )
        services = SimpleNamespace(
            context=SimpleNamespace(
                subagent_store=self.store,
                job_store=jobs,
                subagent_coordinator=self.coordinator,
                logger=Mock(),
                state_dir=Path(self.tmp.name),
                sandbox_manager=manager,
                settings=SimpleNamespace(subagent_retention_seconds=3600),
            )
        )
        try:
            dispatcher = SubAgentDispatcher(services)
            with patch(
                "src.plugins.ai_chat.agent.background.get_bot",
                return_value=bot,
            ):
                online_response = bot.call_api.side_effect
                before = self.store.deliveries(task.task_id)
                bot.call_api.side_effect = lambda action, **params: {"online": False, "good": True}
                result = await dispatcher.reconcile(task.task_id)
                self.assertEqual(result["state"], "deferred")
                bot.call_api.assert_awaited_once_with("get_status")
                self.assertEqual(self.store.deliveries(task.task_id), before)
                bot.call_api.reset_mock()
                bot.call_api.side_effect = online_response
                result = await dispatcher.reconcile(task.task_id)
            self.assertEqual(result["matched"], 1)
            self.assertEqual(
                self.store.deliveries(task.task_id)[0]["state"],
                "acknowledged",
            )
            manager.stop_owned.assert_awaited_once_with(
                f"{packet.conversation_id}:task#{task.task_id}/files",
                "s123abc",
            )
            self.assertTrue(artifact_path.exists())
        finally:
            jobs.close()

    async def test_unacknowledged_artifact_retains_step_workspace(self):
        task = self.submit()
        self.store.set_task_state(
            task.task_id,
            "running",
            plan={"contract": {"delivery_required": True}},
        )
        step = TaskStep("files", "document", "write", "pdf")
        run = self.store.create_run(
            task.task_id,
            step,
            allowed_tools=[],
            model_profile="qwen-local",
        )
        snapshot = "a" * 64
        self.store.finish_run(
            run.run_id,
            "succeeded",
            result={
                "status": "success",
                "artifacts": [
                    {
                        "handle": "s123abc:/workspace/out.pdf",
                        "name": "out.pdf",
                        "snapshot": snapshot,
                    }
                ],
            },
        )
        self.store.begin_delivery(task.task_id, snapshot, {"filename": "out.pdf"})
        self.store.finish_delivery(
            task.task_id,
            snapshot,
            "unknown",
            {"filename": "out.pdf", "ok": False},
        )
        self.store.set_task_state(task.task_id, "partial")
        workspaces = Mock(finalize_task=AsyncMock())

        await self.coordinator._finalize_finished_task(
            task.task_id, AgentExecutionHooks(workspaces=workspaces)
        )
        self.assertEqual(
            workspaces.finalize_task.await_args.kwargs["artifact_digests"],
            (),
        )

        self.store.finish_delivery(
            task.task_id,
            snapshot,
            "acknowledged",
            {"filename": "out.pdf", "ok": True},
        )
        await self.coordinator._finalize_finished_task(
            task.task_id, AgentExecutionHooks(workspaces=workspaces)
        )
        self.assertEqual(workspaces.finalize_task.await_count, 2)
        self.assertEqual(
            workspaces.finalize_task.await_args.kwargs["artifact_digests"],
            (snapshot,),
        )
        self.assertIsNone(workspaces.finalize_task.await_args.kwargs["cleanup_revision"])
        self.store.set_task_state(task.task_id, "completed")
        await self.coordinator._finalize_finished_task(task.task_id, AgentExecutionHooks(workspaces=workspaces))
        self.assertEqual(workspaces.finalize_task.await_args.kwargs["cleanup_revision"], self.store.control(task.task_id)["revision"])

    async def test_cleanup_requires_current_revision_receipts_but_not_intermediate_delivery(self):
        task = self.submit()
        self.store.set_task_state(task.task_id, "running", plan={
            "contract": {"delivery_required": True},
            "steps": [{"id": "draft"}, {"id": "final", "depends_on": ["draft"]}],
        })
        for key, digest in (("draft", "a" * 64), ("final", "b" * 64)):
            run = self.store.create_run(task.task_id, TaskStep(key, "coder", "build", "zip"),
                allowed_tools=[], model_profile="qwen-local")
            self.store.finish_run(run.run_id, "succeeded", result={"status": "success",
                "artifacts": [{"handle": "s123abc:/workspace/" + key + ".zip", "snapshot": digest}]})
        self.store.begin_delivery(task.task_id, "b" * 64, {"filename": "final.zip"})
        self.store.finish_delivery(task.task_id, "b" * 64, "acknowledged", {"ok": True})
        self.store.set_task_state(task.task_id, "completed")
        current = self.store.get(task.task_id)
        self.assertEqual(self.coordinator._artifact_retention_state(current), (True, ("a" * 64, "b" * 64)))
        with patch.object(self.store, "control", return_value={"revision": 999}):
            self.assertEqual(self.coordinator._artifact_retention_state(current), (False, ()))

    async def test_missing_required_delivery_does_not_authorize_cleanup(self):
        task = self.submit()
        self.store.set_task_state(task.task_id, "completed", plan={"contract": {"delivery_required": True}})
        self.assertEqual(self.coordinator._artifact_retention_state(self.store.get(task.task_id)), (False, ()))

    async def test_delivery_prefers_final_or_repair_artifact(self):
        task = self.submit()
        design_run = self.store.create_run(task.task_id, TaskStep("design", "analyst", "design", "plan"), allowed_tools=[], model_profile="qwen-local")
        repair_run = self.store.create_run(task.task_id, TaskStep("design__repair_1", "coder", "repair", "zip"), allowed_tools=[], model_profile="qwen-local")
        design = StepOutcome(TaskStep("design", "analyst", "design", "plan"), design_run,
            {"status": "success", "artifacts": [{"handle": "s111111:/workspace/plan.md"}]}, DeepSeekTrace(), "success")
        repair = StepOutcome(TaskStep("design__repair_1", "coder", "repair", "zip"), repair_run,
            {"status": "success", "artifacts": [{"handle": "s222222:/workspace/app.zip"}]}, DeepSeekTrace(), "success")
        selected = _delivery_outcomes(task, {"design": design, "design__repair_1": repair})
        self.assertEqual([item.step.key for item in selected], ["design__repair_1"])

    async def test_successful_repair_and_delivery_settle_task_as_completed(self):
        task = self.submit()
        failed_run = self.store.create_run(task.task_id, TaskStep("frontend", "coder", "front", "code"), allowed_tools=[], model_profile="qwen-local")
        repair_run = self.store.create_run(task.task_id, TaskStep("frontend__repair_1", "coder", "repair", "zip"), allowed_tools=[], model_profile="qwen-local")
        failed = StepOutcome(TaskStep("frontend", "coder", "front", "code"), failed_run,
            {"status": "failed"}, DeepSeekTrace(), "failed")
        repair = StepOutcome(TaskStep("frontend__repair_1", "coder", "repair", "zip"), repair_run,
            {"status": "success"}, DeepSeekTrace(), "success")
        status = _settled_task_status(
            [failed, repair], [{"ok": True}], {"status": "passed"},
        )
        self.assertEqual(status, "completed")

    async def test_old_delivery_receipt_does_not_change_new_revision(self):
        task = self.submit()
        self.store.begin_delivery(task.task_id, "file", {"filename": "old.pdf"})
        self.store.update_control(task.task_id, expected_version=1, revision=2)
        self.store.begin_delivery(task.task_id, "file", {"filename": "new.pdf"})
        self.store.finish_delivery(task.task_id, "file", "acknowledged", {"filename": "old.pdf"}, revision=1)
        self.assertEqual({d["revision"]: d["state"] for d in self.store.deliveries(task.task_id)}, {1: "acknowledged", 2: "sending"})

    async def test_exhausted_background_job_settles_task(self):
        from src.plugins.ai_chat.agent.background import SubAgentDispatcher
        task = self.submit()
        jobs = DurableJobStore(Path(self.tmp.name) / "exhausted.sqlite3")
        try:
            services = SimpleNamespace(context=SimpleNamespace(subagent_store=self.store, job_store=jobs,
                subagent_coordinator=self.coordinator, logger=Mock()))
            dispatcher = SubAgentDispatcher(services)
            dispatcher.enqueue(task.task_id)
            job = jobs.claim_due("worker", kinds=(dispatcher.kind,))[0]
            await dispatcher.settle_failed_dispatch(job, "failed")
            self.assertEqual(self.store.get(task.task_id).status, "failed")
            self.assertEqual(self.store.dispatchable_tasks(), [])
        finally:
            jobs.close()

    async def test_kind_filter_does_not_steal_workflows(self):
        jobs = DurableJobStore(Path(self.tmp.name) / "jobs.sqlite3")
        try:
            jobs.enqueue(kind="subagent.workflow", idempotency_key="task1")
            jobs.enqueue(kind="historian", idempotency_key="history1")
            claimed = jobs.claim_due("worker", kinds=("historian",))
            self.assertEqual([job.kind for job in claimed], ["historian"])
            self.assertEqual(jobs.claim_due("other", kinds=()), [])
        finally:
            jobs.close()

    async def test_admin_models_are_versioned_and_dispatch_is_not_exposed(self):
        import httpx
        from fastapi import FastAPI
        from src.plugins.ai_chat.admin import AdminServices
        from tests.admin_session_fixture import ApprovedClient, register_admin
        task = self.submit()
        app = FastAPI()
        register_admin(app, AdminServices(version="test", started_at=1, subagent_store=self.store,
            subagent_coordinator=self.coordinator, model_catalog=self.catalog), token="test-token")
        async with ApprovedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-Test-Login": "admin", "If-Match": '"0"'}
            response = await client.get(f"/bot-admin/api/subagents/{task.task_id}", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("dispatch", response.json()["control"])
            body = {"expected_version": 1, "policy": {"mode": "locked", "profile": "gpt-5.6-luna"}}
            response = await client.put(f"/bot-admin/api/subagents/{task.task_id}/models", headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            stale = await client.put(f"/bot-admin/api/subagents/{task.task_id}/models", headers=headers, json=body)
            self.assertEqual(stale.status_code, 409)

    async def test_background_restores_event_and_enqueues_idempotent_final(self):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent
        from src.plugins.ai_chat.agent.background import SubAgentDispatcher
        from src.plugins.ai_chat.delivery import DeliveryStore
        from src.plugins.ai_chat.onebot_codec import scope_from_event
        event = GroupMessageEvent(time=int(time.time()), self_id=123, post_type="message", message_type="group", sub_type="normal",
            message_id=42, group_id=100, user_id=2, message=[{"type": "text", "data": {"text": "实现商城"}}], raw_message="实现商城", font=0,
            sender={"user_id": 2, "nickname": "Test", "role": "member"})
        packet = ContextPacket(scope_from_event(event).key, "group:100:user:2", 2, 3, "实现商城")
        task = self.coordinator.submit(packet=packet, decision=EntryDecision.parse(decision("workflow")),
            dispatch={"event": event.model_dump(mode="json"), "bot_id": "123", "profile": "qwen-local"})
        jobs = DurableJobStore(Path(self.tmp.name) / "background.sqlite3")
        delivery = DeliveryStore(Path(self.tmp.name) / "delivery.sqlite3")
        try:
            async def execute(_bot, restored, _objective, **kwargs):
                self.assertEqual(restored.group_id, 100)
                self.assertEqual(kwargs['resume_task_id'], task.task_id)
                self.store.set_task_state(task.task_id, "completed", result={"answer": "完成"})
            services = SimpleNamespace(context=SimpleNamespace(subagent_store=self.store, job_store=jobs,
                subagent_coordinator=self.coordinator, logger=Mock(), delivery_store=delivery),
                group_enabled=lambda _: True, tools=SimpleNamespace(_ask_ai=execute))
            dispatcher = SubAgentDispatcher(services)
            dispatcher.enqueue(task.task_id)
            job = jobs.claim_due("worker", kinds=(dispatcher.kind,))[0]
            with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=Mock()):
                first = await dispatcher.execute(job)
                second = await dispatcher.execute(job)
            self.assertEqual(first["delivery_id"], second["delivery_id"])
        finally:
            jobs.close(); delivery.close()

    async def test_snapshot_immutable_and_upstream_scope_enforced(self):
        executor = Mock(owner="owner")
        executor.sandbox_manager.export_artifact = AsyncMock(return_value=(b"artifact-v1", False))
        executor.sandbox_manager.install_readonly_file = AsyncMock()
        workspaces = StepWorkspaces(Path(self.tmp.name), executor)
        artifact = (await workspaces.capture(1, [{"handle": "s123abc:/workspace/code.zip"}]))[0]
        self.assertEqual(executor.sandbox_manager.export_artifact.await_args.args[2], "code.zip")
        await workspaces.import_artifact(1, {"code": {"artifacts": [artifact]}}, {"step_id": "code", "artifact_index": 0, "sandbox_id": "s456abc"})
        self.assertEqual(executor.sandbox_manager.install_readonly_file.await_args.args[2], f"upstream/{artifact['snapshot']}/code.zip")
        with self.assertRaises(ValueError):
            await workspaces.import_artifact(1, {"code": {"artifacts": [artifact]}}, {"step_id": "other", "artifact_index": 0, "sandbox_id": "s456abc"})
        with self.assertRaises(FileNotFoundError):
            await workspaces.import_artifact(2, {"code": {"artifacts": [artifact]}}, {"step_id": "code", "artifact_index": 0, "sandbox_id": "s456abc"})

    async def test_directory_artifact_is_exported_as_zip(self):
        executor = Mock(owner="owner")
        executor.sandbox_manager.export_artifact = AsyncMock(return_value=(b"PK-directory", True))
        workspaces = StepWorkspaces(Path(self.tmp.name), executor)
        artifact = (await workspaces.capture(1, [{
            "handle": "s123abc:/workspace/source", "kind": "directory", "name": "source",
        }]))[0]
        self.assertEqual(artifact["name"], "source.zip")
        self.assertEqual(artifact["kind"], "file")
        self.assertEqual(artifact["source_kind"], "directory")

    async def test_only_acknowledged_snapshots_expire_after_retention(self):
        executor = Mock(owner="owner")
        workspaces = StepWorkspaces(
            Path(self.tmp.name),
            executor,
            retention_seconds=3600,
        )
        acknowledged = workspaces._persist(1, b"acknowledged")
        unconfirmed = workspaces._persist(2, b"unconfirmed")
        manager = executor.sandbox_manager
        manager.list = AsyncMock(return_value=[])
        manager.stop_owned = AsyncMock()
        await workspaces.finalize_task(
            1,
            [],
            artifact_digests=(acknowledged,),
        )
        self.assertTrue(workspaces._path(1, acknowledged).exists())
        self.assertTrue(workspaces._path(2, unconfirmed).exists())

        deleted, invalid = prune_acknowledged_artifacts(
            workspaces.root,
            now=int(time.time()) + 3601,
        )
        self.assertEqual((deleted, invalid), (1, 0))
        self.assertFalse(workspaces._path(1, acknowledged).exists())
        self.assertTrue(workspaces._path(2, unconfirmed).exists())

    async def test_recaptured_snapshot_cancels_old_retention_marker(self):
        executor = Mock(owner="owner")
        executor.sandbox_manager.export_artifact = AsyncMock(
            return_value=(b"same-artifact", False)
        )
        workspaces = StepWorkspaces(
            Path(self.tmp.name),
            executor,
            retention_seconds=3600,
        )
        digest = workspaces._persist(1, b"same-artifact")
        workspaces._mark_for_retention(1, digest, int(time.time()) - 172800)

        await workspaces.capture(
            1,
            [{"handle": "s123abc:/workspace/result.pdf", "name": "result.pdf"}],
        )
        deleted, invalid = prune_acknowledged_artifacts(workspaces.root)
        self.assertEqual((deleted, invalid), (0, 0))
        self.assertTrue(workspaces._path(1, digest).exists())

    async def test_capture_preserves_valid_artifacts_when_another_path_is_bad(self):
        executor = Mock(owner="owner")
        executor.sandbox_manager.export_artifact = AsyncMock(side_effect=[
            (b"valid", False), FileNotFoundError("missing"),
        ])
        workspaces = StepWorkspaces(Path(self.tmp.name), executor)
        with self.assertRaises(ArtifactCaptureError) as raised:
            await workspaces.capture(1, [
                {"handle": "s123abc:/workspace/result.zip", "name": "result.zip"},
                {"handle": "s123abc:/workspace/missing.txt", "name": "missing.txt"},
            ])
        self.assertEqual([item["name"] for item in raised.exception.captured], ["result.zip"])

    async def test_worker_keeps_captured_file_when_sibling_artifact_is_invalid(self):
        task = self.submit()
        step = TaskStep("files", "coder", "build", "zip")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        captured = [{"handle": "s123abc:/workspace/result.zip", "name": "result.zip", "snapshot": "a" * 64}]
        workspaces = Mock()
        workspaces.capture = AsyncMock(side_effect=ArtifactCaptureError(captured, ["missing.txt: missing"]))
        answer = json.dumps({
            "status": "success", "summary": "built",
            "artifacts": [
                {"handle": "s123abc:/workspace/result.zip", "name": "result.zip"},
                {"handle": "s123abc:/workspace/missing.txt", "name": "missing.txt"},
            ],
        })
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=AsyncMock(return_value=answer)):
            outcome = await self.coordinator._run_step(
                task, step, run, context=self.packet, upstream={},
                selected_profile=self.catalog.default, tools_by_name={},
                execute_tool=AsyncMock(), hooks=AgentExecutionHooks(workspaces=workspaces),
            )
        self.assertEqual(outcome.state, "partial")
        self.assertEqual(outcome.result["artifacts"], captured)
        self.assertIn("missing.txt", outcome.result["warnings"][0])

    async def test_validation_and_delivery_use_manager_relative_paths(self):
        from src.plugins.ai_chat.sandbox import DockerSandboxManager
        executor = Mock(owner="owner")
        manager = executor.sandbox_manager
        manager.create = AsyncMock(return_value={"sandbox_id": "s123abc"})
        manager.destroy = AsyncMock()
        checked = []
        async def write(_owner, _sid, path, _content, **_kwargs):
            DockerSandboxManager()._workspace_path(path)
            checked.append(path)
        manager.write_file = AsyncMock(side_effect=write)
        executor.send_file_content = AsyncMock(return_value='{"ok":true}')
        workspaces = StepWorkspaces(Path(self.tmp.name), executor)
        artifact = {"name": "result.txt", "size": 4, "snapshot": workspaces._persist(1, b"test")}
        self.assertTrue((await workspaces.validate(1, artifact))["ok"])
        await workspaces.deliver(1, artifact)
        self.assertEqual(checked, ["acceptance.txt"])
        executor.send_file_content.assert_awaited_once_with(b"test", "result.txt")

    async def test_scheduler_cancellation_releases_slot_and_avoids_group_head_of_line(self):
        scheduler = SpecialistScheduler(total=2, per_group=1, per_model=2)
        async with scheduler.slot("group:1", "luna"):
            waiting = asyncio.create_task(scheduler.slot("group:1", "luna").__aenter__())
            await asyncio.sleep(0)
            async with asyncio.timeout(1), scheduler.slot("group:2", "luna"):
                self.assertEqual(scheduler.snapshot()["active"], 2)
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        self.assertEqual(scheduler.snapshot()["active"], 0)
        self.assertEqual(scheduler.snapshot()["waiting"], 0)

    async def test_workspace_owner_is_context_local_not_shared_mutation(self):
        executor = object.__new__(AgentToolExecutor)
        executor._owner = "group:1:user:2"
        async def owner(key):
            token = active_agent_step.set(key)
            try:
                await asyncio.sleep(0)
                return executor.owner
            finally:
                active_agent_step.reset(token)
        values = await asyncio.gather(owner("task#1/front"), owner("task#1/back"))
        self.assertNotEqual(*values)
        self.assertEqual(executor.owner, "group:1:user:2")


if __name__ == "__main__":
    unittest.main()
