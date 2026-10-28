"""Real PostgreSQL release/recovery receipts; no model or production access."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle
from src.plugins.ai_chat.project_task_reconciliation import ProjectTaskReconciler, ProjectTaskReleases
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, TaskStep
from tests import test_project_tasks as fixture
from tests import test_project_publication as publication


class ReleaseFixture:
    owned = fixture.ProjectTaskTests.owned
    new_store = fixture.ProjectTaskTests.new_store
    create = fixture.ProjectTaskTests.create
    snapshot = fixture.ProjectTaskTests.snapshot
    prepare = fixture.ProjectTaskTests.prepare
    count = fixture.ProjectTaskTests.count

    def setup_release(self):
        fixture.ProjectTaskTests.setUp(self)
        self.task = self.prepare()
        self.releases = ProjectTaskReleases(self.binding)
        self.jobs = self.owned(DurableJobStore(self.db))
        self.lifecycle = ProjectLifecycle(self.store, self.jobs)
        self.run = self.tasks.create_run(self.task.task_id, TaskStep("draft", "coder", "edit", "bundle"),
            allowed_tools=[], model_profile="fixture")
        self.tasks.finish_run(self.run.run_id, "failed", result={"summary": "unfinished draft"})
        self.tasks.set_task_state(self.task.task_id, "failed", error="fixture worker stopped")

    def release(self):
        return self.releases.release_failed(self.access, self.task.task_id)

    def assert_locked(self):
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], self.task.task_id)
        self.assertEqual(self.count("project_task_releases"), 0)


class ProjectTaskReleaseTests(ReleaseFixture, unittest.TestCase):
    setUp = ReleaseFixture.setup_release

    def test_failed_and_cancelled_detach_without_touching_independent_drafts(self):
        self.tasks.add_artifacts(self.task.task_id, self.run.run_id, [{
            "handle": "s123456:/workspace/draft.txt", "name": "draft.txt", "kind": "file"}])
        draft = self.root / "independent-draft.txt"
        draft.write_bytes(b"not yet delivered")
        artifacts = self.sql.execute("SELECT * FROM subagent_artifacts WHERE task_id=?", (self.task.task_id,)).fetchall()
        before = self.tasks.get(self.task.task_id)
        notified = []
        self.tasks.set_change_listener(lambda tid: notified.append((tid, self.count("project_task_releases"))))
        receipt = self.release()
        self.assertEqual(receipt["task_status"], "failed")
        self.assertEqual(notified, [(self.task.task_id, 1)])
        self.assertIsNone(self.store.get(self.access, self.pid)["locked_task_id"])
        self.assertEqual(self.tasks.get(self.task.task_id), before)
        self.assertEqual(self.sql.execute("SELECT * FROM subagent_artifacts WHERE task_id=?", (self.task.task_id,)).fetchall(), artifacts)
        self.assertEqual(draft.read_bytes(), b"not yet delivered")
        self.assertEqual(self.store.read_file(self.access, self.pid, 1, "index.html"), b"first")
        self.assertEqual(self.count("project_versions"), 1)
        checkpoint = self.tasks.checkpoints(self.task.task_id)[-1]
        self.assertEqual(checkpoint["phase"], "project_task_released")
        self.assertIs(checkpoint["state"]["task_artifacts_deleted"], False)
        # A fresh task is possible despite the old task's undelivered draft.
        new_task = self.prepare(request_key=fixture.key("second"))
        self.tasks.set_task_state(new_task.task_id, "cancelled")
        self.assertEqual(self.releases.release_failed(self.access, new_task.task_id)["task_status"], "cancelled")
        project = self.store.get(self.access, self.pid)
        self.assertEqual(self.lifecycle.request(self.access, self.pid, "wake",
            expected_version=project["version"], request_key="after-detachment")["status"], "pending")

    def test_replay_after_restart_and_new_task_does_not_unlock_the_new_owner(self):
        receipt = self.release()
        new_task = self.prepare(request_key=fixture.key("second"))
        self.store.open(self.access, self.create())
        reopened = ProjectTaskReleases(ProjectTaskStore(self.new_store(), self.owned(SubAgentStore(self.db))))
        self.assertEqual(reopened.release_failed(self.access, self.task.task_id), receipt)
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], new_task.task_id)
        self.assertEqual(self.count("project_task_releases"), 1)

    def test_nonterminal_partial_and_completed_tasks_are_not_abandoned(self):
        for status in ("queued", "running", "waiting_external", "interrupted", "partial", "completed"):
            self.tasks.set_task_state(self.task.task_id, status)
            with self.subTest(status=status), self.assertRaises(ProjectConflict):
                self.release()
            self.assert_locked()

    def test_scope_actor_bot_and_membership_still_apply_on_replay(self):
        for released in (False, True):
            if released:
                self.release()
            for access in (self.b, replace(self.b, admin=True), replace(self.access, member=False),
                           replace(self.access, scope=replace(self.scope, native_conversation_id="102")),
                           replace(self.access, scope=replace(self.scope, bot_native_user_id="302"))):
                with self.subTest(released=released, access=access), self.assertRaises(PermissionError):
                    self.releases.release_failed(access, self.task.task_id)

    def test_unfinished_steps_and_live_worker_lease_block_release(self):
        self.tasks.finish_run(self.run.run_id, "running", result={})
        with self.assertRaisesRegex(ProjectConflict, "unfinished steps"):
            self.release()
        self.tasks.finish_run(self.run.run_id, "failed", result={})
        job, _ = self.jobs.enqueue(kind="subagent.workflow", scope_key=self.scope.key,
            idempotency_key="release-worker", payload={"task_id": self.task.task_id})
        self.jobs.claim_due("worker", kinds=("subagent.workflow",))
        with self.assertRaisesRegex(ProjectConflict, "execution lease"):
            self.release()
        self.assert_locked()
        self.sql.execute("UPDATE durable_jobs SET lease_until=? WHERE job_id=?", (int(time.time()) - 1, job.job_id))
        self.release()

    def test_pending_unknown_and_timed_out_remote_receipts_do_not_mean_settled(self):
        self.tasks.begin_external(self.task.task_id, 1, self.run.run_id, "remote", {
            "method": "POST", "path": "/v1/jobs", "tool_name": "cluster_job"})
        item = self.tasks.external_calls(self.task.task_id)[0]
        with self.assertRaisesRegex(ProjectConflict, "unconfirmed remote"):
            self.release()
        for response in (
            {"job_id": "job-1", "status": "needs_attention"},
            {"operation": {"operation_id": "op-1", "status": "timed_out"}},
            {"operation": {"operation_id": "op-1", "status": "unknown"}},
            {"pending": True},
        ):
            self.tasks.finish_external(item, response)
            with self.subTest(response=response), self.assertRaisesRegex(ProjectConflict, "unconfirmed remote"):
                self.release()
            self.assert_locked()
        self.tasks.finish_external(item, {"job_id": "job-1", "status": "cancelled"})
        self.release()
        self.assertEqual(self.tasks.external_calls(self.task.task_id)[0]["response"]["status"], "cancelled")

    def test_failed_remote_observation_from_previous_revision_still_blocks(self):
        self.tasks.begin_external(self.task.task_id, 1, self.run.run_id, "old", {"method": "POST", "path": "/v1/jobs"})
        item = self.tasks.external_calls(self.task.task_id)[0]
        self.tasks.finish_external(item, {"status": "needs_attention"})
        self.sql.execute("UPDATE subagent_controls SET revision=2 WHERE task_id=?", (self.task.task_id,))
        with self.assertRaisesRegex(ProjectConflict, "unconfirmed remote"):
            self.release()
        self.assert_locked()

    def test_checkpoint_failure_rolls_back_receipt_unlock_and_events(self):
        before = self.store.get(self.access, self.pid)
        events = self.count("subagent_events")
        listener = Mock()
        self.tasks.set_change_listener(listener)
        original = self.tasks.append_checkpoint
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("lost commit")
        with patch.object(self.tasks, "append_checkpoint", side_effect=crash), self.assertRaises(OSError):
            self.release()
        self.assert_locked()
        self.assertEqual(self.store.get(self.access, self.pid), before)
        self.assertEqual(self.count("subagent_events"), events)
        listener.assert_not_called()
        self.release()

    def test_generation_fence_and_baseline_drift_do_not_unlock(self):
        for column in ("workspace_generation", "lifecycle_fence"):
            before = self.store.get(self.access, self.pid)[column]
            self.sql.execute(f"UPDATE projects SET {column}=? WHERE project_id=?", (before + 1, self.pid))
            with self.subTest(column=column), self.assertRaises(ProjectConflict):
                self.release()
            self.assert_locked()
            self.sql.execute(f"UPDATE projects SET {column}=? WHERE project_id=?", (before, self.pid))
        other_blob = self.store.manifest(self.access, self.pid, 1)["files"][0]["sha256"]
        self.sql.execute("UPDATE project_task_bindings SET manifest_sha256=? WHERE task_id=?", (other_blob, self.task.task_id))
        with self.assertRaises(ProjectConflict):
            self.release()

    def test_released_task_cannot_revise_against_obsolete_project_baseline(self):
        self.release()
        before = self.tasks.control(self.task.task_id)
        coordinator = SubAgentCoordinator(self.tasks, Mock(), logger=Mock())
        with self.assertRaisesRegex(ValueError, "was released"):
            coordinator.revise(self.task.task_id, scope_key=self.scope.key, requester_user_id=201,
                instruction="edit again", step_keys=["draft"], expected_version=before["version"])
        self.assertEqual(self.tasks.control(self.task.task_id), before)
        self.assertEqual(self.tasks.get(self.task.task_id).status, "failed")

    def test_concurrent_release_has_one_receipt_and_one_project_revision(self):
        releases = [ProjectTaskReleases(ProjectTaskStore(self.new_store(), self.owned(SubAgentStore(self.db)))) for _ in range(2)]
        before = self.store.get(self.access, self.pid)["version"]
        barrier = threading.Barrier(2)
        def release(i):
            barrier.wait(timeout=5)
            return releases[i].release_failed(self.access, self.task.task_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(release, range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.count("project_task_releases"), 1)
        self.assertEqual(self.store.get(self.access, self.pid)["version"], before + 1)


class ProjectTaskReconciliationTests(ReleaseFixture, unittest.IsolatedAsyncioTestCase):
    accepted = publication.ProjectPublicationTests.accepted
    event = publication.ProjectPublicationTests.event

    async def asyncSetUp(self):
        self.setup_release()
        self.runtime = SimpleNamespace(store=self.store, lifecycle=self.lifecycle,
            _execution_access=AsyncMock(return_value=self.access),
            backends={"oci": SimpleNamespace(preflight=Mock())})
        self.worker = ProjectTaskReconciler(self.runtime, self.tasks)

    async def test_live_access_failure_defers_without_unlock_then_recovers(self):
        self.runtime._execution_access.side_effect = PermissionError("membership revoked")
        self.assertEqual(await self.worker.run_once(), 1)
        self.assert_locked()
        self.runtime._execution_access.side_effect = None
        replacement = ProjectTaskReconciler(self.runtime, self.owned(SubAgentStore(self.db)))
        self.assertEqual(await replacement.run_once(), 1)
        self.assertEqual(await replacement.run_once(), 0)
        self.assertIsNone(self.store.get(self.access, self.pid)["locked_task_id"])
        self.runtime.backends["oci"].preflight.assert_not_called()

    async def test_restart_resumes_accepted_version_once_not_failed_task_release(self):
        self.baseline = self.store.manifest(self.access, self.pid, 1)
        self.accepted()
        publisher = self.worker.materializations.publications
        publisher.publish(self.access, self.task.task_id, self.digest, self.content)
        self.assertEqual(await self.worker.run_once(), 1)
        pending = self.store.get(self.access, self.pid)
        self.assertIsNotNone(pending["lifecycle_job_id"])
        replacement = ProjectTaskReconciler(self.runtime, self.owned(SubAgentStore(self.db)))
        self.assertEqual(await replacement.run_once(), 0)
        self.assertEqual(self.count("project_task_materializations"), 1)
        self.assertEqual(self.count("project_task_releases"), 0)
        self.assertEqual(self.count("project_versions"), 2)
        self.assertEqual(pending["locked_task_id"], self.task.task_id)
        # Later lifecycle failures must retain the accepted version and task lock.
        self.tasks.set_task_state(self.task.task_id, "failed")
        with self.assertRaisesRegex(ProjectConflict, "must be materialized"):
            self.release()
        self.assertEqual(self.store.get(self.access, self.pid), pending)

    async def test_reconciliation_retries_preflight_without_losing_publication(self):
        self.baseline = self.store.manifest(self.access, self.pid, 1)
        self.accepted()
        self.worker.materializations.publications.publish(self.access, self.task.task_id, self.digest, self.content)
        self.runtime.backends["oci"].preflight.side_effect = OSError("backend unavailable")
        self.assertEqual(await self.worker.run_once(), 1)
        self.assert_locked()
        self.assertEqual(self.count("project_task_materializations"), 0)
        self.runtime.backends["oci"].preflight.side_effect = None
        self.assertEqual(await self.worker.run_once(), 1)
        self.assertEqual(self.count("project_task_materializations"), 1)

    async def test_bounded_pages_do_not_starve_later_tasks(self):
        first = [{"task_id": i, "actor_principal_id": 1, "published": False} for i in range(1, 21)]
        second = [{"task_id": 21, "actor_principal_id": 1, "published": False}]
        self.runtime._execution_access.side_effect = PermissionError("revoked")
        with patch.object(self.worker.releases, "candidates", side_effect=[first, second, []]) as candidates:
            self.assertEqual(await self.worker.run_once(), 20)
            self.assertEqual(await self.worker.run_once(), 1)
            self.assertEqual(await self.worker.run_once(), 0)
        self.assertEqual([call.kwargs["after"] for call in candidates.call_args_list], [0, 20, 0])

    async def test_reconciler_stop_does_not_wait_for_next_period(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        def scan(**kwargs):
            loop.call_soon_threadsafe(stop.set)
            return []
        with patch.object(self.worker.releases, "candidates", side_effect=scan):
            await asyncio.wait_for(self.worker.run(stop), timeout=1)


if __name__ == "__main__":
    unittest.main()
