"""PostgreSQL/controller/VM driver composition with explicit guest fixtures."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.project_browser_controller import ProjectBrowserController
from src.plugins.ai_chat.project_io import BROWSER_SESSION_TOOL, ProjectIOStore
from src.plugins.ai_chat.project_usage import ProjectUsageStore
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.sandbox_runtime.project_browser_vm import VmProjectBrowser
from tests.project_turn_fixture import start_project_turn
from tests.test_project_browser_store import key
from tests.test_project_browser_vm import BrowserVmWireFixture
from tests.test_project_vm_executor import VmExecutorFixture


class VmBrowserControllerTests(BrowserVmWireFixture, VmExecutorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.transition("wake")
        self.browser = self.runtime.browser
        await self.start_browser_fixture(self.manager._directory(self.project()["sandbox_id"]))
        self.addAsyncCleanup(self.browser.close)
        self.ledger = MessageLedger(self.db)
        self.turns = TurnJournal(self.db)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.turns.close)
        self.turn = start_project_turn(self.ledger, self.turns, self.scope)
        usage = ProjectUsageStore(self.projects)
        usage.record(self.access, self.pid, await self.backend.sample_usage(self.project()),
                     expected_version=self.project()["version"])
        self.io = ProjectIOStore(self.projects, usage)
        self.application = SimpleNamespace(store=self.projects, io=self.io,
            app=SimpleNamespace(sandbox_manager=self.manager), backends={"vm": self.backend},
            _execution_access=AsyncMock(return_value=self.access))
        self.controller = ProjectBrowserController(self.application)
        self.grant = self.io.admit(self.access, self.pid, BROWSER_SESSION_TOOL,
            request_key=key("session"), arguments_sha256=key("bootstrap"),
            expected_version=self.project()["version"], journal_turn_id=self.turn.turn_id, native_message_id=5)
        self.controller.store.bind(self.access, self.grant, self.store.instance_id)

    async def click(self):
        return await self.controller.execute(self.access, self.grant, key("click"), "browser_click", {"ref": "b1"})

    def call(self):
        return self.controller.store.find(self.access, key("click"), "browser_click", {"ref": "b1"})

    async def test_application_replay_ack_and_drain_keep_project_vm_running(self):
        self.assertIs(self.controller._client(self.project()), self.browser)
        await self.controller.open(self.access, self.grant)
        self.assertEqual(await self.click(), {"text": "clicked"})
        self.controller = ProjectBrowserController(self.application)
        self.assertEqual(await self.click(), {"text": "clicked"})
        self.engine.click.assert_awaited_once()
        self.assertEqual(self.controller.store.receipt(self.access, self.call())["state"], "acknowledged")
        await self.controller.drain(self.access, self.grant)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")
        self.assertTrue(all(d["state"] == "running" for d in self.domains.values()))

    async def test_receipt_recovered_after_database_failure_and_driver_restart(self):
        await self.controller.open(self.access, self.grant)
        with patch.object(self.controller.store, "observe", side_effect=OSError("fixture DB failure")):
            with self.assertRaises(OSError):
                await self.click()
        sid = self.controller.store.session(self.access, self.grant)["session_id"]
        await self.browser.host.channels.pop(sid).eof()
        self.runtime._browser = VmProjectBrowser(self.runtime)
        self.controller = ProjectBrowserController(self.application)
        self.assertEqual(await self.controller.recover(self.access, self.call()), {"text": "clicked"})
        self.engine.click.assert_awaited_once()
        await self.controller.drain(self.access, self.grant)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")
        self.assertEqual(sum(c["action"] == "start" for c in self.controls), 1)

    async def test_lost_launch_publication_uses_same_vm_session(self):
        with patch.object(self.controller.store, "opened", side_effect=OSError("fixture DB lost launch")):
            with self.assertRaises(OSError):
                await self.controller.open(self.access, self.grant)
        self.controller = ProjectBrowserController(self.application)
        await self.controller.open(self.access, self.grant)
        self.assertEqual(await self.click(), {"text": "clicked"})
        await self.controller.drain(self.access, self.grant)
        self.assertEqual(sum(c["action"] == "start" for c in self.controls), 1)

    async def test_revoked_or_cross_scope_access_never_discloses_page(self):
        await self.controller.open(self.access, self.grant)
        self.application._execution_access.side_effect = PermissionError("revoked")
        with self.assertRaises(PermissionError):
            await self.click()
        self.application._execution_access.side_effect = None
        self.application._execution_access.return_value = replace(self.access,
            scope=replace(self.scope, native_conversation_id="other"))
        with self.assertRaises(PermissionError):
            await self.click()
        self.engine.click.assert_not_awaited()
        self.application._execution_access.return_value = self.access
        await self.controller.drain(self.access, self.grant)

    async def test_lifecycle_drains_vm_browser_before_snapshot_and_guest_shutdown(self):
        self.application.lifecycle = self.lifecycle
        self.executor.browser = self.controller
        await self.controller.open(self.access, self.grant)
        await self.click()
        run = self.lifecycle.request(self.access, self.pid, "stop",
            expected_version=self.project()["version"], request_key="vm-browser-stop",
            browser_turn_id=self.turn.turn_id)
        self.assertEqual(run["phase"], "browser_drain")
        result = await self.executor(self.claim())
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")
        self.assertTrue(all(d["state"] == "shut off" for d in self.domains.values()))
        self.engine.click.assert_awaited_once()
