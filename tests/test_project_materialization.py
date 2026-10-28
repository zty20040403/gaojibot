"""Real PG/CAS/private file RPC; kernel, containers and reviewers are fixtures."""

from dataclasses import replace
from contextlib import contextmanager
import time
import unittest
from unittest.mock import Mock, patch

import nonebot

nonebot.init()

from src.bot_storage import open_store_connection
from src.plugins.ai_chat.project_materialization import ProjectMaterializations
from src.plugins.ai_chat.project_publication import ProjectPublications
from src.plugins.ai_chat.project_restore import ProjectRestores
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore
from tests import test_project_publication as publication
from tests import test_project_restore as restore
from tests import test_project_tasks as tasks
from tests import test_project_vm_executor as vm


class MaterializationMixin:
    accepted = publication.ProjectPublicationTests.accepted
    event = publication.ProjectPublicationTests.event
    publish = publication.ProjectPublicationTests.publish
    count = publication.ProjectPublicationTests.count

    def bind_task(self):
        self.tasks = SubAgentStore(self.projects.database)
        self.addCleanup(self.tasks.close)
        self.binding = ProjectTaskStore(self.projects, self.tasks)
        self.publisher = ProjectPublications(self.binding)
        self.materializer = ProjectMaterializations(self.publisher, self.lifecycle)
        self.projects.open(self.access, self.pid)
        self.task = self.binding.prepare(self.access, self.pid,
            expected_version=self.projects.get(self.access, self.pid)["version"],
            base_version=1, request_key=tasks.key("materialize"),
            packet=tasks.task_packet(self.access), decision=None,
            dispatch={"bot_id": "301", "event": {"user_id": 201}}, max_parallelism=2, max_steps=4)
        self.baseline = self.projects.manifest(self.access, self.pid, 1)

    def materialize(self):
        return self.materializer.request(self.access, self.task.task_id, preflight=self.backend.preflight)

    def assert_locked(self):
        project = self.projects.get(self.access, self.pid)
        self.assertEqual(project["locked_task_id"], self.task.task_id)
        with self.assertRaises(ProjectConflict):
            self.projects.update(self.access, self.pid, expected_version=project["version"], title="forbidden")


