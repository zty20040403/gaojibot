"""Pinned project bytes through the existing isolated readonly installer API."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.agent.execution import active_agent_step
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.project_task_inputs import ProjectTaskInputs
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.subagents import AgentExecutionHooks, SubAgentCoordinator, TaskStep
from tests import test_project_tasks as task_fixture
from tests.test_subagent_v2 import profile


class ReadonlyManagerFixture:
    def __init__(self, owner):
        self.owner = owner
        self.files = {}
        self.purpose = "task"
        self.started = []
        self.after_write = None

    async def list(self, owner):
        return [{"sandbox_id": "s123abc", "purpose": self.purpose}] if owner == self.owner else []

    async def start_owned(self, owner, sid):
        if owner != self.owner or sid != "s123abc":
            raise PermissionError("owner")
        self.started.append((owner, sid))

    async def install_readonly_file(self, owner, sid, path, content):
        if owner != self.owner or sid != "s123abc" or not path.startswith("upstream/"):
            raise PermissionError("owner")
        if path in self.files and self.files[path] != content:
            raise ValueError("readonly conflict")
        self.files[path] = content
        if self.after_write:
            self.after_write(path)


class ProjectTaskInputTests(unittest.IsolatedAsyncioTestCase):
    owned = task_fixture.ProjectTaskTests.owned
    new_store = task_fixture.ProjectTaskTests.new_store
    create = task_fixture.ProjectTaskTests.create
    snapshot = task_fixture.ProjectTaskTests.snapshot
    prepare = task_fixture.ProjectTaskTests.prepare

    def setUp(self):
        task_fixture.ProjectTaskTests.setUp(self)
        self.task = self.prepare()
        self.tasks.set_task_state(self.task.task_id, "running")
        self.task = self.tasks.get(self.task.task_id)
        self.step = TaskStep("code", "coder", "edit", "files")
        self.run = self.tasks.create_run(self.task.task_id, self.step, allowed_tools=[], model_profile="fixture")
        self.tasks.start_run(self.run.run_id)
        self.resolve = AsyncMock(side_effect=lambda: self.access)
        self.inputs = ProjectTaskInputs(self.binding, self.resolve)
        self.workspaces = self.workspace(self.step.key)

    def workspace(self, step):
        owner = f"{self.task.conversation_id}:task#{self.task.task_id}/{step}"
        return SimpleNamespace(executor=SimpleNamespace(base_owner=self.task.conversation_id, owner=owner),
                               manager=ReadonlyManagerFixture(owner))

    async def import_files(self, *, step="code", task_id=None, workspaces=None, **args):
        token = active_agent_step.set(f"task#{self.task.task_id}/{step}")
        try:
            return json.loads(await self.inputs.import_into(task_id or self.task.task_id,
                workspaces or self.workspaces, {"sandbox_id": "s123abc", **args}))
        finally:
            active_agent_step.reset(token)

    async def test_import_original_bytes_and_completion_manifest_is_readonly_and_idempotent(self):
        first = await self.import_files()
        self.assertTrue(first["read_only"])
        self.assertEqual(first["file_count"], 1)
        copied = dict(self.workspaces.manager.files)
        self.assertEqual(copied[first["path"].removeprefix("/workspace/") + "/index.html"], b"first")
        manifest = json.loads(copied[first["manifest_path"].removeprefix("/workspace/")])
        self.assertEqual(manifest, self.binding.baseline(self.access, self.task.task_id)["manifest"])
        self.assertEqual(await self.import_files(), first)
        self.assertEqual(self.workspaces.manager.files, copied)

    async def test_lost_write_response_replays_same_immutable_file(self):
        def crash(path):
            if not path.endswith("manifest.json"):
                self.workspaces.manager.after_write = None
                raise TimeoutError("response lost after import")
        self.workspaces.manager.after_write = crash
        with self.assertRaises(TimeoutError):
            await self.import_files()
        self.assertFalse(any(p.endswith("manifest.json") for p in self.workspaces.manager.files))
        result = await self.import_files()
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.workspaces.manager.files), 2)

    async def test_revocation_during_import_never_reports_completion(self):
        self.workspaces.manager.after_write = lambda _: setattr(self, "access", replace(self.access, member=False))
        with self.assertRaises(PermissionError):
            await self.import_files()
        self.assertFalse(any(p.endswith("manifest.json") for p in self.workspaces.manager.files))

    async def test_only_running_declared_step_and_its_task_sandbox_are_accepted(self):
        for step in ("unknown", "../code"):
            with self.assertRaises(ProjectConflict):
                await self.import_files(step=step)
        with self.assertRaises(PermissionError):
            await self.inputs.import_into(self.task.task_id, self.workspaces, {"sandbox_id": "s123abc"})
        with self.assertRaises(PermissionError):
            await self.import_files(task_id=self.task.task_id + 1)
        self.workspaces.manager.purpose = "project"
        with self.assertRaises(PermissionError):
            await self.import_files()
        self.assertEqual(self.workspaces.manager.started, [])
        self.assertEqual(self.workspaces.manager.files, {})

    async def test_model_cannot_choose_project_version_or_destination_path(self):
        for values in ({"project_id": self.pid}, {"path": "../../other"}, {"version_no": 2}):
            with self.assertRaises(ValueError):
                await self.import_files(**values)
        self.assertEqual(self.workspaces.manager.files, {})

    async def test_two_steps_get_independent_copies_of_same_baseline(self):
        step = TaskStep("review", "analyst", "check", "review")
        run = self.tasks.create_run(self.task.task_id, step, allowed_tools=[], model_profile="fixture")
        self.tasks.start_run(run.run_id)
        second = self.workspace("review")
        first, other = await asyncio.gather(self.import_files(), self.import_files(step="review", workspaces=second))
        self.assertEqual(first["manifest_sha256"], other["manifest_sha256"])
        self.assertEqual(self.workspaces.manager.files, second.manager.files)
        self.assertIsNot(self.workspaces.manager.files, second.manager.files)
        self.assertNotEqual(self.workspaces.executor.owner, second.executor.owner)

    async def test_focus_change_cannot_redirect_the_import(self):
        other = self.create()
        self.snapshot(other, b"other project")
        self.store.open(self.access, other)
        result = await self.import_files()
        self.assertEqual(result["project_id"], self.pid)
        self.assertIn(b"first", self.workspaces.manager.files.values())
        self.assertNotIn(b"other project", self.workspaces.manager.files.values())

    async def test_bound_worker_exposes_import_tool_and_refuses_missing_adapter(self):
        selected = profile("fixture")
        coordinator = SubAgentCoordinator(self.tasks,
            ModelCatalog({selected.name: selected}, default_profile=selected.name), logger=Mock())
        async def model(text, history, tools, execute, **kwargs):
            self.assertIn("import_project_baseline", {t["function"]["name"] for t in tools})
            self.assertIn(self.binding.baseline(self.access, self.task.task_id)["manifest_sha256"], text)
            self.assertIn("gaoji-project-update.zip", text)
            self.assertIn("独立验收人必须导入这一个精确快照和项目基线", text)
            imported = json.loads(await execute("import_project_baseline", {"sandbox_id": "s123abc"}))
            self.assertTrue(imported["ok"])
            return json.dumps({"status": "success", "summary": "read baseline", "artifacts": [], "findings": [], "risks": []})
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model) as call:
            with self.assertRaisesRegex(ValueError, "input adapter"):
                await coordinator._run_step(self.task, self.step, self.run, context=self.packet,
                    upstream={}, selected_profile=selected, tools_by_name={}, execute_tool=AsyncMock(), hooks=None)
            call.assert_not_called()
            outcome = await coordinator._run_step(self.task, self.step, self.run, context=self.packet,
                upstream={}, selected_profile=selected, tools_by_name={}, execute_tool=AsyncMock(),
                hooks=AgentExecutionHooks(workspaces=self.workspaces, project_inputs=self.inputs))
            self.assertEqual(call.call_count, 1)
            self.assertEqual(outcome.state, "success")
        self.assertEqual(len(self.workspaces.manager.files), 2)


if __name__ == "__main__":
    unittest.main()
