"""Filesystem accounting validation; real ext4/QEMU utility proof is separate."""

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid

from src.sandbox_runtime.project_vm_usage import (
    crc32c,
    ext4_used_bytes,
    qcow_used_bytes,
)
from src.sandbox_runtime.project_volume_journal import ProjectVolumeError
from src.sandbox_runtime.project_vm_usage_guest import measure
from src.sandbox_runtime.project_vm_guest import GuestDiskError
from tests.test_project_vm_files import GuestFixture

UUID = "4f8758a1-aeb9-41f2-95e8-4a2e629800c0"
QUOTA = 64 * 1024**2


def superblock(*, total=16384, free=10000, reserved=10, **fields):
    data = bytearray(1024)
    for offset, value in {
        0x4: total & 0xFFFFFFFF,
        0x8: reserved & 0xFFFFFFFF,
        0xC: free & 0xFFFFFFFF,
        0x18: 2,
        0x1C: 2,
        0x4C: 1,
        0x60: 0x2C2,
        0x64: 0x400,
        0x150: total >> 32,
        0x154: reserved >> 32,
        0x158: free >> 32,
    }.items():
        struct.pack_into("<I", data, offset, value)
    struct.pack_into("<HH", data, 0x38, 0xEF53, 1)
    data[0x68:0x78] = uuid.UUID(UUID).bytes
    data[0x175] = 1
    for offset, value in fields.items():
        struct.pack_into("<I", data, int(offset, 16), value)
    struct.pack_into("<I", data, 0x3FC, crc32c(data[:0x3FC]))
    return bytes(data)


class OfflineUsageTests(unittest.TestCase):
    def used(self, data, quota=QUOTA):
        return ext4_used_bytes(data, filesystem_uuid=UUID, quota_bytes=quota)

    def test_crc_vector_reserved_blocks_and_64bit_counters(self):
        self.assertEqual(crc32c(b"123456789"), 0x1CF96D7C)
        self.assertEqual(self.used(superblock()), QUOTA - 9990 * 4096)
        self.assertEqual(self.used(superblock(free=0)), QUOTA)
        blocks = 2**32 + 100
        self.assertEqual(
            self.used(
                superblock(total=blocks, free=blocks - 100, reserved=0), blocks * 4096
            ),
            100 * 4096,
        )

    def test_rejects_corruption_identity_dirty_recovery_features_and_geometry(self):
        bad = bytearray(superblock())
        bad[0xC] ^= 1
        cases = [
            bytes(bad),
            b"",
            superblock()[:-1],
            superblock() + b"x",
            superblock(**{"3a": 0}),
            superblock(**{"3a": 3}),
            superblock(**{"60": 0x2C6}),
            superblock(**{"60": 0x800002C2}),
            superblock(**{"64": 0}),
            superblock(**{"64": 0x600}),
            superblock(**{"64": 0x10400}),
            superblock(**{"e8": 1}),
            superblock(**{"e4": 5}),
            superblock(**{"175": 2}),
            superblock(**{"18": 31}),
            superblock(**{"1c": 3}),
            superblock(**{"150": 1, "60": 0x242}),
            superblock(total=16383),
            superblock(free=16385),
            superblock(reserved=16385),
            superblock(**{"68": 1}),
        ]
        for data in cases:
            with self.subTest(data=data[:20]), self.assertRaises(ProjectVolumeError):
                self.used(data)

    def test_fixed_read_command_and_cleanup_in_success_and_failure(self):
        outputs = []

        def run(*args, **kwargs):
            self.assertEqual(
                args[:-2],
                (
                    "qemu-img",
                    "dd",
                    "-f",
                    "qcow2",
                    "-O",
                    "raw",
                    "bs=1024",
                    "count=2",
                ),
            )
            self.assertEqual(args[-2], "if=/private/project.qcow2")
            self.assertEqual(kwargs, {"timeout": 10})
            output = Path(args[-1].removeprefix("of="))
            outputs.append(output)
            output.write_bytes(bytes(1024) + superblock())

        self.assertEqual(
            qcow_used_bytes(
                Path("/private/project.qcow2"),
                filesystem_uuid=UUID,
                quota_bytes=QUOTA,
                run=run,
            ),
            self.used(superblock()),
        )
        self.assertFalse(outputs[0].parent.exists())

        def failed(*args, **kwargs):
            run(*args, **kwargs)
            raise ProjectVolumeError("QEMU read failed")

        with self.assertRaises(ProjectVolumeError):
            qcow_used_bytes(
                Path("/private/project.qcow2"),
                filesystem_uuid=UUID,
                quota_bytes=QUOTA,
                run=failed,
            )
        self.assertFalse(outputs[-1].parent.exists())


class GuestUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-vm-usage-")
        self.addCleanup(temporary.cleanup)
        self.f = GuestFixture(Path(temporary.name))
        self.f.gate.workspace.verify.return_value = {"used_bytes": 1024**2}
        self.f.gate.activate(1)
        self.request = {
            "identity": self.f.gate.identity,
            "epoch": 1,
            "rebuild_command": "npm ci",
        }

    def sample(self):
        return measure(self.f.gate, self.request, workspace=self.f.workspace)

    def test_classifies_only_locked_dependencies_and_does_not_change_gate(self):
        f = self.f
        before = f.gate.state_path.read_bytes()
        (f.workspace / "node_modules").mkdir()
        (f.workspace / "node_modules/pkg").write_bytes(b"x" * 8192)
        (f.workspace / "source.js").write_bytes(b"retained")
        self.assertEqual(self.sample()["rebuildable_bytes"], 0)
        (f.workspace / "package.json").write_text('{"name":"fixture"}')
        (f.workspace / "package-lock.json").write_text('{"lockfileVersion":3}')
        sample = self.sample()
        self.assertGreaterEqual(sample["rebuildable_bytes"], 8192)
        self.assertEqual(sample["used_bytes"], 1024**2)
        self.assertEqual(f.gate.state_path.read_bytes(), before)
        self.assertEqual((f.workspace / "source.js").read_bytes(), b"retained")
        f.commands.run.assert_not_called()

    def test_busy_or_failed_scan_reports_unknown_rebuildable_not_zero(self):
        self.f.commands.empty.return_value = False
        sample = self.sample()
        self.assertTrue(sample["busy"])
        self.assertIsNone(sample["rebuildable_bytes"])
        self.f.commands.empty.return_value = True
        with patch(
            "src.sandbox_runtime.project_vm_usage_guest.rebuildable_bytes",
            side_effect=ProjectVolumeError("budget exceeded"),
        ):
            sample = self.sample()
        self.assertIsNone(sample["rebuildable_bytes"])
        self.assertEqual(sample["used_bytes"], 1024**2)
        self.assertTrue(sample["warning"])

    def test_wrong_scope_epoch_and_maintenance_are_rejected(self):
        for change in (
            {"identity": {**self.f.gate.identity, "project_id": 99}},
            {"epoch": 2},
            {"extra": True},
            {"rebuild_command": []},
        ):
            with (
                self.subTest(change=change),
                self.assertRaises((ProjectVolumeError, GuestDiskError)),
            ):
                measure(
                    self.f.gate, {**self.request, **change}, workspace=self.f.workspace
                )
        self.f.gate.quiesce(2)
        with self.assertRaises(ValueError):
            self.sample()
