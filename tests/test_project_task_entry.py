"""Project chat admission with real PG/CAS and fixture-only models/hosts."""

from copy import deepcopy
from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.agent.execution import EntryDecision, active_agent_step
from src.plugins.ai_chat.agent.registry import DEFAULT_AGENT_REGISTRY
from src.plugins.ai_chat.application import ChatFailure
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.project_executor import _store
from src.plugins.ai_chat.project_task_entry import submit_project_task, entry_receipt
from src.plugins.ai_chat.project_task_plan import integrate_decision, INTEGRATION_STEP, INTEGRATION_TOOLS
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.subagents import SubAgentCoordinator
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests import test_project_task_admission as fixture
from tests.handler_fixture import HandlerTestCaseMixin
from tests.test_routine_tools import event
from tests.test_subagent_v2 import decision, profile


def saved_plan(store, task_id):
    return next(row["state"]["entry_decision"] for row in reversed(store.checkpoints(task_id))
                if row["state"].get("entry_decision"))


class ProjectPlanTests(unittest.TestCase):
    def integrate(self, raw, **changes):
        options = {"max_steps": 6, "registry": DEFAULT_AGENT_REGISTRY, **changes}
        return integrate_decision(EntryDecision.parse(raw, max_steps=12), **options)

    def test_one_required_integrator_preserves_parallel_business_dag_and_contract(self):
        raw = decision("workflow")
        raw["steps"][0]["optional"] = True
        original = deepcopy(raw)
        result = self.integrate(raw)
        self.assertEqual(raw, original)
        self.assertEqual(list(result.steps[:-1]), raw["steps"])
        final = result.steps[-1]
        self.assertEqual(final["id"], INTEGRATION_STEP)
        self.assertFalse(final["optional"])
        self.assertEqual(final["depends_on"], [s["id"] for s in raw["steps"]])
        self.assertEqual(result.contract.acceptance[:-1], tuple(raw["acceptance"]))
        self.assertEqual(result.contract.outcome_checks[:-1], tuple(raw["outcome_checks"]))
        self.assertFalse(result.contract.delivery_required)
        self.assertEqual(EntryDecision.from_payload(result.as_payload(), max_steps=6), result)

    def test_delegate_gets_integrator_and_preserves_explicit_attachment_requirement(self):
        raw = decision("delegate")
        raw["steps"][0]["agent"] = "researcher"
        raw["delivery_required"] = True
        result = self.integrate(raw, max_steps=2)
        self.assertEqual(result.mode, "workflow")
        self.assertEqual([s["agent"] for s in result.steps], ["researcher", "coder"])
        self.assertTrue(result.contract.delivery_required)

    def test_no_silent_step_or_contract_truncation(self):
        with self.assertRaises(ValueError):
            self.integrate(decision("workflow"), max_steps=3)
        with self.assertRaises(ValueError):
            self.integrate(decision("delegate"), max_steps=1)
        for field in ("deliverables", "constraints", "acceptance"):
            raw = decision("delegate")
            raw[field] = [f"requirement {i}" for i in range(16)]
            if field == "acceptance":
                raw["outcome_checks"] = [{"criterion_index": i, "kind": "evidence"} for i in range(16)]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.integrate(raw)

    def test_reserved_id_and_missing_integrator_capability_are_rejected(self):
        raw = decision("delegate")
        raw["steps"][0]["id"] = INTEGRATION_STEP
        with self.assertRaises(ValueError):
            self.integrate(raw)
        registry = SimpleNamespace(worker_roles=DEFAULT_AGENT_REGISTRY.worker_roles,
            worker=lambda role: replace(DEFAULT_AGENT_REGISTRY.worker(role), allowed_tools=frozenset()))
        with self.assertRaisesRegex(ValueError, "cannot use"):
            self.integrate(decision("delegate"), registry=registry)


