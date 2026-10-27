"""Real journals/Unix RPC with simulated Docker and kernel observations.

These regression tests are not native Linux quota or namespace acceptance.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.local_runtime.files import read_json
from src.sandbox_runtime.client import SandboxClient
from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError, SandboxResult
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumes
from src.sandbox_runtime.protocol import validate_request
from src.sandbox_runtime.server import SandboxRunner
from tests.test_project_volumes import HostFixture
from tests.test_sandbox_runner import INSTANCE, TOKEN, request

IMAGE = "gaoji-app-sandbox:" + "b" * 64


class DockerFixture:
    def __init__(self, manager, volumes):
        self.manager, self.volumes = manager, volumes
        self.containers, self.commands, self.after = {}, [], {}
        self.serial, self.identity_override = 0, None
        self.tee_entered, self.tee_release = None, None

    async def listing(self, owner=None):
        return [
            {
                "sandbox_id": row["Config"]["Labels"]["qqbot.id"],
                "status": "Up 1 second" if row["State"]["Running"] else "Exited (0)",
            }
            for row in self.containers.values()
            if owner is None
            or row["Config"]["Labels"]["qqbot.owner"] == self.manager._owner_hash(owner)
        ]

    def in_use(self, target):
        return any(
            mount.get("Source") == str(target)
            for row in self.containers.values()
            for mount in row["Mounts"]
        )

    async def list_by_label(self, label):
        return await self.listing()

    async def run(self, *arguments, **options):
        command = self.manager._docker_command(arguments)
        self.commands.append(command)
        action = command[1]
        result = SandboxResult("", "", 0)
        if action == "run":
            if "-d" not in command:
                return (
                    result  # bounded readonly-import helper; command assertions below
                )
            self.serial += 1
            labels = dict(
                command[index + 1].split("=", 1)
                for index, part in enumerate(command)
                if part == "--label"
            )
            image = next(
                part for part in command if part.startswith("gaoji-app-sandbox:")
            )
            name = command[command.index("--name") + 1]
            if name in self.containers:
                return SandboxResult("", "Name already in use", 1)
            mounts = []
            for index, part in enumerate(command):
                if part != "--mount":
                    continue
                fields = dict(
                    item.split("=", 1) if "=" in item else (item, True)
                    for item in command[index + 1].split(",")
                )
                mounts.append(
                    {
                        "Type": fields["type"],
                        "Source": fields["source"],
                        "Name": fields["source"],
                        "Destination": fields["target"],
                        "RW": not fields.get("readonly", False),
                        "Propagation": fields.get("bind-propagation", ""),
                    }
                )
            self.containers[name] = {
                "Id": f"{self.serial:064x}",
                "Config": {
                    "Labels": labels,
                    "Image": image,
                    "User": command[command.index("--user") + 1],
                    "Entrypoint": [command[command.index("--entrypoint") + 1]],
                    "Cmd": list(command[command.index(image) + 1 :]),
                },
                "HostConfig": {
                    "ReadonlyRootfs": "--read-only" in command,
                    "Privileged": "--privileged" in command,
                    "CapDrop": [command[command.index("--cap-drop") + 1]],
                    "CapAdd": [],
                    "SecurityOpt": [command[command.index("--security-opt") + 1]],
                    "NetworkMode": command[command.index("--network") + 1],
                    "Memory": 8192 * MIB,
                    "MemorySwap": 8192 * MIB,
                    "NanoCpus": int(command[command.index("--cpus") + 1]) * 10**9,
                    "PidsLimit": int(command[command.index("--pids-limit") + 1]),
                },
                "State": {"Running": True},
                "Mounts": mounts,
            }
            result = SandboxResult(self.containers[name]["Id"], "", 0)
        elif action == "inspect":
            row = self.containers.get(command[-1])
            if row is None:
                return SandboxResult("", "Error: No such object", 1)
            if "--format" in command:
                values = [row["Config"]["Labels"], row["State"]["Running"]]
                if ".Mounts" in command[command.index("--format") + 1]:
                    values.extend((row["Mounts"], "2026-10-27T00:00:00Z"))
            else:
                values = [row]
            result = SandboxResult(json.dumps(values), "", 0)
        elif action in {"start", "stop", "rm"}:
            name, row = next(
                (name, row)
                for name, row in self.containers.items()
                if row["Id"] == command[-1]
            )
            if action == "rm":
                assert "-f" not in command
                assert not row["State"]["Running"]
                del self.containers[name]
            else:
                row["State"]["Running"] = action == "start"
        elif action == "exec":
            if "stat" in command:
                row = next(
                    row for row in self.containers.values() if row["Id"] in command
                )
                path = next(
                    item["Source"]
                    for item in row["Mounts"]
                    if item["Destination"] == "/workspace"
                )
                info = Path(path).stat()
                result = SandboxResult(
                    self.identity_override or f"{info.st_dev}:{info.st_ino}", "", 0
                )
            elif "tee" in command and self.tee_entered is not None:
                self.tee_entered.set()
                await self.tee_release.wait()
        else:
            raise AssertionError(f"Unexpected fixture command: {command}")
        if action in self.after:
            raise self.after.pop(action)
        return result


class ProjectWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gj-pw-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "state").mkdir(mode=0o700)
        self.host = HostFixture()
        self.volumes = ProjectVolumes(
            self.root / "state/project-volumes", INSTANCE, host=self.host
        )
        self.volumes.create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.volumes.mutate("scope", 1, "mount", fence=2)
        self.driver = DockerSandboxManager(
            namespace=INSTANCE, image=IMAGE, network="none", max_file_bytes=MIB
        )
        self.docker = DockerFixture(self.driver, self.volumes)
        self.host.in_use = self.docker.in_use
        self.driver._run = self.docker.run
        self.driver.list = self.docker.listing
        self.driver._list_by_label = self.docker.list_by_label
        self.driver._ensure_nix_volume = AsyncMock()
        self.driver._prepare_workspace_volume = AsyncMock(
            side_effect=AssertionError("No task volume for projects")
        )
        self.driver._remove_volume = AsyncMock(
            side_effect=AssertionError("No project volume deletion by Docker")
        )
        await self.start_runner()
        self.addAsyncCleanup(self.stop_runner)
        self.client = SandboxClient(
            str(self.root / "socket/runner.sock"), TOKEN, max_file_bytes=MIB
        )

    async def start_runner(self):
        self.runner = SandboxRunner(
            self.driver,
            socket_path=self.root / "socket/runner.sock",
            state_directory=self.root / "state",
            token=TOKEN,
            project_volumes=self.volumes,
        )
        await self.runner.start()

    async def stop_runner(self):
        await self.runner.close()

    def journal(self):
        return read_json(self.volumes.root / "p1/volume.json")

    async def operate(self, action, fence):
        return await self.client.project_workspace(
            action, "scope", 1, generation=1, fence=fence
        )

    def container(self):
        return next(iter(self.docker.containers.values()))

    async def test_start_stop_remove_and_rebuild_preserve_project_data(self):
        sentinel = self.volumes.binding("scope", 1) / "report.txt"
        sentinel.write_text("retained project")
        first = await self.operate("start", 3)
        self.assertEqual(first["state"], "running")
        self.assertTrue(first["verified"])
        self.assertEqual(await self.operate("start", 3), first)
        self.assertEqual(self.docker.serial, 1)
        self.assertEqual((await self.operate("stop", 4))["state"], "stopped")
        self.assertEqual(
            (await self.operate("start", 5))["sandbox_id"], first["sandbox_id"]
        )
        await self.operate("stop", 6)
        removed = await self.operate("remove", 7)
        self.assertEqual(removed["state"], "absent")
        self.assertEqual(await self.operate("remove", 7), removed)
        second = await self.operate("start", 8)
        self.assertNotEqual(first["sandbox_id"], second["sandbox_id"])
        self.assertEqual(sentinel.read_text(), "retained project")
        self.assertNotIn(str(self.root), json.dumps(first))
        self.assertNotIn(self.journal()["workspace"]["token"], json.dumps(first))

    async def test_creation_crash_reconciles_same_container_after_runner_restart(self):
        self.docker.after["run"] = RuntimeError(
            "crash after daemon created the container"
        )
        with self.assertRaises(SandboxError):
            await self.operate("start", 3)
        workspace = self.journal()["workspace"]
        await self.stop_runner()
        self.driver.image = "gaoji-app-sandbox:" + "c" * 64
        await self.start_runner()
        result = await self.operate("start", 3)
        self.assertEqual(result["sandbox_id"], workspace["sandbox_id"])
        self.assertEqual(self.container()["Config"]["Image"], IMAGE)
        self.assertEqual(self.docker.serial, 1)
        self.assertFalse(self.journal()["pending"])

    async def test_cancellation_keeps_pending_reservation_and_can_be_reconciled(self):
        self.docker.after["run"] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.driver.project_workspaces.operate(
                "start", "scope", 1, generation=1, fence=3
            )
        self.assertTrue(self.journal()["pending"])
        for operation in ("mount", "unmount", "resize", "delete"):
            with self.assertRaises(SandboxError):
                self.volumes.mutate(
                    "scope",
                    1,
                    operation,
                    fence=4,
                    **({"quota_bytes": 96 * MIB} if operation == "resize" else {}),
                )
        result = await self.operate("start", 3)
        self.assertTrue(result["verified"])
        self.assertEqual(self.docker.serial, 1)

    async def test_pending_creation_protects_volume_before_container_exists(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(image):
            entered.set()
            await release.wait()

        self.driver._ensure_nix_volume = prepare
        operation = asyncio.create_task(
            self.driver.project_workspaces.operate(
                "start", "scope", 1, generation=1, fence=3
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            self.assertFalse(self.docker.containers)
            self.assertTrue(self.journal()["pending"])
            with self.assertRaises(SandboxError):
                self.volumes.mutate("scope", 1, "unmount", fence=4)
        finally:
            release.set()
            await operation

    async def test_failed_rpc_receipt_cannot_duplicate_container_on_replay(self):
        self.docker.after["run"] = RuntimeError("lost completion")
        options = {"generation": 1, "fence": 3, "request_id": "d" * 32}
        for _ in range(2):
            with self.assertRaises(SandboxError):
                await self.client.project_workspace("start", "scope", 1, **options)
        self.assertEqual(self.docker.serial, 1)
        options["request_id"] = "e" * 32
        self.assertTrue(
            (await self.client.project_workspace("start", "scope", 1, **options))[
                "verified"
            ]
        )
        self.assertEqual(self.docker.serial, 1)

    async def test_remove_crash_is_reconciled_without_second_delete(self):
        await self.operate("start", 3)
        await self.operate("stop", 4)
        self.docker.after["rm"] = RuntimeError("crash after removal")
        with self.assertRaises(SandboxError):
            await self.operate("remove", 5)
        self.assertTrue(self.journal()["pending"])
        self.assertEqual((await self.operate("remove", 5))["state"], "absent")
        self.assertEqual(sum(command[1] == "rm" for command in self.docker.commands), 1)
        self.assertFalse(self.volumes.mutate("scope", 1, "unmount", fence=6)["mounted"])

    async def test_generic_lifecycle_cannot_destroy_or_restart_project(self):
        sandbox_id = (await self.operate("start", 3))["sandbox_id"]
        for function in (
            self.driver.destroy,
            self.driver.stop_owned,
            self.driver.start_owned,
        ):
            with self.assertRaises(SandboxError):
                await function("scope", sandbox_id)
        await self.operate("stop", 4)
        with self.assertRaises(SandboxError):
            await self.driver.reclaim_stopped(
                "scope", sandbox_id, eligible=lambda: True
            )
        self.assertEqual(len(self.docker.containers), 1)
        self.assertFalse(any(command[1] == "rm" for command in self.docker.commands))

    async def test_running_removal_rejected_before_creating_an_unresolvable_intent(
        self,
    ):
        await self.operate("start", 3)
        with self.assertRaises(SandboxError):
            await self.operate("remove", 4)
        self.assertEqual(self.journal()["fence"], 3)
        self.assertFalse(self.journal()["pending"])
        await self.operate("stop", 4)

    async def test_wrong_mount_namespace_blocks_readiness_and_file_writes(self):
        self.docker.identity_override = "999:999"
        with self.assertRaisesRegex(SandboxError, "same project mount"):
            await self.operate("start", 3)
        sandbox_id = self.journal()["workspace"]["sandbox_id"]
        with self.assertRaises(SandboxError):
            await self.driver.write_file(
                "scope", sandbox_id, "bad.txt", b"must not write"
            )
        self.assertFalse(any("tee" in command for command in self.docker.commands))
        self.docker.identity_override = None
        self.assertTrue((await self.operate("start", 3))["verified"])

    async def test_acknowledged_start_replay_does_not_restart_a_stopped_container(self):
        await self.operate("start", 3)
        self.container()["State"]["Running"] = False
        with self.assertRaises(SandboxError):
            await self.operate("start", 3)
        self.assertFalse(any(command[1] == "start" for command in self.docker.commands))
        self.assertEqual((await self.operate("start", 4))["state"], "running")

    async def test_owner_generation_and_foreign_mount_are_rejected(self):
        await self.operate("start", 3)
        original = copy.deepcopy(self.container())
        modifications = (
            lambda row: row["Config"]["Labels"].update({"qqbot.owner": "foreign"}),
            lambda row: row["Config"]["Labels"].update(
                {"gaoji.app.workspace-token": "foreign"}
            ),
            lambda row: row["Mounts"][0].update(Source="/elsewhere"),
            lambda row: row["HostConfig"].update(Privileged=True),
            lambda row: row["HostConfig"].update(Memory=0),
        )
        name = next(iter(self.docker.containers))
        for modify in modifications:
            self.docker.containers[name] = copy.deepcopy(original)
            modify(self.container())
            with self.assertRaises(SandboxError):
                await self.operate("stop", 4)
        self.docker.containers[name] = original
        for owner, generation in (("another scope", 1), ("scope", 2)):
            with self.assertRaises(SandboxError):
                await self.client.project_workspace(
                    "stop", owner, 1, generation=generation, fence=4
                )
        self.assertEqual(self.journal()["fence"], 3)

    async def test_missing_container_is_not_reported_as_verified(self):
        await self.operate("start", 3)
        self.docker.containers.clear()
        status = await self.client.project_workspace("status", "scope", 1, generation=1)
        self.assertEqual(status["state"], "missing")
        self.assertFalse(status["verified"])

    async def test_creation_error_does_not_disclose_private_mount_path(self):
        async def fail_creation(*command, **options):
            if command[1] == "inspect":
                return SandboxResult("", "No such container", 1)
            return SandboxResult(
                "", "invalid mount: " + str(self.volumes.root / "p1/mount"), 1
            )

        self.driver._run = fail_creation
        with self.assertRaisesRegex(
            SandboxError, "creation was not confirmed"
        ) as caught:
            await self.operate("start", 3)
        self.assertNotIn(str(self.volumes.root), str(caught.exception))
        self.assertTrue(self.journal()["pending"])
        self.driver._remove_volume.assert_not_awaited()

    def simulate_host_reboot(self):
        self.host.boot = "22222222-2222-4222-8222-222222222222"
        self.host.loop_rows.clear()
        self.host.mount_rows.clear()
        for row in self.docker.containers.values():
            row["State"]["Running"] = False

    async def test_host_reboot_replaces_container_but_not_project_image(self):
        original = await self.operate("start", 3)
        journal = self.journal()
        self.simulate_host_reboot()
        status = await self.client.project_workspace("status", "scope", 1, generation=1)
        self.assertEqual(status["state"], "needs_recovery")
        self.assertFalse(status["verified"])
        self.assertFalse(status["mounted"])
        recovered = await self.operate("start", 4)
        self.assertTrue(recovered["verified"])
        self.assertNotEqual(recovered["sandbox_id"], original["sandbox_id"])
        self.assertEqual(self.journal()["fs_uuid"], journal["fs_uuid"])
        self.assertEqual(self.journal()["image_identity"], journal["image_identity"])
        self.assertEqual(self.journal()["generation"], journal["generation"])
        self.assertEqual(
            self.journal()["workspace"]["recovered_from"], original["sandbox_id"]
        )
        self.assertEqual(self.host.calls.count("format"), 1)
        self.assertIn("fsck", self.host.calls)
        self.assertEqual(len(self.docker.containers), 1)

    async def test_reboot_recovery_resumes_after_each_mount_step(self):
        await self.operate("start", 3)
        self.simulate_host_reboot()
        for step in ("fsck", "attach", "mount"):
            self.host.after[step] = RuntimeError("lost recovery acknowledgement")
            with self.assertRaises(SandboxError):
                await self.operate("start", 4)
            self.assertTrue(self.journal()["pending"])
            with self.assertRaises(SandboxError):
                await self.operate("start", 5)
        result = await self.operate("start", 4)
        self.assertTrue(result["verified"])
        self.assertEqual(self.docker.serial, 2)
        self.assertEqual(sum(command[1] == "rm" for command in self.docker.commands), 1)
        self.assertEqual(self.host.calls.count("attach"), 2)

    async def test_reboot_during_unacknowledged_start_keeps_original_fence(self):
        self.docker.after["run"] = RuntimeError("lost first start")
        with self.assertRaises(SandboxError):
            await self.operate("start", 3)
        self.simulate_host_reboot()
        with self.assertRaises(SandboxError):
            await self.operate("start", 4)
        self.assertTrue((await self.operate("start", 3))["verified"])
        self.assertEqual(self.journal()["fence"], 3)

    async def test_recovery_rejects_running_old_container_and_same_boot_mount_loss(
        self,
    ):
        await self.operate("start", 3)
        self.host.loop_rows.clear()
        self.host.mount_rows.clear()
        with self.assertRaises(SandboxError):
            await self.operate("start", 4)
        self.container()["State"]["Running"] = False
        with self.assertRaises(SandboxError):
            await self.operate("start", 4)
        self.assertEqual(self.journal()["fence"], 3)
        self.assertEqual(len(self.docker.containers), 1)

    async def test_recovery_rejects_unexpected_files_in_unmounted_directory(self):
        await self.operate("start", 3)
        self.simulate_host_reboot()
        orphan = self.volumes.root / "p1/mount/unexpected.txt"
        orphan.write_text("do not hide or delete")
        with self.assertRaises(SandboxError):
            await self.operate("start", 4)
        self.assertEqual(orphan.read_text(), "do not hide or delete")
        self.assertTrue(self.journal()["pending"])

    async def test_stale_stopped_binding_can_be_removed_without_remounting(self):
        await self.operate("start", 3)
        self.simulate_host_reboot()
        self.assertEqual((await self.operate("remove", 4))["state"], "absent")
        self.assertEqual(self.host.calls.count("mount"), 1)
        self.assertIsNone(self.journal()["workspace"])

    async def test_stop_waits_for_no_active_file_transfer(self):
        sandbox_id = (await self.operate("start", 3))["sandbox_id"]
        self.docker.tee_entered, self.docker.tee_release = (
            asyncio.Event(),
            asyncio.Event(),
        )
        writer = asyncio.create_task(
            self.driver.write_file("scope", sandbox_id, "report.txt", b"report")
        )
        try:
            await asyncio.wait_for(self.docker.tee_entered.wait(), timeout=3)
            with self.assertRaisesRegex(SandboxError, "file transfer"):
                await self.operate("stop", 4)
            self.assertEqual(self.journal()["fence"], 3)
        finally:
            self.docker.tee_release.set()
            await writer
        await self.operate("stop", 4)

    async def test_capacity_limit_and_no_unjournalled_project_creation(self):
        self.driver.max_total = 1
        await self.operate("start", 3)
        self.volumes.create("scope", 2, quota_bytes=64 * MIB, fence=1)
        self.volumes.mutate("scope", 2, "mount", fence=2)
        with self.assertRaises(SandboxError):
            await self.client.project_workspace(
                "start", "scope", 2, generation=1, fence=3
            )
        self.assertEqual(self.volumes.status("scope", 2)["fence"], 2)
        with self.assertRaises(SandboxError):
            await self.driver.create("scope", purpose="project")

    async def test_reservation_blocks_unrelated_writers_across_runner_restart(self):
        sandbox_id = (await self.operate("start", 3))["sandbox_id"]
        token = "a" * 32
        reserved = await self.client.project_workspace(
            "reserve", "scope", 1, generation=1, fence=4, reservation=token
        )
        self.assertTrue(reserved["maintenance_active"])
        self.assertNotIn(token, json.dumps(reserved))
        self.assertNotIn(self.journal()["maintenance"], json.dumps(reserved))
        await self.stop_runner()
        await self.start_runner()
        for action in ("start", "stop", "release"):
            with self.assertRaises(SandboxError):
                await self.client.project_workspace(
                    action,
                    "scope",
                    1,
                    generation=1,
                    fence=5,
                    reservation="b" * 32,
                )
        with self.assertRaises(SandboxError):
            await self.operate("stop", 5)
        with self.assertRaises(SandboxError):
            await self.driver.write_file("scope", sandbox_id, "report.txt", b"race")
        with self.assertRaises(SandboxError):
            self.volumes.binding("scope", 1)
        with self.assertRaises(SandboxError):
            await self.client.project_volume("trim", "scope", 1, fence=5)
        self.assertEqual(self.journal()["fence"], 4)
        await self.client.project_workspace(
            "stop", "scope", 1, generation=1, fence=5, reservation=token
        )
        with self.assertRaises(SandboxError):
            await self.operate("start", 6)
        await self.client.project_workspace(
            "release", "scope", 1, generation=1, fence=6, reservation=token
        )
        self.assertFalse(self.journal()["maintenance"])
        self.assertEqual((await self.operate("start", 7))["sandbox_id"], sandbox_id)

    async def test_deleted_volume_keeps_reservation_until_owned_release(self):
        token = "a" * 32
        await self.client.project_workspace(
            "reserve", "scope", 1, generation=1, fence=3, reservation=token
        )
        for fence, action in enumerate(("unmount", "delete"), 4):
            await self.client.project_volume(
                action, "scope", 1, fence=fence, reservation=token
            )
        with self.assertRaises(SandboxError):
            self.volumes.create("scope", 1, generation=2, fence=6, quota_bytes=64 * MIB)
        for generation, fence in ((2, 6), (1, 4)):
            with self.assertRaises(SandboxError):
                await self.client.project_workspace(
                    "release",
                    "scope",
                    1,
                    generation=generation,
                    fence=fence,
                    reservation=token,
                )
        for _ in range(2):
            result = await self.client.project_workspace(
                "release", "scope", 1, generation=1, fence=6, reservation=token
            )
            self.assertFalse(result["maintenance_active"])
            self.assertTrue(result["deleted"])
        self.volumes.create("scope", 1, generation=2, fence=7, quota_bytes=64 * MIB)
        with self.assertRaises(SandboxError):
            await self.client.project_workspace(
                "release", "scope", 1, generation=1, fence=8, reservation=token
            )

    async def test_reservation_crash_replays_persisted_fence(self):
        token = "a" * 32
        for action, fence in (("reserve", 3), ("release", 4)):
            with patch.object(
                self.volumes, "_finish", side_effect=RuntimeError("lost completion")
            ):
                with self.assertRaises(SandboxError):
                    await self.client.project_workspace(
                        action, "scope", 1, generation=1, fence=fence, reservation=token
                    )
            self.assertTrue(self.journal()["pending"])
            await self.stop_runner()
            await self.start_runner()
            result = await self.client.project_workspace(
                action, "scope", 1, generation=1, fence=fence, reservation=token
            )
            self.assertFalse(result["pending"])
            self.assertEqual(result["maintenance_active"], action == "reserve")

    async def test_reservation_holds_io_lock_until_journal_is_durable(self):
        sandbox_id = (await self.operate("start", 3))["sandbox_id"]
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = self.volumes.reserve

        def delayed(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("test journal release")
            return original(*args, **kwargs)

        with patch.object(self.volumes, "reserve", side_effect=delayed):
            reserver = asyncio.create_task(
                self.client.project_workspace(
                    "reserve", "scope", 1, generation=1, fence=4, reservation="a" * 32
                )
            )
            writer = None
            try:
                await asyncio.wait_for(entered.wait(), 3)
                writer = asyncio.create_task(
                    self.driver.write_file("scope", sandbox_id, "late.txt", b"race")
                )
                await asyncio.sleep(0)
                self.assertFalse(writer.done())
            finally:
                release.set()
                await reserver
                if writer is not None:
                    with self.assertRaises(SandboxError):
                        await writer
        self.assertFalse(any("tee" in command for command in self.docker.commands))

    async def test_reservation_refuses_an_active_file_transfer(self):
        sandbox_id = (await self.operate("start", 3))["sandbox_id"]
        self.docker.tee_entered, self.docker.tee_release = (
            asyncio.Event(),
            asyncio.Event(),
        )
        writer = asyncio.create_task(
            self.driver.write_file("scope", sandbox_id, "file", b"busy")
        )
        try:
            await asyncio.wait_for(self.docker.tee_entered.wait(), 3)
            with self.assertRaises(SandboxError):
                await self.client.project_workspace(
                    "reserve", "scope", 1, generation=1, fence=4, reservation="a" * 32
                )
            self.assertFalse(self.journal().get("maintenance"))
        finally:
            self.docker.tee_release.set()
            await writer

    async def test_reservation_protocol_requires_a_private_canonical_token(self):
        args = {"owner": "scope", "project_id": 1, "generation": 1, "fence": 3}
        for action in ("reserve", "release"):
            method = "project_workspace_" + action
            validate_request(
                request(method, {**args, "reservation": "a" * 32}), file_limit=MIB
            )
            for changes in (
                {},
                {"reservation": "wrong"},
                {"reservation": True},
                {"reservation": "a" * 32, "command": "sh"},
            ):
                with self.assertRaises(SandboxError):
                    validate_request(
                        request(method, {**args, **changes}), file_limit=MIB
                    )

    async def test_readonly_import_helper_uses_project_mount_and_identity_guard(self):
        sandbox_id = (await self.operate("start", 3))["sandbox_id"]
        await self.driver.install_readonly_file(
            "scope", sandbox_id, "upstream/report.txt", b"artifact"
        )
        helper = self.docker.commands[-1]
        self.assertEqual(helper[1], "run")
        self.assertNotIn("-d", helper)
        self.assertIn("--read-only", helper)
        self.assertIn(
            "type=bind,source="
            + str(self.volumes.binding("scope", 1))
            + ",target=/workspace,bind-propagation=rprivate",
            helper,
        )
        self.assertIn("Project mount mismatch", helper[helper.index("-c") + 1])
        self.assertEqual(helper[helper.index("--entrypoint") + 1], "python")

    async def test_protocol_never_accepts_mount_image_command_or_optional_generation(
        self,
    ):
        args = {"owner": "scope", "project_id": 1, "generation": 1, "fence": 3}
        validate_request(request("project_workspace_start", args), file_limit=MIB)
        for changes in (
            {"path": "/etc"},
            {"image": IMAGE},
            {"command": "sh"},
            {"generation": True},
            {"fence": False},
        ):
            with self.assertRaises(SandboxError):
                validate_request(
                    request("project_workspace_start", {**args, **changes}),
                    file_limit=MIB,
                )
        del args["generation"]
        with self.assertRaises(SandboxError):
            validate_request(request("project_workspace_start", args), file_limit=MIB)


if __name__ == "__main__":
    unittest.main()
