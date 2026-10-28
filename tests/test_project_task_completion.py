"""Real task artifacts, PostgreSQL and CAS; models and backends are fixtures."""

from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.project_update import ARTIFACT_NAME
from src.plugins.ai_chat.agent.workspaces import StepWorkspaces
from src.plugins.ai_chat.project_task_artifacts import read_update_artifact
from src.plugins.ai_chat.project_task_reconciliation import ProjectTaskReconciler
from src.plugins.ai_chat.projects import ProjectConflict
from tests import test_project_publication as publication
from tests import test_project_task_reconciliation as reconciliation
from tests import test_project_materialization as materialization
from tests import test_project_restore as restore


class ProjectTaskCompletionTests(reconciliation.ReleaseFixture, unittest.IsolatedAsyncioTestCase):
    accepted = publication.ProjectPublicationTests.accepted
    event = publication.ProjectPublicationTests.event

    async def asyncSetUp(self):
        self.setup_release()
        self.state_dir = self.root.parent / "state"
        self.runtime = SimpleNamespace(store=self.store, lifecycle=self.lifecycle,
            app=SimpleNamespace(state_dir=self.state_dir),
            _execution_access=AsyncMock(return_value=self.access),
            backends={"oci": SimpleNamespace(preflight=Mock())})
        self.worker = ProjectTaskReconciler(self.runtime, self.tasks)
        self.publisher = self.worker.materializations.publications
        self.baseline = self.store.manifest(self.access, self.pid, 1)
        self.accepted()
        result = next(run.result for run in self.tasks.runs(self.task.task_id) if run.run_id == self.producer.run_id)
        result["artifacts"][0]["name"] = ARTIFACT_NAME
        self.artifact = result["artifacts"][0]
        self.tasks.finish_run(self.producer.run_id, "succeeded", result=result)
        self.snapshots = StepWorkspaces(self.state_dir, SimpleNamespace(sandbox_manager=None))
        self.assertEqual(self.snapshots._persist(self.task.task_id, self.content), self.digest)
        self.path = self.snapshots._path(self.task.task_id, self.digest)

    async def test_actual_artifact_is_published_and_restore_queued_then_replayed_without_source(self):
        self.assertEqual(await self.worker.run_once(), 1)
        project = self.store.get(self.access, self.pid)
        self.assertEqual(self.count("project_versions"), 2)
        self.assertEqual(self.count("project_task_publications"), 1)
        self.assertEqual(self.count("project_task_materializations"), 1)
        self.assertEqual(self.store.read_file(self.access, self.pid, 2, "index.html"), b"updated")
        self.assertEqual(self.store.read_file(self.access, self.pid, 1, "index.html"), b"first")
        self.assertEqual(project["locked_task_id"], self.task.task_id)
        self.path.unlink()
        self.store.open(self.access, self.create())
        replacement = ProjectTaskReconciler(self.runtime, self.tasks)
        self.assertEqual(await replacement.run_once(), 0)
        self.assertEqual(self.count("project_versions"), 2)
        self.assertEqual(self.count("project_task_materializations"), 1)
        self.assertEqual(self.publisher.output(self.access, self.task.task_id)["publication"]["artifact_sha256"], self.digest)

    async def test_lost_response_after_publication_uses_cas_on_restart(self):
        original = self.publisher.publish
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("lost publication acknowledgement")
        with patch.object(self.publisher, "publish", side_effect=lost):
            await self.worker.run_once()
        self.assertEqual(self.count("project_task_publications"), 1)
        self.assertEqual(self.count("project_task_materializations"), 0)
        self.path.unlink()
        replacement = ProjectTaskReconciler(self.runtime, self.tasks)
        await replacement.run_once()
        self.assertEqual(self.count("project_task_publications"), 1)
        self.assertEqual(self.count("project_task_materializations"), 1)

    async def test_no_model_or_filename_claim_can_replace_exact_independent_acceptance(self):
        self.tasks.finish_run(self.reviewer.run_id, "failed", result={"summary": "not checked"})
        with patch("src.plugins.ai_chat.project_task_reconciliation.read_update_artifact") as read:
            await self.worker.run_once()
        read.assert_not_called()
        self.assertEqual(self.count("project_task_publications"), 0)
        self.assert_locked()

    async def test_access_revoked_after_read_blocks_publication(self):
        self.runtime._execution_access.side_effect = [self.access, PermissionError("left group")]
        await self.worker.run_once()
        self.assertEqual(self.count("project_task_publications"), 0)
        self.assert_locked()

    async def test_missing_or_corrupt_artifact_never_publishes_or_unlocks(self):
        self.path.unlink()
        await self.worker.run_once()
        self.assertEqual(self.count("project_task_publications"), 0)
        self.assert_locked()
        self.path.write_bytes(b"x" * len(self.content))
        await self.worker.run_once()
        self.assertEqual(self.count("project_task_publications"), 0)
        self.assert_locked()
        self.assertEqual(self.store.read_file(self.access, self.pid, 1, "index.html"), b"first")

    def test_ordinary_zip_is_not_selected_and_ambiguous_bundles_fail_before_io(self):
        result = next(run.result for run in self.tasks.runs(self.task.task_id) if run.run_id == self.producer.run_id)
        original = dict(result["artifacts"][0])
        result["artifacts"][0]["name"] = "report.zip"
        self.tasks.finish_run(self.producer.run_id, "succeeded", result=result)
        with self.assertRaisesRegex(ProjectConflict, "one explicit"):
            self.publisher.output(self.access, self.task.task_id)
        result["artifacts"] = [original, {**original, "handle": "s123456:/workspace/another.zip"}]
        self.tasks.finish_run(self.producer.run_id, "succeeded", result=result)
        with self.assertRaisesRegex(ProjectConflict, "one explicit"):
            self.publisher.output(self.access, self.task.task_id)

    def test_output_scope_is_not_bypassed_by_known_task_id(self):
        for access in (self.b, replace(self.access, member=False),
                       replace(self.access, scope=replace(self.scope, native_conversation_id="999"))):
            with self.subTest(access=access), self.assertRaises(PermissionError):
                self.publisher.output(access, self.task.task_id)

    def test_snapshot_reader_bounds_identity_size_and_regular_file(self):
        self.assertEqual(read_update_artifact(self.state_dir, self.task.task_id, self.artifact), self.content)
        for artifact in ({**self.artifact, "snapshot": "../other"}, {**self.artifact, "size": True},
                         {**self.artifact, "size": 0}, {**self.artifact, "size": 2**40},
                         {**self.artifact, "size": len(self.content) - 1}):
            with self.subTest(artifact=artifact), self.assertRaises(ValueError):
                read_update_artifact(self.state_dir, self.task.task_id, artifact)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(ValueError):
            read_update_artifact(self.state_dir, self.task.task_id, self.artifact)

    def test_snapshot_reader_rejects_symlink_file_and_parent_directory(self):
        target = self.state_dir / "external"
        target.write_bytes(self.content)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            read_update_artifact(self.state_dir, self.task.task_id, self.artifact)
        self.path.unlink()
        self.path.parent.rmdir()
        target_dir = self.state_dir / "other-task"
        target_dir.mkdir()
        (target_dir / self.digest).write_bytes(self.content)
        self.path.parent.symlink_to(target_dir, target_is_directory=True)
        with self.assertRaises(OSError):
            read_update_artifact(self.state_dir, self.task.task_id, self.artifact)


