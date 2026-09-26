from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch
from dataclasses import replace
import unittest

from tests import test_subagent_runtime_v2 as runtime
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.agent.execution import EntryDecision, ExecutionEntryError
from src.plugins.ai_chat.subagents import AgentExecutionHooks, TaskStep


class RevisionContractTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime.RuntimeV2Tests.setUp
    tearDown = runtime.RuntimeV2Tests.tearDown
    submit = runtime.RuntimeV2Tests.submit

    def revision(self):
        task = self.submit()
        old = {"version": 3, "objective": "old title", "deliverables": ["PDF"],
            "constraints": ["one page"], "acceptance": ["old title"],
            "outcome_checks": [{"criterion_index": 0, "kind": "evidence"}],
            "delivery_required": True}
        self.store.set_task_state(task.task_id, "partial", plan={"mode": "delegate", "contract": old})
        self.store.create_run(task.task_id, TaskStep("pdf", "document", "old title", "PDF"),
            allowed_tools=[], model_profile="gpt-5.6-luna")
        raw = {**old, "objective": "new title", "acceptance": ["new title"],
            "mode": "revise", "task_type": "execution", "reason": "new title requested",
            "answer": "", "steps": [], "task_id": task.task_id, "step_ids": ["pdf"]}
        return task, old, raw

    async def test_entry_contract_is_persisted_atomically_and_old_contract_archived(self):
        task, old, raw = self.revision()
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="new title", step_keys=["pdf"], expected_version=1,
            contract=EntryDecision.parse(raw).contract.as_payload())
        current = self.store.get(task.task_id)
        self.assertEqual(current.plan["contract"]["acceptance"], ["new title"])
        self.assertEqual(current.plan["contract"]["constraints"], ["one page"])
        self.assertEqual(self.store.revision_checkpoints(task.task_id)[-1]["state"]["previous_contract"], old)
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock()) as planner:
            await self.coordinator._refresh_revision_contract(current, self.catalog.default, None)
        planner.assert_not_awaited()

    async def test_console_refresh_is_persisted_before_worker_and_not_repeated(self):
        task, old, raw = self.revision()
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="new title", step_keys=["pdf"], expected_version=1,
            file_delivery_required=False)
        current = self.store.get(task.task_id)
        self.assertEqual(current.plan["contract_refresh"]["revision"], 2)
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value=raw)) as planner:
            current = await self.coordinator._refresh_revision_contract(current, self.catalog.default, None)
            current = await self.coordinator._refresh_revision_contract(current, self.catalog.default, None)
        planner.assert_awaited_once()
        self.assertEqual(current.plan["contract"]["acceptance"], ["new title"])
        self.assertIs(current.plan["contract"]["delivery_required"], False)
        self.assertNotIn("contract_refresh", current.plan)
        self.assertEqual(self.store.revision_checkpoints(task.task_id)[-1]["state"]["previous_contract"], old)

    async def test_invalid_or_racing_refresh_cannot_change_contract(self):
        task, old, raw = self.revision()
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="new title", step_keys=["pdf"], expected_version=1)
        current = self.store.get(task.task_id)
        with patch.object(self.coordinator, "_supervisor_json", new=AsyncMock(return_value={**raw, "acceptance": []})):
            with self.assertRaises(ExecutionEntryError):
                await self.coordinator._refresh_revision_contract(current, self.catalog.default, None)
        self.assertEqual(self.store.get(task.task_id).plan["contract"], old)
        async def race(*args, **kwargs):
            self.store.update_control(task.task_id, expected_version=2, revision=3)
            return raw
        with patch.object(self.coordinator, "_supervisor_json", side_effect=race):
            with self.assertRaises(LeaseLost):
                await self.coordinator._refresh_revision_contract(current, self.catalog.default, None)
        self.assertEqual(self.store.get(task.task_id).plan["contract"], old)

    async def test_worker_receives_current_contract_not_initial_context(self):
        task, old, raw = self.revision()
        self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
            instruction="new title", step_keys=["pdf"], expected_version=1,
            contract=EntryDecision.parse(raw).contract.as_payload())
        context = replace(self.packet, objective="old title",
            constraints=("keep original scope", "验收：old title", "交付物：old PDF", "one page"))
        with patch.object(self.coordinator, "_run_step_reliably", new=AsyncMock(
                side_effect=RuntimeError("worker reached"))) as worker:
            with self.assertRaisesRegex(RuntimeError, "worker reached"):
                await self.coordinator._resume_task(self.store.get(task.task_id), context=context,
                    selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock(),
                    parent_trace=None, progress=None, hooks=None)
        actual = worker.call_args.kwargs["context"]
        self.assertEqual(actual.objective, "new title")
        self.assertIn("keep original scope", actual.constraints)
        self.assertIn("验收：new title", actual.constraints)
        self.assertNotIn("验收：old title", actual.constraints)
        self.assertEqual(actual.constraints.count("one page"), 1)

    def test_empty_entry_contract_does_not_revision_task(self):
        task, old, raw = self.revision()
        with self.assertRaises(ValueError):
            self.coordinator.revise(task.task_id, scope_key=task.scope_key, requester_user_id=2,
                instruction="new title", step_keys=["pdf"], expected_version=1,
                contract={**old, "acceptance": [], "outcome_checks": []})
        self.assertEqual(self.store.control(task.task_id)["revision"], 1)
        self.assertEqual(self.store.get(task.task_id).plan["contract"], old)

    async def test_resume_does_not_start_all_retained_workspaces(self):
        task = self.submit()
        step = TaskStep("pdf", "document", "finish PDF", "PDF")
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="gpt-5.6-luna")
        self.store.save_agent_session(task.task_id, run.run_id,
            [{"role": "user", "content": "continue current workspace"}],
            scope_key=task.scope_key, requester_user_id=task.requester_user_id,
            model_profile="gpt-5.6-luna", expected_version=0)
        workspaces = Mock()
        workspaces.restore_step = AsyncMock(side_effect=RuntimeError("quota exhausted by old revisions"))
        answer = json.dumps({"status": "success", "summary": "done", "artifacts": []})
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=AsyncMock(return_value=answer)) as model:
            outcome = await self.coordinator._run_step(task, step, run, context=self.packet,
                upstream={}, selected_profile=self.catalog.default, tools_by_name={},
                execute_tool=AsyncMock(), hooks=AgentExecutionHooks(workspaces=workspaces))
        model.assert_awaited_once()
        workspaces.restore_step.assert_not_awaited()
        self.assertNotEqual(outcome.state, "failed")
