"""PostgreSQL + private Unix files; explicit fake Docker/kernel, no QQ/model calls."""

import asyncio
import hashlib
import os
import time
import unittest
from unittest.mock import patch

import nonebot

nonebot.init()

from src.bot_storage import open_store_connection
from src.project_content import CHUNK_BYTES
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_file_executor import DockerProjectFiles
from src.plugins.ai_chat.projects import ProjectQuotaExceeded
from src.sandbox_runtime.docker import SandboxError
from tests import test_project_executor as fixture
from tests.postgres_fixture import postgres_for_test


class ProjectFileExecutorTests(unittest.IsolatedAsyncioTestCase):
    request = fixture.ProjectExecutorTests.request
    claim = fixture.ProjectExecutorTests.claim

    async def asyncSetUp(self):
        await fixture.ProjectExecutorTests.asyncSetUp(self)
        self.backend.files = DockerProjectFiles(
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