class ProjectTaskCompletedWorkspaceTests(materialization.MaterializationMixin, unittest.IsolatedAsyncioTestCase):
    request = restore.OciRestoreTests.request
    wake = restore.OciRestoreTests.wake
    claim = restore.OciRestoreTests.claim

    async def asyncSetUp(self):
        await restore.OciRestoreTests.asyncSetUp(self)
        self.workspace = await self.wake()
        (self.workspace / "index.html").write_bytes(b"first")
        (self.workspace / "obsolete.txt").write_bytes(b"old")
        self.request("stop")
        await self.executor(self.claim())
        self.bind_task()
        self.accepted(desc_updates={"delete": ["obsolete.txt"]})
        result = next(run.result for run in self.tasks.runs(self.task.task_id) if run.run_id == self.producer.run_id)
        result["artifacts"][0]["name"] = ARTIFACT_NAME
        self.tasks.finish_run(self.producer.run_id, "succeeded", result=result)
        self.state_dir = self.root / "task-state"
        snapshots = StepWorkspaces(self.state_dir, SimpleNamespace(sandbox_manager=None))
        snapshots._persist(self.task.task_id, self.content)
        self.path = snapshots._path(self.task.task_id, self.digest)
        self.runtime = SimpleNamespace(store=self.projects, lifecycle=self.lifecycle,
            app=SimpleNamespace(state_dir=self.state_dir),
            _execution_access=AsyncMock(return_value=self.access), backends={"oci": self.backend})

    async def test_automatic_publication_and_restarted_restore_apply_exact_files_before_unlock(self):
        worker = ProjectTaskReconciler(self.runtime, self.tasks)
        self.assertEqual(await worker.run_once(), 1)
        self.assert_locked()
        self.assertEqual(self.count("project_task_publications"), 1)
        # The independent task snapshot can now expire; restore uses project CAS.
        self.path.unlink()
        self.assertEqual(await ProjectTaskReconciler(self.runtime, self.tasks).run_once(), 0)
        job = self.claim()
        await self.executor(job)
        current = self.volumes.binding(self.scope.key, self.pid, generation=2)
        self.assertEqual((current / "index.html").read_bytes(), b"updated")
        self.assertEqual((current / "src/main.py").read_bytes(), b"print(1)")
        self.assertFalse((current / "obsolete.txt").exists())
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "obsolete.txt"), b"old")
        self.assertEqual(self.count("project_versions"), 2)
        self.assertIsNone(self.projects.get(self.access, self.pid)["locked_task_id"])
        self.assertEqual(self.jobs.get(job.job_id).status, "succeeded")
        self.assertEqual(self.sql.execute("SELECT status FROM project_task_materializations").fetchone()[0], "completed")
        self.assertEqual(await worker.run_once(), 0)


if __name__ == "__main__":
    unittest.main()