class OciMaterializationTests(MaterializationMixin, unittest.IsolatedAsyncioTestCase):
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
        self.receipt = self.publish()

    async def test_imports_actual_files_and_unlocks_only_after_final_verification(self):
        self.assert_locked()
        run = self.materialize()
        self.assertEqual(self.materialize()["job_id"], run["job_id"])
        job = self.claim()
        while (step := self.lifecycle.next_step(job))["operation"] != "verify_active":
            evidence = await self.backend.execute(job, step, self.access)
            self.lifecycle.acknowledge(job, step, evidence)
            self.assert_locked()
        with self.assertRaises(ProjectConflict):
            self.lifecycle.finish(job)
        evidence = await self.backend.execute(job, step, self.access)
        self.lifecycle.acknowledge(job, step, evidence)
        result = self.lifecycle.finish(job)
        project = self.projects.get(self.access, self.pid)
        current = self.volumes.binding(self.scope.key, self.pid, generation=2)
        self.assertEqual((current / "index.html").read_bytes(), b"updated")
        self.assertEqual((current / "src/main.py").read_bytes(), b"print(1)")
        self.assertFalse((current / "obsolete.txt").exists())
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "obsolete.txt"), b"old")
        self.assertEqual(self.count("project_versions"), 2)
        self.assertTrue(result["materialized"])
        self.assertEqual(result["snapshot"]["version_no"], 2)
        self.assertIsNone(project["locked_task_id"])
        self.assertIsNone(project["lifecycle_job_id"])
        self.assertEqual(self.jobs.get(job.job_id).status, "succeeded")
        self.assertEqual(self.materialize()["status"], "completed")
        self.assertEqual(self.sql.execute("SELECT status FROM project_task_materializations").fetchone()[0], "completed")
        # Task delivery uses its own immutable artifact, not the project volume.
        self.assertEqual(self.count("subagent_deliveries"), 0)
        self.request("stop")
        await self.executor(self.claim())
        self.assertEqual(self.count("project_versions"), 2)

    async def test_unexpected_old_workspace_edit_is_preserved_and_never_published(self):
        (self.workspace / "unsaved.txt").write_bytes(b"must not disappear")
        self.materialize()
        with self.assertRaisesRegex(ProjectConflict, "baseline workspace changed"):
            await self.executor(self.claim())
        self.assertEqual((self.workspace / "unsaved.txt").read_bytes(), b"must not disappear")
        self.assertEqual(self.count("project_versions"), 2)
        self.assert_locked()
        self.assertFalse(self.volumes.status(self.scope.key, self.pid, generation=1).get("deleted", False))

    async def test_corrupt_accepted_blob_fails_before_old_volume_is_touched(self):
        path = self.projects.blobs.path(publication.sha(b"updated"))
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
        self.materialize()
        commands, calls = list(self.docker.commands), list(self.host.calls)
        with self.assertRaises(ValueError):
            await self.executor(self.claim())
        self.assertEqual(self.docker.commands, commands)
        self.assertEqual(self.host.calls, calls)
        self.assertEqual((self.workspace / "index.html").read_bytes(), b"first")
        self.assert_locked()

    async def test_queue_checkpoint_failure_rolls_back_job_binding_and_generation(self):
        before = self.projects.get(self.access, self.pid)
        counts = [self.count(t) for t in ("durable_jobs", "project_lifecycle_runs", "subagent_checkpoints")]
        with patch.object(self.tasks, "append_checkpoint", side_effect=OSError("commit failed")):
            with self.assertRaises(OSError):
                self.materialize()
        self.assertEqual(self.projects.get(self.access, self.pid), before)
        self.assertEqual(self.count("project_task_materializations"), 0)
        self.assertEqual([self.count(t) for t in ("durable_jobs", "project_lifecycle_runs", "subagent_checkpoints")], counts)
        self.materialize()
        await self.executor(self.claim())

    def test_published_task_cannot_be_revised_against_old_baseline(self):
        coordinator = SubAgentCoordinator(self.tasks, Mock(), logger=Mock())
        before = self.tasks.control(self.task.task_id)
        with self.assertRaisesRegex(ValueError, "start a new task"):
            coordinator.revise(self.task.task_id, scope_key=self.scope.key, requester_user_id=201,
                instruction="another edit", step_keys=["code"], expected_version=before["version"])
        self.assertEqual(self.tasks.control(self.task.task_id), before)
        self.assert_locked()

    async def test_lost_import_receipt_takeover_reuses_job_and_version(self):
        run = self.materialize()
        job = self.claim()
        publish = ProjectRestores.publish

        def lost(*args):
            publish(*args)
            raise OSError("import receipt lost")

        with patch.object(ProjectRestores, "publish", side_effect=lost, autospec=True):
            with self.assertRaises(OSError):
                await self.executor(job)
        self.assertEqual(self.count("project_versions"), 2)
        self.assert_locked()
        self.assertEqual(self.materialize()["job_id"], run["job_id"])
        successor = self.claim(now=int(time.time()) + 61)
        with self.assertRaises(ProjectConflict):
            await self.executor(job)
        await self.executor(successor)
        self.assertEqual(self.count("project_versions"), 2)
        self.assertIsNone(self.projects.get(self.access, self.pid)["locked_task_id"])

    async def test_finish_transaction_failure_never_unlocks_without_receipt(self):
        self.materialize()
        job = self.claim()
        while (step := self.lifecycle.next_step(job)) is not None:
            self.lifecycle.acknowledge(job, step, await self.backend.execute(job, step, self.access))
        transaction = self.projects._transaction

        @contextmanager
        def failed_commit():
            with transaction() as cursor:
                yield cursor
                raise OSError("final commit failed")

        with patch.object(self.projects, "_transaction", failed_commit):
            with self.assertRaisesRegex(OSError, "final commit failed"):
                self.lifecycle.finish(job)
        self.assert_locked()
        self.assertEqual(self.jobs.get(job.job_id).status, "running")
        self.assertEqual(self.sql.execute("SELECT status FROM project_task_materializations").fetchone()[0], "pending")
        successor = self.claim(now=int(time.time()) + 61)
        await self.executor(successor)
        self.assertIsNone(self.projects.get(self.access, self.pid)["locked_task_id"])
        self.assertEqual(self.jobs.get(job.job_id).status, "succeeded")

    async def test_delete_receipt_loss_recovers_without_replacing_again(self):
        self.materialize()
        job = self.claim()
        original = self.client.project_volume

        async def lost(action, *args, **kwargs):
            result = await original(action, *args, **kwargs)
            if action == "delete":
                raise OSError("delete receipt lost")
            return result

        with patch.object(self.client, "project_volume", side_effect=lost):
            with self.assertRaises(OSError):
                await self.executor(job)
        self.assert_locked()
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "obsolete.txt"), b"old")
        await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(self.count("project_versions"), 2)
        current = self.volumes.binding(self.scope.key, self.pid, generation=2)
        self.assertEqual((current / "index.html").read_bytes(), b"updated")

    def test_backend_unavailable_rejects_before_queue_or_generation_change(self):
        before = self.projects.get(self.access, self.pid)
        with patch.object(self.backend, "preflight", side_effect=RuntimeError("no backend")):
            with self.assertRaises(RuntimeError):
                self.materialize()
        self.assertEqual(self.projects.get(self.access, self.pid), before)
        self.assertEqual(self.count("project_task_materializations"), 0)

    async def test_revoked_actor_cannot_materialize_or_resume(self):
        denied = replace(self.access, member=False)
        with self.assertRaises(PermissionError):
            self.materializer.request(denied, self.task.task_id, preflight=self.backend.preflight)
        self.materialize()
        job = self.claim()
        self.executor.resolve_access = lambda *_: denied
        with self.assertRaises(PermissionError):
            await self.executor(job)
        self.assert_locked()
        self.assertEqual((self.workspace / "index.html").read_bytes(), b"first")

    async def test_other_lifecycle_or_changed_revision_cannot_borrow_task_lock(self):
        with self.assertRaises(ProjectConflict):
            self.request("wake")
        self.materialize()
        job = self.claim()
        with self.assertRaises(ProjectConflict):
            self.lifecycle.context(replace(job, job_id=job.job_id + 1), access=self.access)
        self.sql.execute("UPDATE subagent_controls SET revision=revision+1 WHERE task_id=?", (self.task.task_id,))
        with self.assertRaises(ProjectConflict):
            await self.executor(job)
        self.assert_locked()


