"""Guest identity and mount protocol; OS observations here are explicit fixtures."""

import copy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.sandbox_runtime import project_vm_guest as guest
from src.sandbox_runtime.project_vm_disks import VmProjectDiskBinding

MIB = 1024**2
FS_UUID = "11111111-1111-4111-8111-111111111111"


def binding():
    return VmProjectDiskBinding(
        9, 2, "s123456", Path("/private/projects/data.qcow2"), FS_UUID, 64 * MIB
    )


class GuestFixture:
    def __init__(self, config):
        self.config = config
        self.disk = {
            "serial": config["serial"],
            "bytes": config["quota_bytes"],
            "number": "253:16",
        }
        self.rows = []
        self.fs = {}
        self.geometry = config["quota_bytes"]
        self.available = config["quota_bytes"] - MIB
        self.workspace_device = "253:16"
        self.blank = True
        self.owner_ready = False
        self.calls = []
        self.after = {}

    def done(self, operation):
        self.calls.append(operation)
        if operation in self.after:
            raise self.after.pop(operation)

    def device(self):
        return copy.deepcopy(self.disk)

    def mounts(self):
        return copy.deepcopy(self.rows)

    def filesystem(self):
        return self.fs.copy()

    def filesystem_bytes(self):
        return self.geometry

    def prepare_mountpoint(self):
        self.done("secure-empty-mountpoint")

    def require_blank(self, quota):
        self.done("blank-check")
        if not self.blank:
            raise guest.GuestDiskError("not blank")

    def format(self, filesystem_uuid):
        self.fs = {"TYPE": "ext4", "UUID": filesystem_uuid}
        self.blank = False
        self.done("format")

    def mount(self):
        self.rows = [
            {
                "source": "/dev/vdb",
                "target": "/workspace",
                "fstype": "ext4",
                "fsroot": "/",
                "maj:min": "253:16",
                "options": "rw,nosuid,nodev,relatime",
            }
        ]
        self.done("mount")

    def initialize_owner(self):
        self.owner_ready = True
        self.done("owner")

    def usage(self):
        if not self.owner_ready:
            raise guest.GuestDiskError("owner not initialized")
        return {"number": self.workspace_device, "available_bytes": self.available}

    def trim(self):
        self.done("trim")

    def unmount(self):
        self.done("unmount")
        self.rows = []


class GuestWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.config = binding().guest_config(initialize=True)
        self.host = GuestFixture(self.config)
        self.workspace = guest.ProjectGuestWorkspace(self.config, host=self.host)

    def test_first_boot_formats_only_verified_blank_disk_then_reuses_it(self):
        observed = self.workspace.mount()
        self.assertEqual(
            self.host.calls,
            ["secure-empty-mountpoint", "blank-check", "format", "mount", "owner"],
        )
        self.assertTrue(observed["verified"])
        self.assertEqual(observed["used_bytes"], MIB)
        self.assertEqual(observed["filesystem_uuid"], FS_UUID)
        self.assertEqual(self.workspace.mount(), observed)
        self.assertEqual(self.host.calls.count("format"), 1)

    def test_normal_boot_does_not_have_permission_to_create_a_filesystem(self):
        workspace = guest.ProjectGuestWorkspace(
            binding().guest_config(), host=self.host
        )
        with self.assertRaisesRegex(guest.GuestDiskError, "no initialization"):
            workspace.mount()
        self.assertNotIn("blank-check", self.host.calls)
        self.assertNotIn("format", self.host.calls)

    def test_foreign_filesystem_or_partition_table_is_not_reformatted(self):
        for fs in (
            {"TYPE": "ext4", "UUID": "other"},
            {"PTTYPE": "gpt"},
            {"TYPE": "xfs", "UUID": FS_UUID},
        ):
            self.host.fs = fs
            with self.subTest(fs=fs), self.assertRaises(guest.GuestDiskError):
                self.workspace.mount()
        self.assertNotIn("format", self.host.calls)

    def test_partial_unrecognized_disk_is_not_formatted(self):
        self.host.blank = False
        with self.assertRaisesRegex(guest.GuestDiskError, "not blank"):
            self.workspace.mount()
        self.assertNotIn("format", self.host.calls)

    def test_wrong_device_serial_or_quota_blocks_before_any_write(self):
        original = self.host.disk.copy()
        for change in ({"serial": "system-disk"}, {"bytes": 96 * MIB}, {"bytes": True}):
            self.host.disk = {**original, **change}
            with self.subTest(change=change), self.assertRaises(guest.GuestDiskError):
                self.workspace.mount()
        self.assertEqual(self.host.calls, [])

    def test_lost_format_and_mount_ack_can_resume_without_reformatting(self):
        for operation in ("format", "mount"):
            self.host.after[operation] = RuntimeError("lost ack")
            with self.assertRaisesRegex(RuntimeError, "lost ack"):
                self.workspace.mount()
        self.assertTrue(self.workspace.mount()["mounted"])
        self.assertTrue(self.host.owner_ready)
        self.assertEqual(self.host.calls.count("format"), 1)
        self.assertEqual(self.host.calls.count("mount"), 1)

    def test_regular_system_directory_is_not_a_mounted_project(self):
        with self.assertRaisesRegex(guest.GuestDiskError, "system-disk workspace"):
            self.workspace.verify()
        self.assertEqual(self.host.calls, [])

    def test_other_mounts_bind_subdirectory_and_unsafe_options_are_rejected(self):
        self.workspace.mount()
        original = self.host.rows[0].copy()
        for change in (
            {"target": "/elsewhere"},
            {"maj:min": "253:0"},
            {"fsroot": "/subdir"},
            {"options": "rw,nodev"},
            {"options": "ro,nosuid,nodev"},
            {"fstype": "xfs"},
        ):
            self.host.rows = [{**original, **change}]
            with self.subTest(change=change), self.assertRaises(guest.GuestDiskError):
                self.workspace.verify()
        self.host.rows = [original, {**original, "target": "/also-mounted"}]
        with self.assertRaisesRegex(guest.GuestDiskError, "multiple"):
            self.workspace.verify()

    def test_readonly_probe_does_not_repair_missing_filesystem_or_geometry(self):
        self.workspace.mount()
        before = self.host.calls.copy()
        self.host.geometry = 63 * MIB
        with self.assertRaises(guest.GuestDiskError):
            self.workspace.verify()
        self.host.geometry = 64 * MIB
        self.host.fs = {}
        with self.assertRaises(guest.GuestDiskError):
            self.workspace.verify()
        self.assertEqual(before, self.host.calls)

    def test_filesystem_accounting_must_match_mount_and_capacity(self):
        self.workspace.mount()
        for value in (-1, 65 * MIB, True, None):
            self.host.available = value
            with self.subTest(value=value), self.assertRaises(guest.GuestDiskError):
                self.workspace.verify()
        self.host.available = 32 * MIB
        self.host.workspace_device = "253:0"
        with self.assertRaises(guest.GuestDiskError):
            self.workspace.verify()

    def test_trim_verifies_before_and_after_and_does_not_claim_host_reclamation(self):
        with self.assertRaises(guest.GuestDiskError):
            self.workspace.trim()
        self.assertNotIn("trim", self.host.calls)
        self.workspace.mount()
        result = self.workspace.trim()
        self.assertTrue(result["discard_completed"])
        self.assertNotIn("reclaimed_bytes", result)
        with patch.object(
            self.host, "trim", side_effect=lambda: self.host.rows.clear()
        ):
            with self.assertRaises(guest.GuestDiskError):
                self.workspace.trim()

    def test_unmount_relocks_fallback_directory_and_keeps_filesystem(self):
        self.workspace.mount()
        result = self.workspace.unmount()
        self.assertFalse(result["mounted"])
        self.assertEqual(self.host.calls[-2:], ["unmount", "secure-empty-mountpoint"])
        self.assertEqual(self.host.fs["UUID"], FS_UUID)
        self.assertEqual(result, self.workspace.unmount())
        self.assertTrue(self.workspace.mount()["mounted"])
        self.assertEqual(self.host.calls.count("format"), 1)

    def test_busy_unmount_is_not_reported_as_success(self):
        self.workspace.mount()
        self.host.after["unmount"] = guest.GuestDiskError("busy")
        with self.assertRaisesRegex(guest.GuestDiskError, "busy"):
            self.workspace.unmount()
        self.assertTrue(self.workspace.verify()["mounted"])

    def test_config_is_scoped_and_has_no_arbitrary_device_or_path(self):
        for change in (
            {"device": "/dev/vda"},
            {"initialize": "true"},
            {"quota_bytes": True},
            {"project_id": True},
            {"generation": 0},
            {"filesystem_uuid": "../other"},
            {"serial": "x"},
        ):
            with self.subTest(change=change), self.assertRaises(guest.GuestDiskError):
                guest.validate_config({**self.config, **change})
        self.assertEqual(len(self.config["serial"]), 20)
        self.assertNotEqual(self.config["serial"], guest.disk_serial(9, 3, FS_UUID))


