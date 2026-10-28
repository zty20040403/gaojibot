"""Real PostgreSQL + Unix runner RPC; Docker/browser effects remain explicit fixtures."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_browser_controller import ProjectBrowserController
from src.plugins.ai_chat.project_autosave import ProjectAutosave
from src.plugins.ai_chat.project_executor import DockerProjectBackend
from src.plugins.ai_chat.project_io import BROWSER_SESSION_TOOL, ProjectIOStore
from src.plugins.ai_chat.project_usage import ProjectUsageStore
from src.plugins.ai_chat.projects import ProjectConflict, ProjectStore
from tests.test_project_browser_store import key
from tests.test_project_io import IOFixture
from tests import test_project_browser_rpc as rpc_fixture


class ProjectBrowserControllerTests(IOFixture, unittest.IsolatedAsyncioTestCase):
    desktop = False
    volume_directory = "state/project-volumes"
    spawn = rpc_fixture.BrowserRpcTests.spawn
    start_runner = rpc_fixture.BrowserRpcTests.start_runner
    close_runner = rpc_fixture.BrowserRpcTests.close_runner

    async def asyncSetUp(self):
        self.blob_root = self.root
        self.browser_owner = self.scope.key
        self.browser_project_id = self.pid
        self.browser_fence = self.project()["lifecycle_fence"] - 1
        await rpc_fixture.BrowserRpcTests.asyncSetUp(self)
        self.sql.execute("UPDATE projects SET sandbox_id=? WHERE project_id=?",
                         (self.parent["Config"]["Labels"]["qqbot.id"], self.pid))
        self.runtime = SimpleNamespace(store=self.projects, io=self.io,
            app=SimpleNamespace(sandbox_manager=self.client),
            _execution_access=AsyncMock(return_value=self.access),
            backends={"oci": DockerProjectBackend(self.client)}, lifecycle=self.lifecycle)
        self.controller = ProjectBrowserController(self.runtime)
        self.runtime.browser = self.controller
        self.grant = self.io.admit(self.access, self.pid, BROWSER_SESSION_TOOL,
            request_key=key("session"), arguments_sha256=key("config"),
            expected_version=self.project()["version"], journal_turn_id=self.turns[self.principal].turn_id,
            native_message_id=self.principal)
        self.controller.store.bind(self.access, self.grant, self.manager.namespace)

    def restart_application(self):
        projects = self.owned(ProjectStore(self.db, self.blob_root))
        self.runtime.store = projects
        self.runtime.io = ProjectIOStore(projects, ProjectUsageStore(projects))
        self.controller = ProjectBrowserController(self.runtime)
        self.runtime.browser = self.controller

    async def execute(self):
        return await self.controller.execute(self.access, self.grant, key("click"), "browser_click", {"ref": "b1"})

    def call(self):
        return self.controller.store.find(self.access, key("click"), "browser_click", {"ref": "b1"})

    async def test_real_rpc_commit_ack_recovery_and_drain_preserve_parent(self):
        await self.controller.open(self.access, self.grant)
        self.assertEqual(await self.execute(), {"text": "clicked"})
        self.restart_application()
        self.assertEqual(await self.execute(), {"text": "clicked"})
        self.engine.click.assert_awaited_once()
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "dispatched")
        self.journal.finish_turn(self.turns[self.principal].turn_id, status="succeeded")
        await self.controller.drain(self.access, self.grant)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")
        self.assertTrue(self.parent["State"]["Running"])
        self.assertEqual(len(self.docker.containers), 1)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_application_loss_before_publication_then_runner_restart_recovers_receipt(self):
        await self.controller.open(self.access, self.grant)
        with patch.object(self.controller.store, "observe", side_effect=RuntimeError("fixture DB failure")):
            with self.assertRaisesRegex(RuntimeError, "DB failure"):
                await self.execute()
        self.assertIsNone(self.controller.store.receipt(self.access, self.call())["response"])
        self.restart_application()
        await self.close_runner()
        await self.start_runner()
        result = await self.controller.recover(self.access, self.call())
        self.assertEqual(result, {"text": "clicked"})
        self.engine.click.assert_awaited_once()
        self.browser._spawn.assert_not_awaited()
        await self.controller.drain(self.access, self.grant)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")

    async def test_lost_ack_reply_repeats_only_ack_after_committed_postgres_response(self):
        await self.controller.open(self.access, self.grant)
        original = self.client.project_browser
        lost = False
        async def rpc(operation, **target):
            nonlocal lost
            if operation == "ack":
                row = self.controller.store.receipt(self.access, self.call())
                self.assertEqual(row["state"], "received")
                self.assertIsNotNone(row["response"])
            result = await original(operation, **target)
            if operation == "ack" and not lost:
                lost = True
                raise OSError("fixture lost ACK reply")
            return result
        with patch.object(self.client, "project_browser", side_effect=rpc):
            with self.assertRaisesRegex(OSError, "lost ACK"):
                await self.execute()
            self.restart_application()
            self.assertEqual(await self.controller.recover(self.access, self.call()), {"text": "clicked"})
        self.engine.click.assert_awaited_once()
        await self.controller.drain(self.access, self.grant)

    async def test_revocation_after_effect_retains_receipt_without_disclosing_result(self):
        await self.controller.open(self.access, self.grant)
        async def revoke(owner, **args):
            self.runtime._execution_access.side_effect = PermissionError("fixture revoked membership")
            return {"text": "private page"}
        self.engine.click.side_effect = revoke
        with self.assertRaisesRegex(PermissionError, "revoked"):
            await self.execute()
        self.assertEqual(self.controller.store.receipt(self.access, self.call())["state"], "received")
        self.runtime._execution_access.side_effect = None
        self.assertEqual(await self.controller.recover(self.access, self.call()), {"text": "private page"})
        self.engine.click.assert_awaited_once()
        await self.controller.drain(self.access, self.grant)

    async def test_terminal_turn_autosave_drains_browser_before_queuing_snapshot(self):
        await self.controller.open(self.access, self.grant)
        await self.execute()
        self.journal.finish_turn(self.turns[self.principal].turn_id, status="succeeded")
        autosave = ProjectAutosave(self.runtime)
        candidates = autosave.saves.due(self.now + 1)
        self.assertEqual(len(candidates), 1)
        await autosave.save_one(candidates[0], self.now + 1)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")
        run = self.sql.execute("SELECT * FROM project_lifecycle_runs WHERE action='snapshot'").fetchone()
        self.assertIsNotNone(run)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_lost_launch_publication_is_drained_after_turn_without_launching_again(self):
        with patch.object(self.controller.store, "opened", side_effect=RuntimeError("fixture lost launch publication")):
            with self.assertRaisesRegex(RuntimeError, "lost launch"):
                await self.controller.open(self.access, self.grant)
        self.journal.finish_turn(self.turns[self.principal].turn_id, status="crashed")
        self.restart_application()
        await self.controller.drain(self.access, self.grant)
        self.browser._spawn.assert_awaited_once()
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")

    async def test_closed_runner_session_cannot_admit_a_new_action(self):
        await self.controller.open(self.access, self.grant)
        await self.execute()
        await self.close_runner()
        await self.start_runner()
        with self.assertRaises(ProjectConflict):
            await self.controller.execute(self.access, self.grant, key("next"), "browser_click", {"ref": "b1"})
        self.assertIsNone(self.controller.store.find(self.access, key("next"), "browser_click", {"ref": "b1"}))
        await self.controller.drain(self.access, self.grant)
        self.engine.click.assert_awaited_once()
