"""Real PostgreSQL, private runner RPC and files; browser/kernel are fixtures."""

import time
import unittest
from unittest.mock import patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.project_executor import ProjectLifecycleExecutor
from src.plugins.ai_chat.project_file_executor import ProjectFileExecutor
from src.plugins.ai_chat.project_maintenance import ProjectMaintenance
from src.plugins.ai_chat.project_task_admission import ProjectTaskAdmission
from src.plugins.ai_chat.project_turn_saves import ProjectTurnSaves
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.subagents import SubAgentStore
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests import test_project_browser_controller as fixture
from tests.test_project_browser_store import key
from tests.project_turn_fixture import start_project_turn
from tests.test_project_restore import fixture_mount_contents


class ProjectBrowserDrainTests(fixture.BrowserControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sql.execute("UPDATE projects SET quota_bytes=? WHERE project_id=?", (64 * 1024**2, self.pid))
        self.backend = self.runtime.backends["oci"]
        self.backend.files = ProjectFileExecutor(self.lifecycle, self.client, resolve_access=lambda *_: self.access)
        self.executor = self.make_executor()
        self.files = self.volumes.binding(self.scope.key, self.pid)
        (self.files / "note.txt").write_bytes(b"retained source")
        fixture_mount_contents(self)

    def make_executor(self, browser=True):
        return ProjectLifecycleExecutor(self.lifecycle, {"oci": self.backend},
            resolve_access=self.runtime._execution_access, browser=self.controller if browser else None)

    def request(self, action="snapshot", **changes):
        options = dict(expected_version=self.project()["version"], request_key="browser-" + action,
                       browser_turn_id=self.turns[self.principal].turn_id)
        options.update(changes)
        return self.lifecycle.request(self.access, self.pid, action, **options)

    def claim(self, takeover=False):
        return self.jobs.claim_due("successor" if takeover else "worker", kinds=("project.lifecycle",),
                                  now=int(time.time()) + (61 if takeover else 0))[0]

    async def open_browser(self):
        await self.controller.open(self.access, self.grant)
        await self.execute()

    def assert_waiting(self, run, generation=1):
        self.assertEqual(self.project()["lifecycle_job_id"], run["job_id"])
        self.assertEqual(self.project()["workspace_generation"], generation)
        self.assertEqual(self.sql.execute("SELECT count(*) FROM project_lifecycle_steps WHERE job_id=?",
                                         (run["job_id"],)).fetchone()[0], 0)

    async def test_running_turn_snapshot_drains_before_real_file_snapshot(self):
        await self.open_browser()
        run = self.request()
        job = self.claim()
        self.assert_waiting(run)
        with self.assertRaisesRegex(ProjectConflict, "Drain"):
            self.lifecycle.next_step(job)
        with self.assertRaises(ProjectConflict):
            await self.controller.execute(self.access, self.grant, key("late"), "browser_click", {"ref": "b1"})
        with self.assertRaises(ProjectConflict):
            self.projects.update(self.access, self.pid, expected_version=self.project()["version"], title="race")
        result = await self.executor(job)
        self.assertEqual(result["state"], "active")
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "note.txt"), b"retained source")
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")
        self.engine.click.assert_awaited_once()
        self.assertEqual(len(self.docker.containers), 1)
        self.assertTrue(ProjectTurnSaves(self.projects).status(self.access, self.pid)[0]["saved"])

    async def test_reserved_browser_aborts_without_launching_before_snapshot(self):
        self.request()
        await self.executor(self.claim())
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "aborted")
        self.browser._spawn.assert_not_awaited()
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "note.txt"), b"retained source")

    async def test_unknown_click_is_not_repeated_or_treated_as_shutdown(self):
        await self.controller.open(self.access, self.grant)
        self.engine.click.side_effect = RuntimeError("fixture unknown outcome")
        with self.assertRaises(Exception):
            await self.execute()
        run = self.request()
        with self.assertRaises(ProjectConflict):
            await self.executor(self.claim())
        self.assert_waiting(run)
        self.engine.click.assert_awaited_once()
        self.assertEqual(self.projects.versions(self.access, self.pid), [])

    async def test_restart_after_closed_receipt_before_prelude_commit_recovers(self):
        await self.open_browser()
        run = self.request()
        with patch("src.plugins.ai_chat.project_browser_drain.finish", side_effect=OSError("fixture commit loss")):
            with self.assertRaisesRegex(OSError, "commit loss"):
                await self.executor(self.claim())
        self.assert_waiting(run)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")
        self.restart_application()
        self.executor = self.make_executor()
        await self.executor(self.claim(takeover=True))
        self.engine.click.assert_awaited_once()
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 1)

    async def test_lost_shutdown_reply_is_observed_not_relaunched(self):
        await self.open_browser()
        run = self.request()
        rpc = self.client.project_browser
        lost = False

        async def lose(operation, **target):
            nonlocal lost
            result = await rpc(operation, **target)
            if operation == "drain" and not lost:
                lost = True
                raise OSError("fixture shutdown reply lost")
            return result

        with patch.object(self.client, "project_browser", side_effect=lose):
            with self.assertRaises(OSError):
                await self.executor(self.claim())
            self.assert_waiting(run)
            self.assertEqual(len(self.docker.containers), 1)
            await self.executor(self.claim(takeover=True))
        self.browser._spawn.assert_awaited_once()
        self.engine.click.assert_awaited_once()

    async def test_pending_completed_action_is_recovered_before_drain(self):
        await self.controller.open(self.access, self.grant)
        with patch.object(self.controller.store, "observe", side_effect=OSError("fixture DB reply loss")):
            with self.assertRaises(OSError):
                await self.execute()
        self.request()
        await self.executor(self.claim())
        self.engine.click.assert_awaited_once()
        self.assertEqual(self.controller.store.receipt(self.access, self.call())["state"], "acknowledged")

    async def test_missing_adapter_defers_without_physical_mutation(self):
        await self.open_browser()
        run = self.request()
        with self.assertRaises(JobDeferred):
            await self.make_executor(browser=False)(self.claim())
        self.assert_waiting(run)
        self.assertEqual(len(self.docker.containers), 2)

    async def test_revoked_access_prevents_shutdown_and_snapshot(self):
        await self.open_browser()
        run = self.request()
        self.runtime._execution_access.side_effect = PermissionError("fixture revoked")
        with self.assertRaises(PermissionError):
            await self.executor(self.claim())
        self.assert_waiting(run)
        self.assertEqual(len(self.docker.containers), 2)

    async def test_expired_worker_cannot_commit_late_shutdown_receipt(self):
        await self.open_browser()
        run = self.request()
        rpc = self.client.project_browser

        async def expire(operation, **target):
            result = await rpc(operation, **target)
            if operation == "drain":
                self.sql.execute("UPDATE durable_jobs SET lease_until=0 WHERE job_id=?", (run["job_id"],))
            return result

        with patch.object(self.client, "project_browser", side_effect=expire):
            with self.assertRaises(ProjectConflict):
                await self.executor(self.claim())
        self.assert_waiting(run)
        self.assertEqual(self.controller.store.session(self.access, self.grant)["state"], "draining")
        await self.executor(self.claim(takeover=True))
        self.engine.click.assert_awaited_once()

    async def test_busy_workspace_after_browser_exit_keeps_lifecycle_locked(self):
        await self.open_browser()
        run = self.request()
        self.docker.processes = "PID\n1001\n9999\n"
        with self.assertRaises(ProjectConflict):
            await self.executor(self.claim())
        self.assert_waiting(run)
        self.assertEqual(self.controller.store.session(self.access, self.grant)["state"], "closed")
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "dispatched")
        self.docker.processes = None
        await self.executor(self.claim(takeover=True))

    async def test_another_running_turn_cannot_take_browser(self):
        await self.open_browser()
        other = start_project_turn(self.ledger, self.journal, self.scope, message_id=987)
        with self.assertRaisesRegex(ProjectConflict, "Another running turn"):
            self.request(browser_turn_id=other.turn_id)
        with self.assertRaisesRegex(ProjectConflict, "Another running turn"):
            self.request(browser_turn_id=None)
        self.assertIsNone(self.project()["lifecycle_job_id"])

    async def test_idle_maintenance_enqueues_drain_not_pruning_open_browser(self):
        await self.open_browser()
        self.journal.finish_turn(self.turns[self.principal].turn_id, status="succeeded")
        self.sql.execute("UPDATE projects SET last_active_at=? WHERE project_id=?", (self.now - 3600, self.pid))
        maintenance = ProjectMaintenance(self.lifecycle, self.runtime.backends,
            resolve_access=self.runtime._execution_access, enabled=True)
        with patch.object(self.projects, "prune_versions", side_effect=AssertionError("must drain first")):
            results = await maintenance.run_once(now=self.now)
        self.assertEqual(results[0]["action"], "stop")
        await self.executor(self.claim())
        self.assertEqual(self.project()["status"], "stopped")
        self.assertTrue(ProjectTurnSaves(self.projects).status(self.access, self.pid)[0]["saved"])

    async def test_restore_keeps_original_epoch_until_browser_closed_and_saves_original_files(self):
        # Establish a baseline before the browser-owned mutation.
        self.io.abort(self.grant)
        self.sql.execute("UPDATE project_turn_saves SET state='skipped' WHERE project_id=?", (self.pid,))
        self.request(request_key="baseline")
        await self.executor(self.claim())
        self.record(self.sample(100))
        self.grant = self.io.admit(self.access, self.pid, "project_browser_session",
            request_key=key("replacement session"), arguments_sha256=key("config"),
            expected_version=self.project()["version"], journal_turn_id=self.turns[self.principal].turn_id,
            native_message_id=self.principal)
        self.controller.store.bind(self.access, self.grant, self.manager.namespace)
        self.identity["fence"] = self.project()["lifecycle_fence"] - 1
        await self.open_browser()
        (self.files / "note.txt").write_bytes(b"unsaved browser-era source")
        run = self.request("restore", restore_version=1)
        self.assert_waiting(run)
        result = await self.executor(self.claim())
        self.assertEqual(self.project()["workspace_generation"], 2)
        self.assertEqual(result["snapshot"]["version_no"], 3)
        self.assertEqual(result["saved_snapshot"]["version_no"], 2)
        self.assertEqual(self.projects.read_file(self.access, self.pid, 2, "note.txt"), b"unsaved browser-era source")
        self.assertEqual(ProjectTurnSaves(self.projects).status(self.access, self.pid)[0]["version"], "v2")
        restored = self.volumes.binding(self.scope.key, self.pid, generation=2)
        self.assertEqual((restored / "note.txt").read_bytes(), b"retained source")

    async def test_same_turn_task_preparation_drains_then_binds_saved_baseline(self):
        await self.open_browser()
        tasks = self.owned(SubAgentStore(self.db))
        admission = ProjectTaskAdmission(self.lifecycle, tasks)
        self.lifecycle.task_admission = admission
        turn = self.turns[self.principal]
        packet = ContextPacket(self.scope.key, self.scope.key + ":user:201", 201,
                               turn.trigger_canonical_message_id, "Update retained source")
        run = admission.request(self.access, request_key=key("task"), turn_id=turn.turn_id,
            packet=packet, decision=None, dispatch={"bot_id": "301"}, max_parallelism=2,
            max_steps=4, timeout_seconds=600, preflight=self.backend.preflight)
        self.assertFalse(run["task_bound"])
        self.assert_waiting(run)
        result = await self.executor(self.claim())
        self.assertTrue(result["task_bound"])
        self.assertEqual(self.project()["locked_task_id"], result["task_id"])
        self.assertEqual(admission.bindings.read_baseline_file(self.access, result["task_id"], "note.txt")["content"],
                         b"retained source")
        self.engine.click.assert_awaited_once()

    async def test_uncommitted_prelude_finish_rolls_back_marker_and_generation(self):
        await self.open_browser()
        run = self.request()
        event = self.projects._event

        def fail(cursor, pid, access, kind, *args):
            event(cursor, pid, access, kind, *args)
            if kind == "browser_drained":
                raise OSError("fixture transaction rollback")

        with patch.object(self.projects, "_event", side_effect=fail):
            with self.assertRaisesRegex(OSError, "transaction rollback"):
                await self.executor(self.claim())
        self.assert_waiting(run)
        stored = self.sql.execute("SELECT plan FROM project_lifecycle_runs WHERE job_id=?", (run["job_id"],)).fetchone()
        self.assertNotIn("completed", stored["plan"]["browser_drain"])
        await self.executor(self.claim(takeover=True))
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 1)

    async def test_editor_can_close_finished_browser_without_reading_its_page(self):
        await self.open_browser()
        self.journal.finish_turn(self.turns[self.principal].turn_id, status="succeeded")
        self.projects.set_member(self.access, self.pid, self.b.principal_id, "editor",
                                 expected_version=self.project()["version"])
        run = self.lifecycle.request(self.b, self.pid, "snapshot", expected_version=self.project()["version"],
                                     request_key="editor-save")
        self.assertEqual(run["phase"], "browser_drain")
        with self.assertRaises(PermissionError):
            self.controller.store.receipt(self.b, self.call())
        self.runtime._execution_access.return_value = self.b
        self.backend.files.resolve_access = lambda *_: self.b
        result = await self.executor(self.claim())
        self.assertNotIn("clicked", str(result))
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "completed")

    async def test_queued_browser_action_is_abandoned_not_clicked_on_takeover(self):
        await self.open_browser()
        self.controller.store.admit(self.access, self.grant, key("reserved"), "browser_click", {"ref": "b2"})
        self.request()
        await self.executor(self.claim())
        self.engine.click.assert_awaited_once()
        self.assertIsNone(self.controller.store.pending(self.access, self.grant))

    async def test_mismatched_shutdown_receipt_never_advances_lifecycle(self):
        await self.open_browser()
        run = self.request()
        rpc = self.client.project_browser

        async def mismatch(operation, **target):
            result = await rpc(operation, **target)
            return {**result, "state": "ready"} if operation == "drain" else result

        with patch.object(self.client, "project_browser", side_effect=mismatch):
            with self.assertRaises(ProjectConflict):
                await self.executor(self.claim())
        self.assert_waiting(run)
        self.assertEqual(self.io.receipt(self.access, self.grant)["state"], "dispatched")
        await self.executor(self.claim(takeover=True))