class GuestLinuxAdapterTests(unittest.TestCase):
    def test_blank_check_reads_entire_device_not_just_superblock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test-only-disk"
            with path.open("wb") as stream:
                stream.truncate(2 * MIB)
            with patch.object(guest, "DEVICE", path):
                guest.LinuxGuestDisk().require_blank(2 * MIB)
                with path.open("r+b") as stream:
                    stream.seek(2 * MIB - 1)
                    stream.write(b"x")
                with self.assertRaisesRegex(guest.GuestDiskError, "not entirely blank"):
                    guest.LinuxGuestDisk().require_blank(2 * MIB)
                with patch.object(guest.time, "monotonic", side_effect=[0, 61]):
                    with self.assertRaisesRegex(guest.GuestDiskError, "timed out"):
                        guest.LinuxGuestDisk().require_blank(2 * MIB)

    def test_mountpoint_refuses_symlinks_and_retained_system_disk_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "workspace"
            target.mkdir()
            retained = target / "do-not-hide"
            retained.write_text("data")
            with (
                patch.object(guest, "WORKSPACE", target),
                patch.object(guest.os, "chown") as chown,
            ):
                with self.assertRaises(guest.GuestDiskError):
                    guest.LinuxGuestDisk().prepare_mountpoint()
                chown.assert_not_called()
                self.assertEqual(retained.read_text(), "data")
            link = Path(temporary) / "alias"
            link.symlink_to(target)
            with patch.object(guest, "WORKSPACE", link):
                with self.assertRaises(guest.GuestDiskError):
                    guest.LinuxGuestDisk().prepare_mountpoint()

    def test_mount_and_trim_commands_are_fixed_and_never_force_unmount(self):
        host = guest.LinuxGuestDisk()
        host.run = Mock(return_value="")
        host.mount()
        host.trim()
        host.unmount()
        commands = [call.args for call in host.run.call_args_list]
        self.assertEqual(
            commands,
            [
                ("e2fsck", "-p", "/dev/vdb"),
                ("mount", "-t", "ext4", "-o", "nosuid,nodev", "/dev/vdb", "/workspace"),
                ("sync", "-f", "/workspace"),
                ("fstrim", "/workspace"),
                ("sync", "-f", "/workspace"),
                ("umount", "/workspace"),
            ],
        )

    def test_first_boot_permission_is_durably_consumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gaoji-project.json"
            config = binding().guest_config(initialize=True)
            path.write_text(json.dumps(config))
            with patch.object(guest, "CONFIG_PATH", path):
                guest.seal_initialization(config)
            self.assertEqual(
                json.loads(path.read_text()), {**config, "initialize": False}
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_failed_config_replace_keeps_original_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gaoji-project.json"
            config = binding().guest_config(initialize=True)
            path.write_text(json.dumps(config))
            with (
                patch.object(guest, "CONFIG_PATH", path),
                patch.object(guest.os, "replace", side_effect=OSError("failed")),
            ):
                with self.assertRaises(OSError):
                    guest.seal_initialization(config)
            self.assertEqual(json.loads(path.read_text()), config)
            self.assertEqual(list(Path(temporary).iterdir()), [path])
