"""Recovery hooks resolve the bound task, not the currently focused project."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.projects import ProjectAccess, ProjectStore
from src.plugins.ai_chat.project_task_inputs import inputs_for_task, project_output_contract
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.subagents import SubAgentStore
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests import test_project_context as fixture
from tests.handler_fixture import HandlerTestCaseMixin
from tests.postgres_fixture import postgres_for_test
from tests.test_routine_tools import event
from tests.test_project_tasks import key, task_packet


class ProjectTaskHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.ProjectContextTests.asyncSetUp(self)
        self.app.subagent_store = SubAgentStore(self.app.database)
        self.addCleanup(self.app.subagent_store.close)
        self.runtime.store.snapshot(self.access, self.pid, [ProjectFile("index.html", b"original")],
            expected_version=self.runtime.store.get(self.access, self.pid)["version"])
        self.runtime.store.open(self.access, self.pid)
        self.binding = ProjectTaskStore(self.runtime.store, self.app.subagent_store)
        self.task = self.binding.prepare(self.access, self.pid,
            expected_version=self.runtime.store.get(self.access, self.pid)["version"], base_version=1,
            request_key=key("recovery-hooks"), packet=task_packet(self.access), decision=None,
            dispatch={"bot_id": "301"}, max_parallelism=2, max_steps=4)

    async def test_focus_switch_and_expiry_do_not_change_recovered_baseline(self):
        other = self.runtime.store.create(self.access, "Unrelated project")
        self.runtime.store.open(self.access, other["project_id"])
        with patch.object(self.runtime.store, "current", side_effect=AssertionError("must not resolve focus")):
            inputs = await inputs_for_task(self.context, self.task.task_id)
            self.assertEqual((await inputs.index(self.task.task_id))["project_id"], self.pid)
        self.assertEqual(self.runtime.store.get(self.access, self.pid)["locked_task_id"], self.task.task_id)
        self.assertEqual(self.docker.serial, 0)

    async def test_disconnected_or_disabled_runtime_defers_without_fallback(self):
        self.bots.clear()
        with self.assertRaises(JobDeferred):
            await inputs_for_task(self.context, self.task.task_id)
        self.app.settings = replace(self.app.settings, projects_enabled=False)
        with self.assertRaises(JobDeferred):
            await inputs_for_task(self.context, self.task.task_id)
        self.assertEqual(self.docker.serial, 0)

    async def test_foreign_scope_actor_bot_and_revoked_allowlist_are_rejected(self):
        for fields in ({"group_id": 999}, {"user_id": 202}, {"self_id": 302}):
            context = SimpleNamespace(app=self.app, event=self.context.event.model_copy(update=fields))
            with self.subTest(fields=fields), self.assertRaises((PermissionError, JobDeferred)):
                await inputs_for_task(context, self.task.task_id)
        self.app.settings = replace(self.app.settings, sandbox_allowed_users=set())
        with self.assertRaises(PermissionError):
            await inputs_for_task(self.context, self.task.task_id)

    async def test_nonproject_tasks_need_no_project_runtime(self):
        task = self.app.subagent_store.prepare_task(packet=task_packet(self.access), decision=None,
            dispatch={"bot_id": "301"}, max_parallelism=2, max_steps=4)
        self.app.project_runtime = None
        self.assertIsNone(await inputs_for_task(self.context, task.task_id))

    def test_contract_explains_explicit_deletions_and_host_materialization(self):
        text = project_output_contract()
        for value in ("gaoji-project-update.zip", "gaoji-project-update.json", "delete", "base_manifest_sha256",
                      "SHA256", "rebuild", "宿主恢复完成回执"):
            self.assertIn(value, text)


class ProjectTaskTurnTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = self.app_context
        database = postgres_for_test(self)
        self.app.settings = replace(self.app.settings, projects_enabled=True,
            sandbox_enabled=True, sandbox_allowed_users={201}, subagent_entry_enabled=False)
        self.store = ProjectStore(database, self.app.state_dir / "projects")
        self.ledger = MessageLedger(database)
        self.tasks = SubAgentStore(database)
        for store in (self.store, self.ledger, self.tasks):
            self.addCleanup(store.close)
        self.app.message_ledger = self.ledger
        self.app.subagent_store = self.tasks
        self.message = event()
        principal = self.ledger.ensure_principal_identity("onebot-v11", "201", "A")
        self.access = ProjectAccess(scope_from_event(self.message), principal, member=True)
        project = self.store.create(self.access, "Original project")
        self.pid = project["project_id"]
        self.store.snapshot(self.access, self.pid, [ProjectFile("index.html", b"original")],
                            expected_version=project["version"])
        self.task = ProjectTaskStore(self.store, self.tasks).prepare(self.access, self.pid,
            expected_version=self.store.get(self.access, self.pid)["version"], base_version=1,
            request_key=key("chat-recovery"), packet=task_packet(self.access), decision=None,
            dispatch={"bot_id": "301"}, max_parallelism=2, max_steps=4)
        self.app.project_runtime = SimpleNamespace(ready=True, closed=False, store=self.store,
            resolve_access=AsyncMock(return_value=self.access), close=AsyncMock())
        self.coordinator = SimpleNamespace(resume=AsyncMock(return_value="continued"))
        self.app.subagent_coordinator = self.coordinator
        self.model = self.enterContext(patch("src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools", new_callable=AsyncMock))

    async def test_background_chat_assembly_uses_original_binding_and_never_current_notes(self):
        other = self.store.create(self.access, "Do not inject this project")
        self.store.open(self.access, other["project_id"])
        with patch("src.plugins.ai_chat.tool_executor.current_project_context", side_effect=AssertionError("mutable focus")):
            answer = await self.handlers.tools._ask_ai(AsyncMock(), self.message, "continue",
                                                     resume_task_id=self.task.task_id)
        self.assertIn("continued", answer)
        self.model.assert_not_awaited()
        self.coordinator.resume.assert_awaited_once()
        hooks = self.coordinator.resume.await_args.kwargs["hooks"]
        self.assertEqual((await hooks.project_inputs.index(self.task.task_id))["project_id"], self.pid)

    async def test_unavailable_runtime_keeps_defer_signal_for_background_worker(self):
        self.app.project_runtime.ready = False
        with self.assertRaises(JobDeferred):
            await self.handlers.tools._ask_ai(AsyncMock(), self.message, "continue",
                                             resume_task_id=self.task.task_id)
        self.model.assert_not_awaited()
        self.coordinator.resume.assert_not_awaited()
        self.assertEqual(self.tasks.get(self.task.task_id).status, "queued")
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], self.task.task_id)


if __name__ == "__main__":
    unittest.main()
