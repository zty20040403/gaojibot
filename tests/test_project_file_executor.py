"""PostgreSQL + private Unix files; explicit fake Docker/kernel, no QQ/model calls."""

import asyncio
import hashlib
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.bot_storage import open_store_connection
from src.project_content import CHUNK_BYTES
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_file_executor import ProjectFileExecutor
from src.plugins.ai_chat.project_executor import ProjectBackendUnavailable
from src.plugins.ai_chat.projects import ProjectConflict, ProjectQuotaExceeded
from src.sandbox_runtime.docker import SandboxError
from tests import test_project_executor as fixture
from tests.postgres_fixture import postgres_for_test


class ProjectFileExecutorTests(unittest.IsolatedAsyncioTestCase):
    request = fixture.ProjectExecutorTests.request
    claim = fixture.ProjectExecutorTests.claim

    async def asyncSetUp(self):
        await fixture.ProjectExecutorTests.asyncSetUp(self)
        self.backend.files = ProjectFileExecutor(
            self.lifecycle,
            self.client,
            resolve_access=lambda project, actor: self.access,
        )
        _, self.sql = open_store_connection(postgres_for_test(self))
        self.addCleanup(self.sql.close)

    async def wake(self):
        self.request()
        await self.executor(self.claim())
        return self.volumes.binding(self.scope.key, self.pid)

    def configure_rebuild(self, command="npm ci"):
        project = self.projects.get(self.access, self.pid)
        return self.projects.update(
            self.access,
            self.pid,
            expected_version=project["version"],
            rebuild_command=command,
        )

    async def dependency_project(self, *, locked=True):
        root = await self.wake()
        self.configure_rebuild()
        (root / "package.json").write_bytes(b'{"name":"fixture"}')
        if locked:
            (root / "package-lock.json").write_bytes(b'{"lockfileVersion":3}')
        (root / "source.js").write_bytes(b"retained source")
        (root / "node_modules/pkg").mkdir(parents=True)
        (root / "node_modules/pkg/data").write_bytes(b"dependency bytes")
        self.request("stop")
        await self.executor(self.claim())
        return root

    async def test_hibernate_prunes_only_after_real_snapshot_publication(self):
        root = await self.dependency_project()
        self.assertTrue((root / "node_modules/pkg/data").exists())
        self.request("hibernate")
        result = await self.executor(self.claim())
        self.assertEqual(result["state"], "hibernated")
        self.assertFalse((root / "node_modules").exists())
        self.assertEqual((root / "source.js").read_bytes(), b"retained source")
        versions = self.projects.versions(self.access, self.pid)
        self.assertEqual(len(versions), 1)
        manifest = json.loads(self.projects.blobs.read(versions[0]["manifest_sha256"]))
        self.assertEqual(manifest["rebuild"]["excluded"], ["node_modules"])
        self.assertEqual(manifest["rebuild"]["command"], "npm ci")
        self.assertEqual(len(manifest["rebuild"]["locks"]), 2)
        evidence = self.sql.execute(
            "SELECT evidence FROM project_lifecycle_steps WHERE operation='prune_rebuildable'"
        ).fetchone()["evidence"]
        self.assertEqual(
            evidence["snapshot"]["manifest_sha256"], versions[0]["manifest_sha256"]
        )
        self.assertEqual(evidence["pruned_roots"], 1)

    async def test_hibernate_without_lock_keeps_dependencies(self):
        root = await self.dependency_project(locked=False)
        self.request("hibernate")
        await self.executor(self.claim())
        self.assertEqual(
            (root / "node_modules/pkg/data").read_bytes(), b"dependency bytes"
        )
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "node_modules/pkg/data"),
            b"dependency bytes",
        )
        evidence = self.sql.execute(
            "SELECT evidence FROM project_lifecycle_steps WHERE operation='snapshot_workspace' LIMIT 1"
        ).fetchone()["evidence"]
        self.assertIn("Missing package-lock.json", evidence["warnings"][0])

    async def test_prune_lost_ack_takeover_reconciles_before_removal(self):
        root = await self.dependency_project()
        self.request("hibernate")
        job = self.claim()
        call = self.client.project_files

        async def uncertain(operation, *args, **kwargs):
            result = await call(operation, *args, **kwargs)
            if operation == "prune":
                raise SandboxError("fixture lost cleanup acknowledgement")
            return result

        with patch.object(self.client, "project_files", side_effect=uncertain):
            with self.assertRaises(SandboxError):
                await self.executor(job)
        self.assertFalse((root / "node_modules").exists())
        self.assertTrue(self.docker.containers)
        self.assertTrue(
            self.volumes.status(self.scope.key, self.pid)["maintenance_active"]
        )
        successor = self.claim(now=int(time.time()) + 61)
        await self.executor(successor)
        self.assertEqual(
            self.projects.get(self.access, self.pid)["status"], "hibernated"
        )
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 1)
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "source.js"),
            b"retained source",
        )

    async def test_changed_tree_after_prune_receipt_blocks_hibernate_completion(self):
        root = await self.dependency_project()
        self.request("hibernate")
        call = self.client.project_files

        async def changed(operation, *args, **kwargs):
            result = await call(operation, *args, **kwargs)
            if operation == "prune":
                (root / "source.js").write_bytes(b"changed retained content")
            return result

        with patch.object(self.client, "project_files", side_effect=changed):
            with self.assertRaises(SandboxError):
                await self.executor(self.claim())
        self.assertEqual(self.projects.get(self.access, self.pid)["status"], "stopped")
        self.assertTrue(self.docker.containers)
        self.assertEqual((root / "source.js").read_bytes(), b"changed retained content")

    async def test_hibernated_wake_keeps_version_recipe_when_metadata_is_cleared(self):
        await self.dependency_project()
        self.request("hibernate")
        await self.executor(self.claim())
        project = self.configure_rebuild("")
        run = self.lifecycle.request(
            self.access,
            self.pid,
            "wake",
            expected_version=project["version"],
            request_key="wake-with-pinned-recipe",
        )
        self.assertEqual(run["plan"]["rebuild_command"], "npm ci")
        self.assertIn("rebuild_dependencies", run["plan"]["steps"])
        commands, calls = list(self.docker.commands), list(self.host.calls)
        with patch.object(
            self.backend.files,
            "operations",
            self.backend.files.operations - {"rebuild_dependencies"},
        ):
            with self.assertRaises(ProjectBackendUnavailable):
                await self.executor(self.claim())
        self.assertEqual(self.docker.commands, commands)
        self.assertEqual(self.host.calls, calls)

    async def test_version_rollback_preserves_locked_recipe_and_digest(self):
        await self.dependency_project()
        original = self.projects.versions(self.access, self.pid)[0]
        project = self.projects.get(self.access, self.pid)
        self.projects.snapshot(
            self.access,
            self.pid,
            [ProjectFile("different", b"new version")],
            expected_version=project["version"],
        )
        project = self.projects.get(self.access, self.pid)
        restored = self.projects.restore(
            self.access,
            self.pid,
            1,
            expected_version=project["version"],
        )
        self.assertEqual(restored["version_no"], 3)
        self.assertEqual(restored["manifest_sha256"], original["manifest_sha256"])

    async def test_vm_rebuild_preparation_reads_frozen_pg_version_and_checks_lease(self):
        await self.dependency_project()
        version = self.projects.versions(self.access, self.pid)[0]
        manifest = self.projects.manifest(self.access, self.pid, version["version_no"])
        project = self.projects.get(self.access, self.pid)
        args = dict(owner=self.scope.key, project_id=self.pid, generation=1, fence=30,
                    reservation="a" * 32, baseline={k: version[k] for k in ("version_no", "manifest_sha256")},
                    rebuild_spec=manifest["rebuild"], rebuild_command="npm ci")
        receipt = {**{k: args[k] for k in ("project_id", "generation", "fence")},
                   "manifest_sha256": version["manifest_sha256"], "command": "npm ci", "state": "completed",
                   "pending": False, "exit_code": 0, "finished_at": int(time.time())}
        client = Mock(project_rebuild=AsyncMock(return_value=receipt), project_rebuild_prepare=AsyncMock())
        files = ProjectFileExecutor(self.lifecycle, client, resolve_access=lambda *a: self.access)
        context = AsyncMock(return_value=(self.access, project))
        async def prepare(*a, **kwargs):
            self.assertEqual(kwargs["manifest"], manifest)
            await kwargs["check_context"]()
        client.project_rebuild_prepare.side_effect = prepare
        with patch.object(files, "_context", context), patch.object(self.lifecycle, "record_progress"):
            evidence = await files._rebuild(object(), {"arguments": args})
        self.assertTrue(evidence["verified"])
        self.assertEqual(context.await_count, 6)
        self.assertEqual(client.project_rebuild.await_count, 2)
        client.project_rebuild_prepare.assert_awaited_once()
        client.reset_mock()
        args["baseline"]["manifest_sha256"] = "f" * 64
        with patch.object(files, "_context", context), self.assertRaises(ProjectConflict):
            await files._rebuild(object(), {"arguments": args})
        client.project_rebuild_prepare.assert_not_awaited()
        client.project_rebuild.assert_not_awaited()

    async def test_stop_snapshots_real_bytes_without_restarting_a_writer(self):
        root = await self.wake()
        (root / "report.txt").write_text("saved content", encoding="utf-8")
        data = os.urandom(CHUNK_BYTES + 27)
        (root / "binary.dat").write_bytes(data)
        (root / "empty").touch()
        (root / "run.sh").write_bytes(b"#!/bin/sh\n")
        (root / "run.sh").chmod(0o755)
        self.request("stop")
        job = self.claim()
        result = await self.executor(job)
        self.assertEqual(result["state"], "stopped")
        version = self.projects.versions(self.access, self.pid)[0]
        self.assertEqual(version["file_count"], 4)
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "binary.dat"), data
        )
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "empty"), b""
        )
        self.assertEqual(self.docker.serial, 1)
        self.assertFalse(
            next(iter(self.docker.containers.values()))["State"]["Running"]
        )
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM project_snapshot_staging"
            ).fetchone()[0],
            0,
        )

    async def test_restore_baseline_before_first_start_and_verify_executable_mode(self):
        data = b"binary\0\xff" * (CHUNK_BYTES // 4)
        self.projects.snapshot(
            self.access,
            self.pid,
            [
                ProjectFile("dir/data.bin", data),
                ProjectFile("run.sh", b"#!/bin/sh\n", 0o755),
                ProjectFile("empty", b""),
            ],
            expected_version=1,
        )
        root = await self.wake()
        self.assertEqual((root / "dir/data.bin").read_bytes(), data)
        self.assertEqual((root / "run.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((root / "empty").read_bytes(), b"")
        self.assertEqual(self.docker.serial, 1)
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 1)

    async def test_restore_lost_chunk_ack_resumes_same_generation_without_duplicate_container(
        self,
    ):
        data = b"x" * (CHUNK_BYTES + 15)
        self.projects.snapshot(
            self.access, self.pid, [ProjectFile("output.bin", data)], expected_version=1
        )
        self.request()
        job = self.claim()
        call = self.client.project_files
        interrupted = False

        async def uncertain(operation, *args, **kwargs):
            nonlocal interrupted
            result = await call(operation, *args, **kwargs)
            if operation == "restore_file" and not interrupted:
                interrupted = True
                raise SandboxError("fixture lost acknowledgement after chunk write")
            return result

        with patch.object(self.client, "project_files", side_effect=uncertain):
            with self.assertRaises(SandboxError):
                await self.executor(job)
        self.assertEqual(self.docker.serial, 0)
        successor = self.claim(now=int(time.time()) + 61)
        await self.executor(successor)
        root = self.volumes.binding(self.scope.key, self.pid)
        self.assertEqual((root / "output.bin").read_bytes(), data)
        self.assertEqual(self.docker.serial, 1)
        self.assertEqual(
            self.projects.get(self.access, self.pid)["workspace_generation"], 1
        )

    async def test_staged_snapshot_survives_takeover_and_is_not_collected(self):
        root = await self.wake()
        content = b"protected staged content"
        (root / "report").write_bytes(content)
        self.request("stop")
        job = self.claim()
        snapshot = self.projects.snapshot
        with patch.object(
            self.projects,
            "snapshot",
            side_effect=RuntimeError("crash before publication"),
        ):
            with self.assertRaises(RuntimeError):
                await self.executor(job)
        self.assertEqual(self.projects.versions(self.access, self.pid), [])
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM project_snapshot_staging"
            ).fetchone()[0],
            1,
        )
        self.assertNotIn(
            hashlib.sha256(content).hexdigest(),
            self.projects.collect_blobs(now=int(time.time()) + 90000),
        )
        successor = self.claim(now=int(time.time()) + 61)
        with (
            patch.object(self.projects, "snapshot", wraps=snapshot),
            patch.object(
                self.client, "project_files", wraps=self.client.project_files
            ) as calls,
        ):
            await self.executor(successor)
        self.assertFalse(any(call.args[0] == "read" for call in calls.call_args_list))
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "report"), content
        )
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM project_snapshot_staging"
            ).fetchone()[0],
            0,
        )

    async def test_streaming_does_not_lock_out_queue_heartbeat(self):
        root = await self.wake()
        (root / "report").write_bytes(b"content")
        self.request("stop")
        job = self.claim()
        put = self.projects.blobs.put
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        once = False

        def slow(content):
            nonlocal once
            if not once:
                once = True
                loop.call_soon_threadsafe(entered.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            return put(content)

        with patch.object(self.projects.blobs, "put", side_effect=slow):
            running = asyncio.create_task(self.executor(job))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                renewed = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.jobs.renew_lease,
                        job.job_id,
                        job.lease_owner,
                        lease_generation=job.lease_generation,
                    ),
                    2,
                )
                self.assertTrue(renewed)
            finally:
                release.set()
                await running

    async def test_staged_bytes_count_toward_scope_quota(self):
        root = await self.wake()
        (root / "report").write_bytes(b"x" * 2048)
        self.sql.execute(
            "UPDATE project_scope_limits SET quota_bytes=1024 WHERE scope_key=?",
            (self.scope.key,),
        )
        self.request("stop")
        with self.assertRaises(ProjectQuotaExceeded):
            await self.executor(self.claim())
        self.assertEqual(self.projects.versions(self.access, self.pid), [])
        self.assertEqual((root / "report").stat().st_size, 2048)

    async def test_mutated_source_cannot_publish_a_stale_snapshot(self):
        root = await self.wake()
        (root / "report").write_bytes(b"first")
        self.request("stop")
        job = self.claim()
        call = self.client.project_files

        async def changed(operation, *args, **kwargs):
            if operation == "check":
                (root / "added").write_bytes(b"do not lose this")
            return await call(operation, *args, **kwargs)

        with patch.object(self.client, "project_files", side_effect=changed):
            with self.assertRaises(SandboxError):
                await self.executor(job)
        self.assertEqual(self.projects.versions(self.access, self.pid), [])
        self.assertIsNotNone(
            self.projects.get(self.access, self.pid)["lifecycle_job_id"]
        )

    async def test_no_restore_success_when_blob_checksum_fails(self):
        self.projects.snapshot(
            self.access, self.pid, [ProjectFile("file", b"good")], expected_version=1
        )
        path = self.projects.blobs.path(hashlib.sha256(b"good").hexdigest())
        path.chmod(0o600)
        path.write_bytes(b"bad!")
        self.request()
        with self.assertRaises(ValueError):
            await self.executor(self.claim())
        self.assertEqual(self.docker.serial, 0)

    async def test_restore_rechecks_actual_tree_before_starting_container(self):
        self.projects.snapshot(
            self.access, self.pid, [ProjectFile("file", b"good")], expected_version=1
        )
        self.request()
        call = self.client.project_files

        async def changed(operation, *args, **kwargs):
            result = await call(operation, *args, **kwargs)
            if operation == "restore_finish":
                root = self.volumes.binding(self.scope.key, self.pid)
                (root / "file").write_bytes(b"not the restored bytes")
            return result

        with patch.object(self.client, "project_files", side_effect=changed):
            with self.assertRaises(SandboxError):
                await self.executor(self.claim())
        self.assertEqual(self.docker.serial, 0)

    async def test_missing_staged_blob_prevents_version_publication(self):
        root = await self.wake()
        (root / "report").write_bytes(b"content")
        self.request("stop")
        call = self.client.project_files

        async def missing(operation, *args, **kwargs):
            result = await call(operation, *args, **kwargs)
            if operation == "check":
                self.projects.blobs.path(
                    hashlib.sha256(b"content").hexdigest()
                ).unlink()
            return result

        with patch.object(self.client, "project_files", side_effect=missing):
            with self.assertRaises(FileNotFoundError):
                await self.executor(self.claim())
        self.assertEqual(self.projects.versions(self.access, self.pid), [])
        self.assertEqual((root / "report").read_bytes(), b"content")
