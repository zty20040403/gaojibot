"""Real private records and gate locks; device effects are explicit fixtures."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.sandbox_runtime import project_vm_guest as disk
from src.sandbox_runtime.project_vm_gate import ProjectGuestGate, LinuxGuestCommands, _read_private
from src.sandbox_runtime.project_vm_resize_guest import ProjectGuestResize, JOURNAL_NAME
from tests.test_project_vm_guest import GuestFixture, binding, MIB


class ResizeDisk(GuestFixture):
    def filesystem_used_bytes(self):
        return self.geometry - self.available

    def check(self):
        self.done("check")

    def resize(self, quota):
        self.available += quota - self.geometry
        self.geometry = quota
        self.done("resize")

    def unmount(self):
        self.rows = []
        self.done("unmount")


class GuestResizeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-resize-guest-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = binding().guest_config()
        self.host = ResizeDisk(self.config)
        self.host.fs = {"TYPE": "ext4", "UUID": self.config["filesystem_uuid"]}
        disk.ProjectGuestWorkspace(self.config, host=self.host).mount()
        self.config_path = self.root / "config.json"
        disk.write_private_json(self.config_path, self.config)
        self.enterContext(patch.object(disk, "CONFIG_PATH", self.config_path))
        self.enterContext(patch.object(disk, "load_config", side_effect=lambda: disk.validate_config(_read_private(self.config_path))))
        self.commands = Mock(spec=LinuxGuestCommands)
        self.commands.empty.return_value = True
        self.commands.sandbox_processes.return_value = False
        self.gate = self.reopen()
        self.gate.quiesce(2)
        self.host.calls.clear()

    def reopen(self):
        config = disk.load_config()
        return ProjectGuestGate(config, state_path=self.root / "gate.json", lock_root=self.root,
            commands=self.commands, workspace=disk.ProjectGuestWorkspace(config, host=self.host))

    def resize(self, phase, *, old=64, target=96, epoch=3):
        self.gate = self.reopen()
        return ProjectGuestResize(self.gate).operate(phase, epoch, old * MIB, target * MIB)

    def state(self):
        return _read_private(self.root / JOURNAL_NAME)

    def grow(self):
        self.resize("prepare")
        self.host.disk["bytes"] = 96 * MIB
        self.resize("apply")
        return self.resize("commit")

    def test_online_growth_changes_filesystem_and_commits_verified_config(self):
        prepared = self.resize("prepare")
        self.assertEqual(prepared["phase"], "prepared")
        self.assertEqual(self.host.calls, [])
        self.host.disk["bytes"] = 96 * MIB
        applied = self.resize("apply")
        self.assertEqual(applied["filesystem_bytes"], 96 * MIB)
        self.assertEqual(disk.load_config()["quota_bytes"], 64 * MIB)
        result = self.resize("commit")
        self.assertEqual((result["phase"], result["device_bytes"], result["quota_bytes"]), ("completed", 96 * MIB, 96 * MIB))
        self.assertTrue(result["mounted"])
        self.assertEqual(self.host.calls.count("resize"), 1)
        self.assertNotIn("check", self.host.calls)
        self.assertNotIn("format", self.host.calls)
        self.assertEqual(self.gate._state()["mode"], "quiesced")
        self.assertEqual(self.reopen().activate(4)["mode"], "active")

    def test_shrink_unmounts_then_resizes_before_host_truncation(self):
        self.grow()
        self.host.calls.clear()
        self.assertFalse(self.resize("prepare", old=96, target=64, epoch=5)["mounted"])
        applied = self.resize("apply", old=96, target=64, epoch=5)
        self.assertEqual((applied["device_bytes"], applied["filesystem_bytes"]), (96 * MIB, 64 * MIB))
        self.assertEqual(self.host.calls[:4], ["unmount", "secure-empty-mountpoint", "check", "resize"])
        with self.assertRaises(disk.GuestDiskError):
            self.resize("commit", old=96, target=64, epoch=5)
        self.assertEqual(disk.load_config()["quota_bytes"], 96 * MIB)
        self.host.disk["bytes"] = 64 * MIB
        result = self.resize("commit", old=96, target=64, epoch=5)
        self.assertTrue(result["mounted"])
        self.assertEqual(disk.load_config()["quota_bytes"], 64 * MIB)

    def test_unmounted_growth_stays_unmounted(self):
        self.host.rows = []
        result = self.grow()
        self.assertFalse(result["mounted"])
        self.assertIn("check", self.host.calls)
        self.assertNotIn("mount", self.host.calls)

    def test_lost_filesystem_ack_resumes_without_repeating_resize(self):
        self.resize("prepare")
        self.host.disk["bytes"] = 96 * MIB
        self.host.after["resize"] = OSError("ack lost")
        with self.assertRaises(OSError):
            self.resize("apply")
        self.assertEqual(self.state()["phase"], "prepared")
        self.resize("apply")
        self.resize("commit")
        self.assertEqual(self.host.calls.count("resize"), 1)

    def test_lost_unmount_ack_keeps_original_mount_intent(self):
        self.grow()
        self.host.after["unmount"] = OSError("ack lost")
        with self.assertRaises(OSError):
            self.resize("prepare", old=96, target=64, epoch=5)
        self.assertTrue(self.state()["mounted"])
        self.resize("prepare", old=96, target=64, epoch=5)
        self.resize("apply", old=96, target=64, epoch=5)
        self.host.disk["bytes"] = 64 * MIB
        self.assertTrue(self.resize("commit", old=96, target=64, epoch=5)["mounted"])

    def test_config_write_then_crash_is_reconciled_before_activation(self):
        self.resize("prepare")
        self.host.disk["bytes"] = 96 * MIB
        self.resize("apply")
        original = disk.write_private_json
        def lose_ack(path, value):
            original(path, value)
            if path == self.config_path:
                raise OSError("config ack lost")
        with patch.object(disk, "write_private_json", side_effect=lose_ack), self.assertRaises(OSError):
            self.resize("commit")
        self.assertEqual(disk.load_config()["quota_bytes"], 96 * MIB)
        with self.assertRaises(disk.GuestDiskError):
            self.reopen().activate(4)
        self.assertEqual(self.resize("commit")["phase"], "completed")

    def test_pending_resize_blocks_execution_file_maintenance_and_other_disk_actions(self):
        self.resize("prepare")
        for call in (lambda: self.reopen().activate(4), lambda: self.reopen().disk_operation("trim", 4),
                     lambda: self.reopen().run(4, ["/bin/true"], 10, 32), lambda: self.reopen().inspect(4)):
            with self.assertRaises(disk.GuestDiskError):
                call()
        with self.assertRaises(disk.GuestDiskError), self.reopen().maintenance(4):
            self.fail("maintenance entered during resize")

    def test_conflicting_or_stale_requests_cannot_replace_intent(self):
        self.resize("prepare")
        initial = self.state()
        for args in ({"target": 80}, {"epoch": 4}, {"epoch": 1}, {"old": 96, "target": 128}):
            with self.subTest(args=args), self.assertRaises(disk.GuestDiskError):
                self.resize("prepare", **args)
        self.assertEqual(self.state(), initial)

    def test_missing_host_growth_and_skipped_phase_cannot_commit(self):
        for phase in ("apply", "commit"):
            with self.assertRaises(disk.GuestDiskError):
                self.resize(phase)
        self.resize("prepare")
        with self.assertRaises(disk.GuestDiskError):
            self.resize("apply")
        with self.assertRaises(disk.GuestDiskError):
            self.resize("commit")
        self.assertNotIn("resize", self.host.calls)

    def test_foreign_device_filesystem_and_writers_fail_before_mutation(self):
        for key, value in (("serial", "foreign"), ("bytes", 80 * MIB), ("number", "253:0")):
            original = self.host.disk.copy()
            self.host.disk[key] = value
            with self.subTest(key=key), self.assertRaises(disk.GuestDiskError):
                self.resize("prepare")
            self.host.disk = original
        self.commands.sandbox_processes.return_value = True
        with self.assertRaises(disk.GuestDiskError):
            self.resize("prepare")
        self.assertFalse((self.root / JOURNAL_NAME).exists())
        self.assertEqual(self.host.calls, [])

    def test_usage_prevents_shrink_even_before_command_dispatch(self):
        self.grow()
        self.host.available = 16 * MIB
        self.host.calls.clear()
        with self.assertRaisesRegex(disk.GuestDiskError, "usage"):
            self.resize("prepare", old=96, target=64, epoch=5)
        self.assertEqual(self.state()["phase"], "completed")
        self.assertEqual(self.host.calls, [])

    def test_completed_receipt_checks_actual_geometry_and_mount(self):
        result = self.grow()
        self.assertEqual(self.resize("commit"), result)
        self.host.geometry = 64 * MIB
        with self.assertRaises(disk.GuestDiskError):
            self.resize("commit")
        self.host.geometry = 96 * MIB
        self.host.rows = []
        with self.assertRaises(disk.GuestDiskError):
            self.resize("commit")

    def test_ready_receipt_cannot_authorize_host_shrink_when_filesystem_drifted(self):
        self.resize("prepare")
        self.host.disk["bytes"] = 96 * MIB
        self.resize("apply")
        self.host.geometry = 64 * MIB
        with self.assertRaises(disk.GuestDiskError):
            self.resize("apply")

    def test_ready_shrink_receipt_rejects_remount_before_host_truncation(self):
        self.grow()
        self.resize("prepare", old=96, target=64, epoch=5)
        self.resize("apply", old=96, target=64, epoch=5)
        self.host.mount()
        with self.assertRaisesRegex(disk.GuestDiskError, "unmounted"):
            self.resize("apply", old=96, target=64, epoch=5)

    def test_private_corrupt_or_foreign_journal_is_not_a_missing_intent(self):
        self.resize("prepare")
        record = self.state()
        for change in ({"epoch": True}, {"phase": []}, {"project_id": 10}, {"mounted": 1}, {"quota_bytes": True}):
            disk.write_private_json(self.root / JOURNAL_NAME, {**record, **change})
            with self.subTest(change=change), self.assertRaises(disk.GuestDiskError):
                self.resize("apply")
        disk.write_private_json(self.root / JOURNAL_NAME, record)
        (self.root / JOURNAL_NAME).chmod(0o644)
        with self.assertRaises(disk.GuestDiskError):
            self.resize("apply")

    def test_disk_commands_are_fixed_and_do_not_force_repair(self):
        host = disk.LinuxGuestDisk()
        with patch.object(host, "run") as run:
            host.check()
            host.resize(96 * MIB)
        self.assertEqual(run.call_args_list[0].args, ("e2fsck", "-p", "-f", "/dev/vdb"))
        self.assertEqual(run.call_args_list[1].args, ("resize2fs", "/dev/vdb", "98304K"))

    def test_offline_usage_validates_counters(self):
        host = disk.LinuxGuestDisk()
        with patch.object(host, "run", return_value="Block count: 100\nFree blocks: 70\nReserved block count: 10\nBlock size: 4096"):
            self.assertEqual(host.filesystem_used_bytes(), 40 * 4096)
        with patch.object(host, "run", return_value="Block count: 100\nFree blocks: 101\nReserved block count: 0\nBlock size: 4096"):
            with self.assertRaises(disk.GuestDiskError):
                host.filesystem_used_bytes()
