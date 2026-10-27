"""Volume journals and real Unix RPC; host utilities are an explicit fixture.

These tests do not prove kernel quota/mount behavior. Native helper acceptance
is a separate M2 gate; no production server or Docker daemon is contacted here.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.local_runtime.files import read_json
from src.sandbox_runtime.client import SandboxClient
from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError
from src.sandbox_runtime.project_volumes import (
    MIB,
    LinuxVolumeHost,
    ProjectVolumeError,
    ProjectVolumes,
)
from src.sandbox_runtime.protocol import validate_request
from src.sandbox_runtime.server import SandboxRunner
from tests.test_sandbox_runner import request, INSTANCE, TOKEN


class HostFixture:
    def __init__(self):
        self.filesystems = {}
        self.loop_rows, self.mount_rows, self.calls = [], [], []
        self.busy = False
        self.after = {}
        self.next_device = 20

    def done(self, operation):
        self.calls.append(operation)
        if operation in self.after:
            raise self.after.pop(operation)

    def format(self, image, fs_uuid):
        with image.open("r+b") as stream:
            stream.write(b"fixture-not-ext4")
            stream.flush()
            os.fsync(stream.fileno())
        self.filesystems[str(image)] = {
            "TYPE": "ext4",
            "UUID": fs_uuid,
            "bytes": image.stat().st_size,
        }
        self.done("format")

    def filesystem(self, image):
        return self.filesystems.get(str(image), {})

    def filesystem_bytes(self, image):
        return self.filesystems[str(image)]["bytes"]

    def loops(self):
        return [dict(row) for row in self.loop_rows]

    def mounts(self):
        return [dict(row) for row in self.mount_rows]

    def attach(self, image):
        self.next_device += 1
        device = f"/dev/loop{self.next_device}"
        self.loop_rows.append(
            {
                "name": device,
                "back-file": str(image),
                "back-ino": image.stat().st_ino,
                "back-maj:min": f"{os.major(image.stat().st_dev)}:{os.minor(image.stat().st_dev)}",
                "offset": 0,
                "sizelimit": 0,
                "ro": False,
            }
        )
        self.done("attach")
        return device

    def detach(self, device):
        self.loop_rows = [row for row in self.loop_rows if row["name"] != device]
        self.done("detach")

    def mount(self, device, target):
        self.mount_rows.append(
            {
                "target": str(target),
                "source": device,
                "fstype": "ext4",
                "fsroot": "/",
                "options": "rw,nosuid,nodev",
                "maj:min": self.device_number(device),
            }
        )
        self.done("mount")

    def unmount(self, target):
        self.mount_rows = [
            row for row in self.mount_rows if row["target"] != str(target)
        ]
        self.done("unmount")

    def device_number(self, device):
        return "7:" + device.removeprefix("/dev/loop")

    def in_use(self, target):
        return self.busy

    def initialize_owner(self, target):
        self.done("initialize")

    def refresh_capacity(self, device):
        self.done("refresh_capacity")

    def resize(self, image_or_device, quota):
        key = str(image_or_device)
        if key.startswith("/dev/"):
            key = next(row["back-file"] for row in self.loop_rows if row["name"] == key)
        self.filesystems[key]["bytes"] = quota
        self.done("resize")

    def fsck(self, image):
        self.done("fsck")

    def trim(self, target):
        self.done("trim")


class ProjectVolumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-volumes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "volumes"
        self.host = HostFixture()
        self.admitted = []
        self.store = self.reopen()

    def reopen(self):
        return ProjectVolumes(
            self.root, INSTANCE, host=self.host, capacity_check=self.admitted.append
        )

    def create(self, project_id=1, **options):
        return self.store.create(
            "scope", project_id, quota_bytes=64 * MIB, fence=1, **options
        )

    def record(self):
        return read_json(self.root / "p1/volume.json")

    def image(self):
        return next((self.root / "p1").glob("*.ext4"))

    def test_create_is_sparse_fenced_and_idempotent_across_reopen(self):
        first = self.create()
        again = self.reopen().create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.assertEqual(first, again)
        self.assertEqual(self.host.calls.count("format"), 1)
        self.assertLess(first["allocated_bytes"], MIB)
        self.assertEqual(first["quota_bytes"], 64 * MIB)
        self.assertEqual(self.admitted, [64 * MIB])
        self.assertNotIn(str(self.root), json.dumps(first))

    def test_owner_instance_and_stale_fence_cannot_take_over(self):
        self.create()
        self.store.mutate("scope", 1, "mount", fence=2)
        for call in (
            lambda: self.store.status("different", 1),
            lambda: ProjectVolumes(self.root, "2" * 32, host=self.host).status(
                "scope", 1
            ),
            lambda: self.store.mutate("scope", 1, "unmount", fence=1),
            lambda: self.store.mutate(
                "scope", 1, "resize", fence=2, quota_bytes=96 * MIB
            ),
        ):
            with self.assertRaises(ProjectVolumeError):
                call()
        self.assertNotIn("unmount", self.host.calls)
        self.assertNotIn("resize", self.host.calls)

    def test_concurrent_creation_has_one_format(self):
        stores = [self.reopen() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            result = list(
                workers.map(
                    lambda value: value.create(
                        "scope", 1, quota_bytes=64 * MIB, fence=1
                    ),
                    stores,
                )
            )
        self.assertEqual(len({row["fence"] for row in result}), 1)
        self.assertEqual(self.host.calls.count("format"), 1)

    def test_recovery_after_each_external_mount_step_never_reattaches_twice(self):
        self.create()
        for operation in ("attach", "mount", "initialize"):
            self.host.after[operation] = RuntimeError("simulated crash")
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.reopen().mutate("scope", 1, "mount", fence=2)
            self.assertTrue(self.record()["pending"])
            with self.assertRaises(ProjectVolumeError):
                self.store.mutate("scope", 1, "unmount", fence=3)
        result = self.reopen().mutate("scope", 1, "mount", fence=2)
        self.assertTrue(result["mounted"])
        self.assertFalse(result["pending"])
        self.assertEqual(self.host.calls.count("attach"), 1)
        self.assertEqual(self.host.calls.count("mount"), 1)

    def test_recovery_after_format_verifies_instead_of_overwriting(self):
        self.host.after["format"] = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.create()
        self.reopen().create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.assertEqual(self.host.calls.count("format"), 1)
        self.assertIn("fsck", self.host.calls)

    def test_missing_image_is_not_replaced_by_empty_project(self):
        self.create()
        self.image().unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.status("scope", 1)
        with self.assertRaises((FileNotFoundError, ProjectVolumeError)):
            self.reopen().create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.assertEqual(self.host.calls.count("format"), 1)

    def test_invalid_create_retry_cannot_leave_a_pending_operation(self):
        self.create()
        before = self.record()
        with self.assertRaises(ProjectVolumeError):
            self.store.create("scope", 1, quota_bytes=96 * MIB, fence=2)
        self.assertEqual(self.record(), before)
        self.assertTrue(self.store.mutate("scope", 1, "mount", fence=2)["mounted"])

    def test_capacity_rejection_and_uncertain_format_never_overwrite_data(self):
        with patch.object(self.store, "_admit", side_effect=ProjectVolumeError("full")):
            with self.assertRaises(ProjectVolumeError):
                self.create()
        self.assertEqual(self.host.calls, [])
        self.host.after["format"] = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.create()
        image = self.image()
        del self.host.filesystems[str(image.resolve())]
        with self.assertRaisesRegex(ProjectVolumeError, "refusing to overwrite"):
            self.reopen().create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.assertEqual(self.host.calls.count("format"), 1)

    def test_replaced_image_hardlink_and_symlink_are_rejected(self):
        self.create()
        image = self.image()
        renamed = image.with_suffix(".original")
        image.rename(renamed)
        image.write_bytes(b"foreign data")
        with self.assertRaises(ProjectVolumeError):
            self.store.status("scope", 1)
        image.unlink()
        os.link(renamed, image)
        with self.assertRaises(ProjectVolumeError):
            self.store.status("scope", 1)
        image.unlink()
        image.symlink_to(renamed)
        with self.assertRaises(ProjectVolumeError):
            self.store.status("scope", 1)

    def test_remount_discovers_new_device_and_rejects_foreign_mapping(self):
        self.create()
        self.store.mutate("scope", 1, "mount", fence=2)
        old_device = self.host.loop_rows[0]["name"]
        self.store.mutate("scope", 1, "unmount", fence=3)
        self.reopen().mutate("scope", 1, "mount", fence=4)
        self.assertNotEqual(self.host.loop_rows[0]["name"], old_device)
        self.host.loop_rows[0]["back-ino"] += 1
        with self.assertRaises(ProjectVolumeError):
            self.store.mutate("scope", 1, "unmount", fence=5)
        self.assertEqual(self.host.calls.count("unmount"), 1)

    def test_foreign_and_unsafe_mounts_never_get_unmounted(self):
        self.create()
        self.store.mutate("scope", 1, "mount", fence=2)
        original = dict(self.host.mount_rows[0])
        for field, value in (
            ("maj:min", "8:0"),
            ("options", "rw"),
            ("fsroot", "/subdirectory"),
            ("fstype", "xfs"),
            ("target", "/unrelated"),
        ):
            self.host.mount_rows[0] = {**original, field: value}
            with self.assertRaises(ProjectVolumeError):
                self.store.mutate("scope", 1, "unmount", fence=3)
        self.assertNotIn("unmount", self.host.calls)

    def test_busy_filesystem_rejects_maintenance_but_allows_online_expansion(self):
        self.create()
        self.store.mutate("scope", 1, "mount", fence=2)
        self.host.busy = True
        for operation in ("unmount", "fsck", "trim", "delete"):
            with self.assertRaises(ProjectVolumeError):
                self.store.mutate("scope", 1, operation, fence=3)
        result = self.store.mutate("scope", 1, "resize", fence=3, quota_bytes=96 * MIB)
        self.assertEqual(result["quota_bytes"], 96 * MIB)
        self.assertEqual(self.host.calls.count("refresh_capacity"), 1)
        with self.assertRaises(ProjectVolumeError):
            self.store.mutate("scope", 1, "resize", fence=4, quota_bytes=64 * MIB)

    def test_shrink_is_offline_and_verified_before_file_truncation(self):
        self.store.create("scope", 1, quota_bytes=96 * MIB, fence=1)
        self.store.mutate("scope", 1, "mount", fence=2)
        with self.assertRaises(ProjectVolumeError):
            self.store.mutate("scope", 1, "resize", fence=3, quota_bytes=64 * MIB)
        self.store.mutate("scope", 1, "unmount", fence=3)
        with (
            patch.object(self.host, "resize", return_value=None),
            self.assertRaises(ProjectVolumeError),
        ):
            self.store.mutate("scope", 1, "resize", fence=4, quota_bytes=64 * MIB)
        self.assertEqual(self.image().stat().st_size, 96 * MIB)
        result = self.reopen().mutate(
            "scope", 1, "resize", fence=4, quota_bytes=64 * MIB
        )
        self.assertEqual(result["quota_bytes"], 64 * MIB)
        self.assertEqual(self.image().stat().st_size, 64 * MIB)

    def test_growth_can_resume_after_resize_finishes_before_journal_commit(self):
        self.create()
        self.host.after["resize"] = RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.store.mutate("scope", 1, "resize", fence=2, quota_bytes=96 * MIB)
        self.assertTrue(self.store.status("scope", 1)["pending"])
        result = self.reopen().mutate(
            "scope", 1, "resize", fence=2, quota_bytes=96 * MIB
        )
        self.assertEqual(result["quota_bytes"], 96 * MIB)
        self.assertFalse(result["pending"])

    def test_delete_resumes_after_unlink_and_never_recreates_tombstone(self):
        self.create()
        original = self.store._save

        def crash(directory, record):
            if record.get("deleted"):
                raise RuntimeError("crash after unlink")
            return original(directory, record)

        with (
            patch.object(self.store, "_save", side_effect=crash),
            self.assertRaises(RuntimeError),
        ):
            self.store.mutate("scope", 1, "delete", fence=2)
        result = self.reopen().mutate("scope", 1, "delete", fence=2)
        self.assertTrue(result["deleted"])
        self.assertEqual(result, self.store.mutate("scope", 1, "delete", fence=2))
        with self.assertRaises(ProjectVolumeError):
            self.store.create("scope", 1, quota_bytes=64 * MIB, fence=3)

    def test_fsck_trim_and_binding_require_expected_state(self):
        self.create()
        with self.assertRaises(ProjectVolumeError):
            self.store.binding("scope", 1)
        self.store.mutate("scope", 1, "fsck", fence=2)
        self.store.mutate("scope", 1, "mount", fence=3)
        self.assertEqual(
            self.store.binding("scope", 1), (self.root / "p1/mount").resolve()
        )
        self.store.mutate("scope", 1, "trim", fence=4)
        self.assertEqual(self.host.calls.count("trim"), 1)
        with self.assertRaises(ProjectVolumeError):
            self.store.mutate("scope", 1, "fsck", fence=5)

    def test_rpc_rejects_paths_devices_unknown_operations_and_unbounded_numbers(self):
        args = {"owner": "scope", "project_id": 1, "quota_bytes": 64 * MIB, "fence": 1}
        validate_request(request("project_volume_create", args), file_limit=MIB)
        for changes in (
            {"image": "/etc/passwd"},
            {"device": "/dev/sda"},
            {"quota_bytes": 0},
            {"quota_bytes": 64 * MIB + 1},
            {"fence": True},
            {"project_id": "../1"},
        ):
            with self.assertRaises(SandboxError):
                validate_request(
                    request("project_volume_create", {**args, **changes}),
                    file_limit=MIB,
                )
        with self.assertRaises(SandboxError):
            validate_request(request("project_volume_exec", args), file_limit=MIB)

    def test_backing_device_alias_and_deleted_loop_are_rejected(self):
        self.create()
        self.store.mutate("scope", 1, "mount", fence=2)
        original = dict(self.host.loop_rows[0])
        for change in (
            {"back-maj:min": "999:999"},
            {"back-file": str(self.image()) + " (deleted)"},
            {"back-file": "/alias/owned-image"},
        ):
            self.host.loop_rows[0] = {**original, **change}
            with self.subTest(change=change), self.assertRaises(ProjectVolumeError):
                self.store.mutate("scope", 1, "unmount", fence=3)
        self.assertNotIn("unmount", self.host.calls)

    def test_directory_and_uuid_corruption_do_not_reach_host_mutations(self):
        self.create()
        previous = list(self.host.calls)
        for value in ("../other", None, 3):
            record = {**self.record(), "fs_uuid": value}
            with self.assertRaises(ProjectVolumeError):
                self.store._observed(self.root / "p1", record)
        self.assertEqual(self.host.calls, previous)


class LinuxVolumeHostTests(unittest.TestCase):
    def setUp(self):
        # Test the fixed command contract without root or a real Docker socket.
        with (
            patch("src.sandbox_runtime.project_volumes.sys.platform", "linux"),
            patch("src.sandbox_runtime.project_volumes.os.geteuid", return_value=0),
        ):
            self.host = LinuxVolumeHost()
        self.host.run = Mock()

    def test_stopped_container_references_and_ancestor_mounts_protect_volume(self):
        identifier = "a" * 64
        for source in ("/state/p1/mount", "/state/p1/mount/subdir", "/state"):
            self.host.run.side_effect = [
                identifier + "\n",
                json.dumps([{"Source": source}]),
            ]
            self.assertTrue(self.host.in_use(Path("/state/p1/mount")))
            self.assertIn("-a", self.host.run.call_args_list[-2].args)
        self.host.run.side_effect = [
            identifier + "\n",
            json.dumps([{"Source": "/state/p10/mount"}]),
        ]
        self.assertFalse(self.host.in_use(Path("/state/p1/mount")))

    def test_incomplete_container_inventory_is_not_treated_as_idle(self):
        self.host.run.side_effect = ["a" * 64 + "\n" + "b" * 64, "[]\n"]
        with self.assertRaises(ProjectVolumeError):
            self.host.in_use(Path("/state/p1/mount"))

    def test_parses_fixed_filesystem_capacity_and_loop_columns(self):
        self.host.run.return_value = "Block count:  16384\nBlock size: 4096\n"
        self.assertEqual(
            self.host.filesystem_bytes(Path("/private/project.ext4")), 64 * MIB
        )
        self.host.run.return_value = "Block count: unknown\n"
        with self.assertRaises(ProjectVolumeError):
            self.host.filesystem_bytes(Path("/private/project.ext4"))
        self.host.run.return_value = '{"loopdevices": []}'
        self.assertEqual(self.host.loops(), [])
        self.assertIn("BACK-MAJ:MIN", self.host.run.call_args.args[-1])


class ProjectVolumeRpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gj-pv-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "state").mkdir(mode=0o700)
        self.host = HostFixture()
        self.volumes = ProjectVolumes(
            self.root / "state/project-volumes", INSTANCE, host=self.host
        )
        self.driver = DockerSandboxManager(
            namespace=INSTANCE,
            image="gaoji-app-sandbox:" + "b" * 64,
            max_file_bytes=MIB,
        )
        self.driver._run = AsyncMock(
            side_effect=AssertionError("No real Docker in unit tests")
        )
        await self.start()
        self.addAsyncCleanup(self.stop)
        self.client = SandboxClient(
            str(self.root / "socket/runner.sock"), TOKEN, max_file_bytes=MIB
        )

    async def start(self):
        self.runner = SandboxRunner(
            self.driver,
            socket_path=self.root / "socket/runner.sock",
            state_directory=self.root / "state",
            token=TOKEN,
            project_volumes=self.volumes,
        )
        await self.runner.start()

    async def stop(self):
        await self.runner.close()

    async def test_disabled_by_default_and_namespace_bound(self):
        self.assertTrue((await self.client.health())["project_volumes_enabled"])
        self.runner.project_volumes = None
        self.assertFalse((await self.client.health())["project_volumes_enabled"])
        with self.assertRaisesRegex(SandboxError, "尚未配置"):
            await self.client.project_volume(
                "create", "scope", 1, fence=1, quota_bytes=64 * MIB
            )
        other = ProjectVolumes(self.root / "foreign", "2" * 32, host=self.host)
        with self.assertRaises(ValueError):
            SandboxRunner(
                self.driver,
                socket_path=self.root / "other.sock",
                state_directory=self.root / "state",
                token=TOKEN,
                project_volumes=other,
            )

    async def test_create_replays_same_request_after_runner_restart(self):
        first = await self.client.project_volume(
            "create", "scope", 1, fence=1, quota_bytes=64 * MIB, request_id="a" * 32
        )
        await self.stop()
        await self.start()
        again = await self.client.project_volume(
            "create", "scope", 1, fence=1, quota_bytes=64 * MIB, request_id="a" * 32
        )
        self.assertEqual(first, again)
        self.assertEqual(self.host.calls.count("format"), 1)
        await self.client.project_volume("mount", "scope", 1, fence=2)
        status = await self.client.project_volume("status", "scope", 1)
        self.assertTrue(status["mounted"])
        self.assertNotIn(str(self.root), json.dumps(status))

    async def test_cancel_waits_for_filesystem_operation_and_does_not_block_event_loop(
        self,
    ):
        began, release = threading.Event(), threading.Event()
        original = self.host.format

        def blocked(image, fs_uuid):
            began.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Fixture timed out")
            original(image, fs_uuid)

        self.host.format = blocked
        creating = asyncio.create_task(
            self.client.project_volume(
                "create", "scope", 1, fence=1, quota_bytes=64 * MIB
            )
        )
        try:
            self.assertTrue(await asyncio.to_thread(began.wait, 2))
            self.assertTrue(
                (await asyncio.wait_for(self.client.health(), 1))["channel_ready"]
            )
            operation = next(
                task
                for _, task in self.runner.inflight.values()
                if task.get_name() == "sandbox:project_volume_create"
            )
            operation.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(operation.done())
        finally:
            release.set()
        with self.assertRaises(SandboxError):
            await creating
        self.assertFalse(self.volumes.status("scope", 1)["pending"])
        self.assertEqual(self.host.calls.count("format"), 1)


if __name__ == "__main__":
    unittest.main()
