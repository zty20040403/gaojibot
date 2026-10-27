"""Real directory journals with explicit Docker fixtures; not Desktop acceptance."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError
from src.sandbox_runtime.project_desktop_workspaces import DesktopProjectWorkspaces
from src.sandbox_runtime.project_directory import (
    DirectoryProjectVolumes,
    DesktopVolumeHost,
)
from src.sandbox_runtime.project_files import ProjectFiles
from src.sandbox_runtime.project_usage import ProjectUsage
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumeError
from tests.test_project_volumes import HostFixture
from tests.test_project_workspaces import DockerFixture, IMAGE
from tests.test_project_dependencies import DependencyDockerFixture
from tests.test_sandbox_runner import INSTANCE


class DesktopHostFixture(HostFixture):
    def __init__(self, root):
        super().__init__()
        self.data_root = root
        self.volume = "gaoji-desktop-" + "c" * 32 + "-data"

    subpath = DesktopVolumeHost.subpath


class DesktopDockerFixture(DockerFixture):
    async def run(self, *arguments, **options):
        result = await super().run(*arguments, **options)
        command = self.manager._docker_command(arguments)
        if command[1] == "run" and "-d" in command:
            row = self.containers[command[command.index("--name") + 1]]
            mounts = []
            for index, value in enumerate(command):
                if value != "--mount":
                    continue
                fields = dict(
                    item.split("=", 1) if "=" in item else (item, True)
                    for item in command[index + 1].split(",")
                )
                if fields["target"] != "/workspace":
                    continue
                mount = next(
                    m for m in row["Mounts"] if m["Destination"] == "/workspace"
                )
                mount["Source"] = str(
                    self.volumes.host.data_root / fields["volume-subpath"]
                )
                mounts.append(
                    {
                        "Type": "volume",
                        "Source": fields["source"],
                        "Target": "/workspace",
                        "VolumeOptions": {
                            "NoCopy": True,
                            "Subpath": fields["volume-subpath"],
                        },
                    }
                )
            row["HostConfig"]["Mounts"] = mounts
        return result


class DesktopDependencyFixture(DependencyDockerFixture, DesktopDockerFixture):
    pass


class DirectoryVolumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gj-soft-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.host = DesktopHostFixture(self.root)
        self.volumes = DirectoryProjectVolumes(
            self.root / "projects", INSTANCE, host=self.host
        )
        self.created = self.volumes.create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.volumes.mutate("scope", 1, "mount", fence=2)
        self.target = self.volumes.binding("scope", 1)

    def test_roundtrip_fences_generation_and_soft_usage(self):
        (self.target / "notes.txt").write_text("retained")
        usage = ProjectUsage(self.volumes).measure("scope", 1, generation=1)
        self.assertEqual(usage["quota_mode"], "soft")
        self.assertGreater(usage["used_bytes"], 0)
        self.assertEqual(usage["fence"], 2)
        self.assertEqual(self.created["quota_mode"], "soft")
        reopened = DirectoryProjectVolumes(
            self.root / "projects", INSTANCE, host=self.host
        )
        self.assertEqual(reopened.binding("scope", 1), self.target)
        for options in ({"generation": 2}, {"fence": 1}):
            with self.assertRaises(ProjectVolumeError):
                reopened.mutate("scope", 1, "unmount", **({"fence": 3} | options))
        with self.assertRaises(ProjectVolumeError):
            reopened.status("other", 1)
        self.volumes.mutate("scope", 1, "unmount", fence=3)
        self.volumes.mutate("scope", 1, "delete", fence=4)
        self.assertFalse(self.target.exists())
        self.assertTrue(self.volumes.mutate("scope", 1, "delete", fence=4)["deleted"])
        new = self.volumes.create(
            "scope", 1, quota_bytes=64 * MIB, fence=5, generation=2
        )
        self.assertEqual(new["generation"], 2)

    def test_directory_substitution_and_symlink_are_rejected(self):
        original = self.target.with_name("saved")
        self.target.rename(original)
        self.target.mkdir(mode=0o700)
        with self.assertRaises(ProjectVolumeError):
            self.volumes.status("scope", 1)
        self.target.rmdir()
        self.target.symlink_to(original)
        with self.assertRaises(ProjectVolumeError):
            self.volumes.mutate("scope", 1, "unmount", fence=3)
        self.assertTrue(original.exists())

    def test_soft_usage_can_exceed_quota_and_admission_never_clears_it(self):
        with patch(
            "src.sandbox_runtime.project_directory.allocated_bytes",
            return_value=70 * MIB,
        ):
            self.assertEqual(
                self.volumes.measure("scope", 1, generation=1)["used_bytes"], 70 * MIB
            )
            with self.assertRaisesRegex(ProjectVolumeError, "project_quota_full"):
                self.volumes.require_quota("scope", 1, generation=1)
        with patch(
            "src.sandbox_runtime.project_directory.allocated_bytes",
            return_value=61 * MIB,
        ):
            with self.assertRaisesRegex(
                ProjectVolumeError, "project_quota_dependencies"
            ):
                self.volumes.require_quota("scope", 1, generation=1, dependencies=True)
            with self.assertRaisesRegex(ProjectVolumeError, "project_quota_full"):
                self.volumes.require_quota("scope", 1, generation=1, incoming=4 * MIB)
        with patch(
            "src.sandbox_runtime.project_directory.allocated_bytes",
            side_effect=OSError("unreadable"),
        ):
            with self.assertRaisesRegex(ProjectVolumeError, "project_quota_unknown"):
                self.volumes.require_quota("scope", 1, generation=1)

    def test_readonly_snapshot_uses_existing_reservations(self):
        (self.target / "NOTES.md").write_text("keep this")
        token = "a" * 32
        self.volumes.reserve(
            "scope", 1, generation=1, fence=3, reservation=token, action="reserve"
        )
        with self.assertRaises(ProjectVolumeError):
            self.volumes.require_quota("scope", 1, generation=1)
        result, _ = ProjectFiles(self.volumes).operate(
            "snapshot", "scope", 1, generation=1, fence=4, reservation=token
        )
        self.assertTrue(result)
        self.assertEqual((self.target / "NOTES.md").read_text(), "keep this")

    def test_busy_shrink_and_external_consumer_are_rejected(self):
        self.host.busy = True
        with self.assertRaises(ProjectVolumeError):
            self.volumes.mutate("scope", 1, "unmount", fence=3)
        self.host.busy = False
        self.volumes.mutate("scope", 1, "unmount", fence=3)
        self.volumes.mutate("scope", 1, "resize", fence=4, quota_bytes=128 * MIB)
        with patch(
            "src.sandbox_runtime.project_directory.allocated_bytes",
            return_value=70 * MIB,
        ):
            with self.assertRaises(ProjectVolumeError):
                self.volumes.mutate("scope", 1, "resize", fence=5, quota_bytes=64 * MIB)


class DesktopWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gj-soft-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.host = DesktopHostFixture(self.root)
        self.volumes = DirectoryProjectVolumes(
            self.root / "project-volumes", INSTANCE, host=self.host
        )
        self.volumes.create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.volumes.mutate("scope", 1, "mount", fence=2)
        self.target = self.volumes.binding("scope", 1)
        self.manager = DockerSandboxManager(
            namespace=INSTANCE, image=IMAGE, network="none", max_file_bytes=MIB
        )
        self.docker = DesktopDockerFixture(self.manager, self.volumes)
        self.manager._run = self.docker.run
        self.manager.list = self.docker.listing
        self.manager._list_by_label = self.docker.list_by_label
        self.manager._ensure_nix_volume = AsyncMock()
        self.host.in_use = self.docker.in_use
        self.workspaces = DesktopProjectWorkspaces(self.manager, self.volumes)
        self.manager.project_workspaces = self.workspaces
        result = await self.workspaces.operate(
            "start", "scope", 1, generation=1, fence=3
        )
        self.sid = result["sandbox_id"]

    async def test_subpath_isolation_and_boot_recovery(self):
        row = next(iter(self.docker.containers.values()))
        mount = row["HostConfig"]["Mounts"][0]
        self.assertTrue(mount["VolumeOptions"]["NoCopy"])
        self.assertEqual(
            mount["VolumeOptions"]["Subpath"], self.host.subpath(self.target)
        )
        (self.target / "keep").write_text("still here")
        await self.workspaces.operate("stop", "scope", 1, generation=1, fence=4)
        self.host.boot = "22222222-2222-4222-8222-222222222222"
        result = await self.workspaces.operate(
            "start", "scope", 1, generation=1, fence=5
        )
        self.assertNotEqual(result["sandbox_id"], self.sid)
        self.assertEqual((self.target / "keep").read_text(), "still here")

    async def test_write_and_exec_refused_before_dispatch(self):
        for operation in (
            lambda: self.manager.exec("scope", self.sid, "echo test"),
            lambda: self.manager.write_file("scope", self.sid, "file", b"x"),
            lambda: self.manager.install_readonly_file(
                "scope", self.sid, "upstream/file", b"x"
            ),
        ):
            self.docker.commands.clear()
            with patch(
                "src.sandbox_runtime.project_directory.allocated_bytes",
                return_value=64 * MIB,
            ):
                with self.assertRaisesRegex(SandboxError, "project_quota_full"):
                    await operation()
            self.assertFalse(
                any(
                    "tee" in command or "echo test" in command
                    for command in self.docker.commands
                )
            )

    async def test_quota_stop_does_not_wait_for_active_writer_lock_or_delete_data(self):
        (self.target / "keep").write_text("retained")
        async with self.manager._exec_locks.setdefault(self.sid, asyncio.Lock()):
            with patch(
                "src.sandbox_runtime.project_directory.allocated_bytes",
                return_value=64 * MIB,
            ):
                await asyncio.wait_for(
                    self.workspaces.enforce("scope", 1, 1), timeout=3
                )
        row = next(iter(self.docker.containers.values()))
        self.assertFalse(row["State"]["Running"])
        self.assertEqual((self.target / "keep").read_text(), "retained")
        self.assertTrue(
            any(
                command[1] == "stop" and command[-1] == row["Id"]
                for command in self.docker.commands
            )
        )

    async def test_unreadable_usage_stops_only_verified_project(self):
        with patch(
            "src.sandbox_runtime.project_directory.allocated_bytes",
            side_effect=OSError("lost"),
        ):
            await self.workspaces.enforce("scope", 1, 1)
        self.assertFalse(
            next(iter(self.docker.containers.values()))["State"]["Running"]
        )

    async def test_wrong_subpath_never_authorizes_stop(self):
        row = next(iter(self.docker.containers.values()))
        row["HostConfig"]["Mounts"][0]["VolumeOptions"]["Subpath"] = "other-project"
        with self.assertRaises(SandboxError):
            await self.workspaces.enforce("scope", 1, 1)
        self.assertTrue(row["State"]["Running"])
        self.assertFalse(any(command[1] == "stop" for command in self.docker.commands))

    async def test_empty_docker_driver_config_is_not_a_custom_mount(self):
        row = next(iter(self.docker.containers.values()))
        options = row["HostConfig"]["Mounts"][0]["VolumeOptions"]
        options["DriverConfig"] = {}
        self.assertIsNotNone(await self.workspaces.for_sandbox("scope", self.sid))
        options["DriverConfig"] = {"Name": "foreign"}
        with self.assertRaises(SandboxError):
            await self.workspaces.for_sandbox("scope", self.sid)

    async def test_inventory_survives_restart_without_task_records(self):
        reopened = DirectoryProjectVolumes(
            self.root / "project-volumes", INSTANCE, host=self.host
        )
        self.assertEqual(reopened.projects(), [("scope", 1, 1)])
        with self.assertRaises(ProjectVolumeError):
            reopened.status("scope", 999)
        self.assertEqual(reopened.projects(), [("scope", 1, 1)])
        self.assertNotIn("owner", reopened.status("scope", 1))

    async def test_inspection_failure_does_not_kill_watch_and_cancel_still_works(self):
        with patch.object(self.volumes, "projects", side_effect=[RuntimeError("fixture"), []]) as inventory:
            with patch("src.sandbox_runtime.project_desktop_workspaces.asyncio.sleep",
                       side_effect=[None, asyncio.CancelledError]) as sleep:
                with self.assertLogs("src.sandbox_runtime.project_desktop_workspaces", level="ERROR"):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.workspaces.watch()
        self.assertEqual(inventory.call_count, 2)
        self.assertEqual(sleep.call_args.args, (30,))

    async def test_private_rpc_reports_soft_quota_and_closes_watch(self):
        from src.sandbox_runtime.client import SandboxClient
        from src.sandbox_runtime.server import SandboxRunner
        from tests.test_sandbox_runner import TOKEN

        runner = SandboxRunner(
            self.manager,
            socket_path=self.root / "socket/runner.sock",
            state_directory=self.root,
            token=TOKEN,
            project_volumes=self.volumes,
        )
        await runner.start()
        watcher = runner.quota_watch
        try:
            client = SandboxClient(str(runner.socket_path), TOKEN)
            self.assertEqual((await client.health())["project_quota_mode"], "soft")
            usage = await client.project_usage("scope", 1, generation=1)
            self.assertEqual(usage["quota_mode"], "soft")
            self.assertTrue(usage["complete"])
        finally:
            await runner.close()
        self.assertTrue(watcher.done())

    async def test_dependency_writer_is_also_stopped_and_not_replayed(self):
        from src.sandbox_runtime.project_dependencies import ProjectDependencies

        await self.workspaces.operate("stop", "scope", 1, generation=1, fence=4)
        await self.workspaces.operate("remove", "scope", 1, generation=1, fence=5)
        self.docker = DesktopDependencyFixture(self.manager, self.volumes)
        self.docker.automatic = False
        self.manager._run = self.docker.run
        self.manager.list = self.docker.listing
        self.manager._list_by_label = self.docker.list_by_label
        self.manager._prepare_packages = AsyncMock(return_value=())
        self.host.in_use = self.docker.in_use
        token = "a" * 32
        self.volumes.reserve(
            "scope", 1, generation=1, fence=6, reservation=token, action="reserve"
        )
        (self.target / "package.json").write_text('{"name":"fixture"}')
        (self.target / "package-lock.json").write_text('{"lockfileVersion":3}')
        snapshot, _ = ProjectFiles(self.volumes).operate(
            "snapshot",
            "scope",
            1,
            generation=1,
            fence=7,
            reservation=token,
            rebuild_command="npm ci",
        )
        arguments = dict(
            generation=1,
            fence=8,
            reservation=token,
            manifest_sha256=snapshot["manifest_sha256"],
        )
        dependencies = ProjectDependencies(self.workspaces)
        first = await dependencies.operate("run", "scope", 1, **arguments)
        self.assertEqual(first["state"], "running")
        self.assertEqual(self.volumes.projects(), [("scope", 1, 1)])
        with patch(
            "src.sandbox_runtime.project_directory.allocated_bytes",
            return_value=64 * MIB,
        ):
            await self.workspaces.enforce("scope", 1, 1)
        self.assertFalse(self.docker.dependency()["State"]["Running"])
        result = await dependencies.operate("run", "scope", 1, **arguments)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            sum(command[1] == "create" for command in self.docker.actions), 1
        )


if __name__ == "__main__":
    unittest.main()