class VmMaterializationTests(MaterializationMixin, vm.VmExecutorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        _, self.sql = open_store_connection(self.db)
        self.addCleanup(self.sql.close)

    async def prepare_dependencies(self, *, hibernate=False):
        await self.transition("wake")
        old = self.guests[self.project()["sandbox_id"]].workspace
        (old / "index.html").write_bytes(b"first")
        (old / "package.json").write_bytes(b'{"name":"fixture"}')
        (old / "package-lock.json").write_bytes(b'{"lockfileVersion":3}')
        (old / "node_modules/pkg").mkdir(parents=True)
        (old / "node_modules/pkg/data").write_bytes(b"dependency")
        self.projects.update(self.access, self.pid, expected_version=self.project()["version"], rebuild_command="npm ci")
        await self.transition("stop")
        if hibernate:
            await self.transition("hibernate")
        self.bind_task()
        self.accepted({"index.html": b"accepted VM files"})
        self.publish()
        self.materialize()
        return self.claim()

    async def test_vm_restores_and_rebuilds_with_same_published_version(self):
        job = await self.prepare_dependencies()
        await self.verify_materialization(job)

    async def test_hibernated_baseline_pruned_dependencies_are_not_unsaved_changes(self):
        job = await self.prepare_dependencies(hibernate=True)
        await self.verify_materialization(job)

    async def verify_materialization(self, job):
        result = await self.executor(job)
        root = self.guests[self.project()["sandbox_id"]].workspace
        self.assertEqual((root / "index.html").read_bytes(), b"accepted VM files")
        self.assertEqual(result["snapshot"]["version_no"], 2)
        self.assertEqual(self.count("project_versions"), 2)
        self.assertIsNone(self.project()["locked_task_id"])
        evidence = self.sql.execute("SELECT evidence FROM project_lifecycle_steps WHERE operation='rebuild_dependencies'").fetchone()[0]
        self.assertEqual(evidence["command"], "npm ci")
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["rebuilt_version"], result["snapshot"])

    async def test_rebuild_failure_preserves_task_lock_and_published_version(self):
        job = await self.prepare_dependencies()
        with patch.object(self.files, "_rebuild", side_effect=ProjectConflict("dependency install failed")):
            with self.assertRaisesRegex(ProjectConflict, "dependency install failed"):
                await self.executor(job)
        self.assert_locked()
        self.assertEqual(self.count("project_versions"), 2)
        self.assertEqual(self.sql.execute("SELECT status FROM project_task_materializations").fetchone()[0], "pending")
        successor = self.claim(now=int(time.time()) + 61)
        await self.verify_materialization(successor)


if __name__ == "__main__":
    unittest.main()
