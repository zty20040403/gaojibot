"""PostgreSQL/Unix integration, simulated mount persistence and Docker execution."""

import asyncio
import hashlib
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_maintenance import ProjectMaintenance
from src.plugins.ai_chat.project_policy import DAY
from src.plugins.ai_chat.projects import ProjectConflict
from src.sandbox_runtime.docker import SandboxError
from tests import test_project_file_executor as fixture
from tests.test_project_dependencies import DependencyDockerFixture
from tests.test_project_volumes import HostFixture


class PersistentMountFixture(HostFixture):
    """Model hidden unmounted contents, not a native filesystem acceptance test."""

    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        directory.mkdir()

    def contents(self, device):
        image = next(r["back-file"] for r in self.loop_rows if r["name"] == device)
        directory = self.directory / hashlib.sha256(image.encode()).hexdigest()
        directory.mkdir(exist_ok=True)
        return directory

    def mount(self, device, target):
        for path in self.contents(device).iterdir():
            shutil.move(path, target / path.name)
        super().mount(device, target)

    def unmount(self, target):
        device = next(
            r["source"] for r in self.mount_rows if r["target"] == str(target)
        )
        for path in target.iterdir():
            shutil.move(path, self.contents(device) / path.name)
        super().unmount(target)


class ProjectDependencyExecutorTests(unittest.IsolatedAsyncioTestCase):
    claim = fixture.ProjectFileExecutorTests.claim
    wake = fixture.ProjectFileExecutorTests.wake
    configure_rebuild = fixture.ProjectFileExecutorTests.configure_rebuild
    dependency_project = fixture.ProjectFileExecutorTests.dependency_project

    async def asyncSetUp(self):
        await fixture.ProjectFileExecutorTests.asyncSetUp(self)
        self.requests = 0
        self.host = PersistentMountFixture(self.root / "fixture-disks")
        self.volumes.host = self.host
        self.docker = DependencyDockerFixture(self.driver, self.volumes)
        self.host.in_use = self.docker.in_use
        self.driver._run = self.docker.run
        self.driver.list = self.docker.listing
        self.driver._list_by_label = self.docker.list_by_label
        self.driver._prepare_packages = AsyncMock(return_value=())

    def request(self, action="wake"):
        self.requests += 1
        project = self.projects.get(self.access, self.pid)
        return self.lifecycle.request(
            self.access,
            self.pid,
            action,
            expected_version=project["version"],
            request_key=f"{action}-{self.requests}",
        )

    def seed_locked_version(self):
        files = {
            "package.json": b'{"name":"fixture"}',
            "package-lock.json": b'{"lockfileVersion":3}',
            "source.js": b"retained source",
        }
        return self.projects.snapshot(
            self.access,
            self.pid,
            [ProjectFile(path, data) for path, data in files.items()],
            expected_version=1,
            rebuild={
                "command": "npm ci",
                "locks": [
                    {"path": path, "sha256": hashlib.sha256(files[path]).hexdigest()}
                    for path in sorted(files)
                    if path.endswith(".json")
                ],
                "excluded": [],
            },
        )

    def rebuild_step(self):
        return self.sql.execute(
            "SELECT * FROM project_lifecycle_steps WHERE operation='rebuild_dependencies' ORDER BY job_id DESC LIMIT 1"
        ).fetchone()

    async def test_automatic_idle_cycle_uses_private_helper_and_restores_retained_files(self):
        root = await self.wake()
        (root / "source.txt").write_bytes(b"automatic retained content")
        maintenance = ProjectMaintenance(
            self.lifecycle, {"oci": self.backend}, enabled=True,
            resolve_access=lambda project, actor: self.access,
        )
        for age, action, state in ((1801, "stop", "stopped"), (7 * DAY + 1, "hibernate", "hibernated"),
                                   (60 * DAY + 1, "archive", "archived")):
            now = int(time.time())
            self.sql.execute("UPDATE projects SET last_active_at=? WHERE project_id=?", (now - age, self.pid))
            result = await maintenance.run_once(now=now)
            self.assertEqual(result[0].get("action"), action, result)
            await self.executor(self.claim())
            project = self.projects.get(self.access, self.pid)
            self.assertEqual(project["status"], state)
            self.assertEqual(project["last_active_at"], now - age)
        self.assertEqual(len(self.docker.containers), 0)
        self.assertTrue(await self.backend.maintenance_ready(project))
        self.request("wake")
        await self.executor(self.claim())
        restored = self.volumes.binding(self.scope.key, self.pid, generation=2)
        self.assertEqual((restored / "source.txt").read_bytes(), b"automatic retained content")

    async def test_helper_reports_busy_command_and_file_transfer_without_reclaiming(self):
        await self.wake()
        project = self.projects.get(self.access, self.pid)
        self.assertTrue(await self.backend.maintenance_ready(project))
        lock = self.driver._exec_locks.setdefault(project["sandbox_id"], asyncio.Lock())
        async with lock:
            self.assertFalse(await self.backend.maintenance_ready(project))
        with patch.object(self.driver, "_ensure_idle", side_effect=SandboxError("fixture command active")):
            self.assertFalse(await self.backend.maintenance_ready(project))
        self.assertTrue(await self.backend.maintenance_ready(project))

    async def test_usage_rpc_measures_mounted_and_offline_without_remount(self):
        await self.wake()
        project = self.projects.get(self.access, self.pid)
        self.host.measured_used_bytes = 123456
        sample = await self.backend.sample_usage(project)
        self.assertEqual(sample["used_bytes"], 123456)
        self.assertTrue(sample["mounted"])
        with self.assertRaises(SandboxError):
            await self.client.project_usage("foreign", self.pid, generation=project["workspace_generation"])
        self.request("stop")
        await self.executor(self.claim())
        self.request("hibernate")
        await self.executor(self.claim())
        calls = list(self.host.calls)
        sample = await self.backend.sample_usage(self.projects.get(self.access, self.pid))
        self.assertEqual(sample["used_bytes"], 123456)
        self.assertIsNone(sample["rebuildable_bytes"])
        self.assertFalse(sample["mounted"])
        self.assertEqual(self.host.calls, calls)

    async def test_95_percent_blocks_actual_installer_creation_not_just_ui(self):
        self.seed_locked_version()
        quota = self.projects.get(self.access, self.pid)["quota_bytes"]
        self.host.measured_used_bytes = (quota * 95 + 99) // 100
        self.request()
        with self.assertRaisesRegex(SandboxError, "project_dependency_limit"):
            await self.executor(self.claim())
        self.assertFalse(any(c[1] == "create" for c in self.docker.actions))
        self.assertEqual(self.projects.get(self.access, self.pid)["status"], "stopped")
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "source.js"), b"retained source")

    async def test_old_helper_without_busy_observation_is_not_eligible(self):
        await self.wake()
        project = self.projects.get(self.access, self.pid)
        original = self.client.project_workspace

        async def old(*args, **kwargs):
            result = await original(*args, **kwargs)
            result.pop("busy", None)
            return result

        with patch.object(self.client, "project_workspace", side_effect=old):
            self.assertFalse(await self.backend.maintenance_ready(project))

    async def test_fresh_restore_rebuilds_before_start_with_published_evidence(self):
        version = self.seed_locked_version()
        self.driver.max_per_owner = 1
        run = self.request()
        steps = run["plan"]["steps"]
        self.assertLess(
            steps.index("restore_version"), steps.index("rebuild_dependencies")
        )
        self.assertLess(
            steps.index("rebuild_dependencies"), steps.index("start_workspace")
        )
        await self.executor(self.claim())
        root = self.volumes.binding(self.scope.key, self.pid)
        self.assertEqual((root / "source.js").read_bytes(), b"retained source")
        self.assertEqual((root / "node_modules/fixture").read_bytes(), b"reconstructed")
        row = self.rebuild_step()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["evidence"]["exit_code"], 0)
        self.assertEqual(
            row["evidence"]["rebuilt_version"]["manifest_sha256"],
            version["manifest_sha256"],
        )
        self.assertEqual(len(self.docker.containers), 1)
        self.assertNotIn(
            "gaoji.app.rebuild-fence",
            next(iter(self.docker.containers.values()))["Config"]["Labels"],
        )

    async def test_full_hibernate_and_archive_roundtrip_retains_recipe(self):
        await self.dependency_project()
        for action in ("hibernate", "wake", "stop", "hibernate", "archive", "wake"):
            self.request(action)
            await self.executor(self.claim())
            if action == "wake":
                root = self.volumes.binding(
                    self.scope.key,
                    self.pid,
                    generation=self.projects.get(self.access, self.pid)[
                        "workspace_generation"
                    ],
                )
                self.assertEqual((root / "source.js").read_bytes(), b"retained source")
                self.assertEqual(
                    (root / "node_modules/fixture").read_bytes(), b"reconstructed"
                )
                self.assertEqual(len(self.docker.containers), 1)
        self.assertEqual(
            self.projects.get(self.access, self.pid)["workspace_generation"], 2
        )
        self.assertEqual(sum(c[1] == "create" for c in self.docker.actions), 2)

    async def test_missing_lock_keeps_dependencies_without_running_an_installer(self):
        await self.dependency_project(locked=False)
        self.request("hibernate")
        await self.executor(self.claim())
        run = self.request()
        self.assertNotIn("rebuild_dependencies", run["plan"]["steps"])
        await self.executor(self.claim())
        root = self.volumes.binding(self.scope.key, self.pid)
        self.assertEqual(
            (root / "node_modules/pkg/data").read_bytes(), b"dependency bytes"
        )
        self.assertFalse(any(c[1] == "create" for c in self.docker.actions))

    async def test_failure_records_progress_but_never_starts_workspace(self):
        self.seed_locked_version()
        self.docker.exit_code = 42
        self.request()
        with self.assertRaises(ProjectConflict):
            await self.executor(self.claim())
        row = self.rebuild_step()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["evidence"]["progress"]["exit_code"], 42)
        self.assertEqual(row["evidence"]["progress"]["state"], "failed")
        self.assertEqual(
            self.lifecycle.journal(self.access, self.pid)[0]["status"],
            "needs_attention",
        )
        self.assertEqual(self.projects.get(self.access, self.pid)["status"], "stopped")
        self.assertFalse(self.docker.containers)
        self.assertTrue(
            self.volumes.status(self.scope.key, self.pid)["maintenance_active"]
        )
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "source.js"),
            b"retained source",
        )

    async def test_lost_completion_ack_recovers_without_second_install(self):
        self.seed_locked_version()
        self.request()
        call = self.client.project_rebuild

        async def lost(operation, *args, **kwargs):
            result = await call(operation, *args, **kwargs)
            if operation == "run":
                raise SandboxError("fixture lost completion response")
            return result

        with patch.object(self.client, "project_rebuild", side_effect=lost):
            with self.assertRaises(SandboxError):
                await self.executor(self.claim())
        await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(sum(c[1] == "create" for c in self.docker.actions), 1)
        self.assertEqual(self.projects.get(self.access, self.pid)["status"], "active")

    async def test_running_progress_survives_worker_cancellation_and_lease_takeover(
        self,
    ):
        self.seed_locked_version()
        self.docker.automatic = False
        self.request()
        job = self.claim()
        published = asyncio.Event()
        loop = asyncio.get_running_loop()
        record = self.lifecycle.record_progress
        dispatched = []

        def observe(*args):
            record(*args)
            dispatched.append(args[1])
            loop.call_soon_threadsafe(published.set)

        with patch.object(self.lifecycle, "record_progress", side_effect=observe):
            running = asyncio.create_task(self.executor(job))
            try:
                await asyncio.wait_for(published.wait(), 10)
            finally:
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
        row = self.rebuild_step()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["evidence"]["progress"]["state"], "running")
        successor = self.claim(now=int(time.time()) + 61)
        with self.assertRaises(ProjectConflict):
            record(job, dispatched[0], {"state": "completed"})
        self.docker.complete()
        await self.executor(successor)
        self.assertEqual(sum(c[1] == "create" for c in self.docker.actions), 1)
        self.assertEqual(self.projects.get(self.access, self.pid)["status"], "active")

    async def test_cached_completion_requires_fresh_retained_file_verification(self):
        self.seed_locked_version()
        self.request()
        call = self.client.project_rebuild

        async def changed(operation, *args, **kwargs):
            result = await call(operation, *args, **kwargs)
            if operation == "run":
                root = next(iter(self.host.mount_rows))["target"]
                (Path(root) / "source.js").write_bytes(b"changed after success")
            return result

        with patch.object(self.client, "project_rebuild", side_effect=changed):
            with self.assertRaises(SandboxError):
                await self.executor(self.claim())
        self.assertEqual(self.projects.get(self.access, self.pid)["status"], "stopped")
        self.assertFalse(self.docker.containers)