class ProjectEntryTests(unittest.IsolatedAsyncioTestCase):
    wake = fixture.ProjectTaskAdmissionTests.wake
    claim = fixture.ProjectTaskAdmissionTests.claim
    count = fixture.ProjectTaskAdmissionTests.count
    setup_admission = fixture.ProjectTaskAdmissionTests.setup_admission
    request = fixture.ProjectTaskAdmissionTests.request

    async def asyncSetUp(self):
        await fixture.ProjectTaskAdmissionTests.asyncSetUp(self)
        self.configure_entry(SimpleNamespace(db_call=_store, settings=SimpleNamespace(projects_enabled=True,
            is_sandbox_user_allowed=lambda _: True)))

    def configure_entry(self, app):
        self.catalog = ModelCatalog({"fixture": profile("fixture")}, default_profile="fixture")
        self.coordinator = SubAgentCoordinator(self.tasks, self.catalog, logger=Mock(), max_steps=6)
        self.coordinator.dispatcher = Mock()
        self.planner = self.enterContext(patch.object(self.coordinator, "_supervisor_json",
            new=AsyncMock(return_value=decision("workflow"))))
        self.runtime = SimpleNamespace(ready=True, closed=False, store=self.projects,
            task_admission=self.admission, backends={"oci": self.backend},
            resolve_access=AsyncMock(return_value=self.access), close=AsyncMock())
        app.project_runtime = self.runtime
        app.subagent_coordinator = self.coordinator
        app.subagent_store = self.tasks
        app.message_ledger = self.ledger
        app.turn_journal = self.journal
        self.app = app
        self.context = SimpleNamespace(app=app, event=event(), capabilities={"projects", "subagents"},
            routine_run_id="", journal_turn_id=self.turn.turn_id)
        self.packet_factory = AsyncMock(return_value=self.packet)

    async def submit(self, **changes):
        return await submit_project_task(self.context, **{
            "objective": self.packet.objective, "role": None, "packet_factory": self.packet_factory,
            "selected_profile": self.catalog.default, "available_tools": INTEGRATION_TOOLS, **changes})

    async def test_current_project_prepares_saved_baseline_and_durable_integrating_task(self):
        root = await self.wake()
        (root / "index.html").write_bytes(b"unsaved current file")
        receipt = await self.submit()
        self.assertEqual(receipt["status"], "preparing")
        self.assertIsNone(receipt["task_id"])
        self.assertIn("保存核实后", receipt["message"])
        self.assertEqual(self.count("subagent_tasks"), 0)
        self.assertIn("最多规划 5", self.planner.await_args.args[0])
        result = await self.executor(self.claim())
        task = self.tasks.get(result["task_id"])
        self.assertEqual(task.project_id, self.pid)
        self.assertEqual(saved_plan(self.tasks, task.task_id)["steps"][-1]["id"], INTEGRATION_STEP)
        self.assertEqual(self.admission.bindings.read_baseline_file(self.access, task.task_id, "index.html")["content"],
                         b"unsaved current file")
        self.assertIn(task.task_id, self.tasks.dispatchable_tasks())
        self.coordinator.dispatcher.enqueue.assert_not_called()
        self.planner.side_effect = AssertionError("Recovery must use the persisted plan")
        with patch.object(self.coordinator, "_execute_workflow", new=AsyncMock(return_value="fixture")) as execute:
            await self.coordinator._resume_task(task, context=self.packet,
                selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock(),
                parent_trace=None, progress=None, hooks=None)
        self.assertEqual(self.tasks.get(task.task_id).plan["steps"][-1]["id"], INTEGRATION_STEP)
        self.assertEqual(execute.await_args.kwargs["steps"][-1].key, INTEGRATION_STEP)

    async def test_replay_after_focus_change_and_turn_finish_never_calls_planner_again(self):
        first = await self.submit()
        other = self.projects.create(self.access, "Different")["project_id"]
        self.projects.open(self.access, other)
        self.journal.finish_turn(self.turn.turn_id, status="succeeded")
        self.planner.side_effect = AssertionError("Do not replan")
        self.packet_factory.side_effect = AssertionError("Do not rebuild context")
        self.assertEqual(await self.submit(), first)
        result = await self.executor(self.claim())
        replay = await self.submit()
        self.assertEqual(replay["task_id"], result["task_id"])
        self.assertEqual(replay["status"], "bound")
        with self.assertRaises(ProjectConflict):
            await self.submit(objective="A different request")
        with self.assertRaises(ProjectConflict):
            await self.submit(role="coder")
        self.assertEqual(self.count("subagent_tasks"), 1)

    async def test_concurrent_planners_keep_first_committed_task_envelope(self):
        arrived, ready = 0, asyncio.Event()
        async def plan(*args, **kwargs):
            nonlocal arrived
            arrived += 1
            raw = decision("workflow")
            raw["reason"] = f"Planner attempt {arrived}"
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            return raw
        self.planner.side_effect = plan
        receipts = await asyncio.gather(self.submit(), self.submit())
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.count("durable_jobs"), 1)
        await self.executor(self.claim())
        self.assertEqual(self.count("subagent_tasks"), 1)

    async def test_no_project_and_routine_or_worker_context_preserve_existing_routes(self):
        self.sql.execute("DELETE FROM project_focus")
        self.assertIsNone(await self.submit())
        self.projects.open(self.access, self.pid)
        self.context.routine_run_id = "routine-1"
        self.assertIsNone(await self.submit())
        self.context.routine_run_id = ""
        token = active_agent_step.set("task#1/worker")
        try:
            self.assertIsNone(await self.submit())
        finally:
            active_agent_step.reset(token)
        self.planner.assert_not_awaited()

    async def test_unavailable_runtime_and_dispatcher_do_not_create_ordinary_tasks(self):
        self.runtime.ready = False
        with self.assertRaises(JobDeferred):
            await self.submit()
        self.runtime.ready = True
        self.coordinator.dispatcher = None
        with self.assertRaises(JobDeferred):
            await self.submit()
        self.planner.assert_not_awaited()
        self.assertEqual(self.count("subagent_tasks"), 0)

    async def test_focus_switch_or_revocation_during_planning_rejects_admission(self):
        async def changed(*args, **kwargs):
            other = self.projects.create(self.access, "Changed during planning")["project_id"]
            self.projects.open(self.access, other)
            return decision("workflow")
        self.planner.side_effect = changed
        with self.assertRaises(ProjectConflict):
            await self.submit()
        self.projects.open(self.access, self.pid)
        async def revoked(*args, **kwargs):
            self.runtime.resolve_access.return_value = replace(self.access, member=False)
            return decision("workflow")
        self.planner.side_effect = revoked
        with self.assertRaises(PermissionError):
            await self.submit()
        self.assertEqual(self.count("durable_jobs"), 0)

    async def test_known_automatic_decision_does_not_make_second_planning_call(self):
        await self.submit(decision=EntryDecision.parse(decision("workflow")))
        self.planner.assert_not_awaited()
        result = await self.executor(self.claim())
        self.assertEqual(saved_plan(self.tasks, result["task_id"])["steps"][-1]["id"], INTEGRATION_STEP)

    async def test_missing_or_disabled_tools_fail_before_planning_or_freezing(self):
        with self.assertRaisesRegex(PermissionError, "sandbox_create"):
            await self.submit(available_tools=INTEGRATION_TOOLS - {"sandbox_create"})
        with patch("src.plugins.ai_chat.project_task_entry.tool_enabled", return_value=False):
            with self.assertRaises(PermissionError):
                await self.submit()
        self.planner.assert_not_awaited()
        self.assertEqual(self.count("durable_jobs"), 0)

    async def test_two_step_budget_and_bad_planner_output_never_modify_shared_limits(self):
        self.coordinator.max_steps = 2
        self.planner.side_effect = [decision("workflow"), decision("delegate")]
        await self.submit()
        self.assertEqual(self.coordinator.max_steps, 2)
        self.assertEqual(self.planner.await_count, 2)
        result = await self.executor(self.claim())
        self.assertEqual(len(saved_plan(self.tasks, result["task_id"])["steps"]), 2)

    async def test_explicit_delegate_keeps_role_and_gains_integration(self):
        raw = decision("delegate")
        raw["steps"][0]["agent"] = "researcher"
        self.planner.return_value = raw
        await self.submit(role="researcher")
        result = await self.executor(self.claim())
        self.assertEqual([s["agent"] for s in saved_plan(self.tasks, result["task_id"])["steps"]],
                         ["researcher", "coder"])

    def test_failed_preparation_receipt_does_not_claim_running_task(self):
        value = entry_receipt({"project_id": self.pid, "job_id": 1,
            "status": "failed", "task_bound": False, "task_id": None})
        self.assertEqual(value["status"], "needs_attention")
        self.assertIn("尚未确认启动", value["message"])


class ProjectEntryChatTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    setup_admission = fixture.ProjectTaskAdmissionTests.setup_admission
    configure_entry = ProjectEntryTests.configure_entry
    claim = fixture.ProjectTaskAdmissionTests.claim

    async def asyncSetUp(self):
        await fixture.ProjectTaskAdmissionTests.asyncSetUp(self)
        app = self.app_context
        app.settings = replace(app.settings, projects_enabled=True, sandbox_enabled=True,
            sandbox_allowed_users={201}, subagent_entry_enabled=False, tool_intent_enabled=False)
        app.sandbox_manager = SimpleNamespace(close=AsyncMock())
        self.configure_entry(app)

    async def test_task_command_uses_project_preparation_not_coordinator_submit(self):
        bot = AsyncMock(self_id="301")
        with patch.object(self.coordinator, "submit", side_effect=AssertionError("ordinary task")):
            answer = await self.handlers.tools._ask_ai(bot, event(), self.packet.objective,
                task_mode=True, journal_turn_id=self.turn.turn_id)
        self.assertIn("保存核实后", answer)
        result = await self.executor(self.claim())
        self.assertEqual(self.tasks.get(result["task_id"]).project_id, self.pid)

    async def test_registry_tool_entry_uses_same_project_path(self):
        async def model(_text, _history, _tools, execute_tool, **kwargs):
            value = json.loads(await execute_tool("run_subagents", {"goal": self.packet.objective}))
            self.assertTrue(value["ok"], value)
            self.assertIn("保存核实后", value["result"])
            return value["result"]
        with patch("src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools", side_effect=model):
            answer = await self.handlers.tools._ask_ai(AsyncMock(self_id="301"), event(), self.packet.objective,
                journal_turn_id=self.turn.turn_id)
        self.assertIn("保存核实后", answer)
        result = await self.executor(self.claim())
        self.assertEqual(saved_plan(self.tasks, result["task_id"])["steps"][-1]["id"], INTEGRATION_STEP)

    async def test_admission_failure_is_not_reported_as_model_outage(self):
        with patch("src.plugins.ai_chat.project_task_entry.tool_enabled", return_value=False):
            with self.assertRaises(ChatFailure) as raised:
                await self.handlers.tools._ask_ai(AsyncMock(self_id="301"), event(), self.packet.objective,
                    task_mode=True, journal_turn_id=self.turn.turn_id)
        self.assertEqual(raised.exception.code, "project_task_request")
        self.assertEqual(self.sql.execute("SELECT count(*) FROM durable_jobs").fetchone()[0], 0)
        self.planner.assert_not_awaited()

    async def test_automatic_entry_keeps_step_slot_and_does_not_replan_or_claim_started(self):
        self.app.settings = replace(self.app.settings, subagent_entry_enabled=True)
        async def model(_text, _history, _tools, execute_tool, **kwargs):
            self.assertEqual(kwargs["entry_max_steps"], 5)
            self.assertIsNotNone(kwargs["entry_handler"])
            return await kwargs["entry_handler"](EntryDecision.parse(decision("delegate")))
        with patch("src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools", side_effect=model):
            answer = await self.handlers.tools._ask_ai(AsyncMock(self_id="301"), event(), self.packet.objective,
                journal_turn_id=self.turn.turn_id)
        self.assertIn("保存核实后", answer)
        self.planner.assert_not_awaited()
        result = await self.executor(self.claim())
        self.assertEqual(len(saved_plan(self.tasks, result["task_id"])["steps"]), 2)
