"""Project disk journal tests, plus real local qemu-img file operations.

No VM, libvirt daemon, production host or QQ account is used. This does not
replace guest filesystem, trim, ENOSPC or host-reboot acceptance on Linux.
"""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from src.local_runtime.files import read_json, write_json
from src.sandbox_runtime.project_vm_disks import QcowProjectHost, VmProjectDisks
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumeError, ProjectVolumes

INSTANCE = "a" * 32


class DiskFixture:
    def __init__(self):
        self.images = {}
        self.attached = set()
        self.calls = []
        self.after = {}

    def done(self, operation):
        self.calls.append(operation)
        if operation in self.after:
            raise self.after.pop(operation)

    def create(self, path, quota):
        path.write_bytes(b"fixture-not-qcow2")
        self.images[str(path)] = {
            "format": "qcow2",
            "virtual-size": quota,
            "format-specific": {"type": "qcow2", "data": {}},
        }
        self.done("create")

    def info(self, path, *, shared=False):
        return copy.deepcopy(self.images.get(str(path), {}))

    def check(self, path):
        self.done("check")

    def grow(self, path, quota):
        self.images[str(path)]["virtual-size"] = quota
        self.done("grow")

    def consumers(self, path):
        return self.attached.copy()


class VmProjectDiskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-vm-disks-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "projects"
        self.host = DiskFixture()
        self.admitted = []
        self.store = self.reopen()

    def reopen(self):
        return VmProjectDisks(
            self.root, INSTANCE, host=self.host, capacity_check=self.admitted.append
        )

    def create(self, **changes):
        return self.store.create(
            "scope", 1, **{"quota_bytes": 64 * MIB, "fence": 1, **changes}
        )

    def record(self):
        return read_json(self.root / "p1/volume.json")

    def image(self):
        return self.store._path(self.root / "p1", self.record())

    def test_create_exact_capacity_without_preallocating_or_exposing_paths(self):
        result = self.create()
        self.assertEqual(result["quota_bytes"], 64 * MIB)
        self.assertEqual(result["quota_mode"], "hard")
        self.assertLess(result["allocated_bytes"], MIB)
        self.assertNotIn("used_bytes", result)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(self.admitted, [64 * MIB])
        self.assertEqual(
            result, self.reopen().create("scope", 1, quota_bytes=64 * MIB, fence=1)
        )
        self.assertEqual(self.host.calls.count("create"), 1)

    def test_concurrent_creation_formats_once(self):
        stores = [self.reopen() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda store: store.create(
                        "scope", 1, quota_bytes=64 * MIB, fence=1
                    ),
                    stores,
                )
            )
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(self.host.calls.count("create"), 1)

    def test_create_recovery_never_overwrites_a_completed_image(self):
        self.host.after["create"] = RuntimeError("crash")
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.create()
        original = self.image().read_bytes()
        self.assertTrue(self.record()["pending"])
        with self.assertRaises(ProjectVolumeError):
            self.store.delete("scope", 1, fence=2)
        result = self.reopen().create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.assertFalse(result["pending"])
        self.assertEqual(self.image().read_bytes(), original)
        self.assertEqual(self.host.calls.count("create"), 1)

    def test_unknown_partial_header_is_not_reformatted(self):
        with patch.object(
            self.host,
            "create",
            side_effect=lambda path, quota: path.write_bytes(b"partial"),
        ):
            with self.assertRaises(ProjectVolumeError):
                self.create()
        with self.assertRaises(ProjectVolumeError):
            self.reopen().create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.assertEqual(self.image().read_bytes(), b"partial")
        self.assertNotIn("create", self.host.calls)

    def test_owner_instance_backend_and_generation_are_bound(self):
        self.create()
        for call in (
            lambda: self.store.status("other", 1),
            lambda: VmProjectDisks(self.root, "b" * 32, host=self.host).status(
                "scope", 1
            ),
            lambda: ProjectVolumes(self.root, INSTANCE, host=self.host).status(
                "scope", 1
            ),
            lambda: self.store.status("scope", 1, generation=2),
        ):
            with self.assertRaises(ProjectVolumeError):
                call()

    def test_invalid_quota_fence_and_generation_do_not_create_image(self):
        for changes in (
            {"quota_bytes": True},
            {"quota_bytes": 63 * MIB},
            {"quota_bytes": 64 * MIB + 1},
            {"fence": 0},
            {"fence": True},
            {"generation": 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(ProjectVolumeError):
                self.create(**changes)
        self.assertFalse(self.host.images)

    def test_capacity_rejection_is_before_disk_creation(self):
        self.store.capacity_check = lambda size: (_ for _ in ()).throw(
            ProjectVolumeError("capacity")
        )
        with self.assertRaisesRegex(ProjectVolumeError, "capacity"):
            self.create()
        self.assertFalse(self.host.images)
        self.assertFalse((self.root / "p1/volume.json").exists())

    def test_missing_or_replaced_disk_is_not_silently_recreated(self):
        self.create()
        image = self.image()
        image.rename(image.with_suffix(".retained"))
        with self.assertRaises(ProjectVolumeError):
            self.create()
        image.write_bytes(b"replacement")
        with self.assertRaises(ProjectVolumeError):
            self.create()
        self.assertEqual(image.read_bytes(), b"replacement")
        self.assertEqual(self.host.calls.count("create"), 1)

    def test_symlink_hardlink_and_permissions_reject(self):
        self.create()
        image = self.image()
        alias = image.with_suffix(".alias")
        os.link(image, alias)
        with self.assertRaises(ProjectVolumeError):
            self.store.status("scope", 1)
        alias.unlink()
        image.chmod(0o644)
        with self.assertRaises(ProjectVolumeError):
            self.store.status("scope", 1)
        image.chmod(0o600)
        image.rename(alias)
        image.symlink_to(alias)
        with self.assertRaises(ProjectVolumeError):
            self.store.delete("scope", 1, fence=2)
        self.assertTrue(alias.exists())

    def test_unexpected_backing_external_data_and_geometry_are_rejected(self):
        self.create()
        key = str(self.image())
        original = copy.deepcopy(self.host.images[key])
        variants = [
            {"virtual-size": 128 * MIB},
            {"virtual-size": True},
            {"backing-filename": "/secret"},
            {"encrypted": True},
            {"snapshots": [{"name": "unexpected"}]},
            {"format-specific": []},
            {"format-specific": {"type": "qcow2", "data": []}},
        ] + [
            {"format-specific": {"type": "qcow2", "data": {field: value}}}
            for field, value in (
                ("data-file", "/elsewhere"),
                ("corrupt", True),
                ("encrypt", {}),
                ("encrypt", {"format": "luks"}),
            )
        ]
        for change in variants:
            self.host.images[key] = {**original, **change}
            with self.subTest(change=change), self.assertRaises(ProjectVolumeError):
                self.store.status("scope", 1)
        self.host.images[key] = original

    def test_claim_protects_disk_before_domain_exists_and_across_restart(self):
        self.create()
        binding = self.store.claim("scope", 1, "s123456", fence=2)
        self.assertEqual(binding.path, self.image())
        self.assertEqual(binding.filesystem_uuid, self.record()["fs_uuid"])
        self.assertEqual(binding, self.reopen().claim("scope", 1, "s123456", fence=2))
        for call in (
            lambda: self.store.delete("scope", 1, fence=3),
            lambda: self.store.grow("scope", 1, quota_bytes=96 * MIB, fence=3),
            lambda: self.store.claim("scope", 1, "s654321", fence=3),
            lambda: self.store.claim("scope", 1, "s123456", fence=1),
        ):
            with self.assertRaises(ProjectVolumeError):
                call()
        self.assertEqual(self.record()["fence"], 2)

    def test_release_requires_domain_removal_and_matches_claim(self):
        self.create()
        self.store.claim("scope", 1, "s123456", fence=2)
        self.host.attached.add("gaoji-vm-s123456")
        self.assertTrue(self.store.status("scope", 1)["attached"])
        with self.assertRaises(ProjectVolumeError):
            self.store.release("scope", 1, "s123456", fence=3)
        self.host.attached.clear()
        with self.assertRaises(ProjectVolumeError):
            self.store.release("scope", 1, "s654321", fence=3)
        result = self.store.release("scope", 1, "s123456", fence=3)
        self.assertIsNone(result["sandbox_id"])
        self.assertEqual(result, self.reopen().release("scope", 1, "s123456", fence=3))
        self.assertTrue(self.image().exists())

    def test_unclaimed_foreign_consumer_is_detected_not_adopted(self):
        self.create()
        self.host.attached.add("another-domain")
        for call in (
            lambda: self.store.status("scope", 1),
            lambda: self.store.claim("scope", 1, "s123456", fence=2),
            lambda: self.store.delete("scope", 1, fence=2),
        ):
            with self.assertRaises(ProjectVolumeError):
                call()

    def test_matching_domain_name_does_not_authorize_unjournalled_attachment(self):
        self.create()
        self.host.attached.add("gaoji-vm-s123456")
        with self.assertRaises(ProjectVolumeError):
            self.store.claim("scope", 1, "s123456", fence=2)
        self.assertIsNone(self.record()["sandbox_id"])

    def test_claim_and_release_reconcile_lost_acknowledgements(self):
        self.create()
        for operation, fence in (("claim", 2), ("release", 3)):
            original = self.store._save

            def fail_finish(directory, record):
                if not record["pending"]:
                    raise RuntimeError("lost ack")
                return original(directory, record)

            with patch.object(self.store, "_save", side_effect=fail_finish):
                with self.assertRaisesRegex(RuntimeError, "lost ack"):
                    getattr(self.store, operation)("scope", 1, "s123456", fence=fence)
            getattr(self.reopen(), operation)("scope", 1, "s123456", fence=fence)
            self.assertFalse(self.record()["pending"])

    def test_delete_recovery_keeps_tombstone_and_generation_fence(self):
        self.create()
        original = self.store._save

        def fail_finish(directory, record):
            if record.get("deleted"):
                raise RuntimeError("lost deletion ack")
            return original(directory, record)

        with patch.object(self.store, "_save", side_effect=fail_finish):
            with self.assertRaisesRegex(RuntimeError, "lost deletion ack"):
                self.store.delete("scope", 1, fence=2)
        self.assertFalse(self.image().exists())
        result = self.reopen().delete("scope", 1, fence=2)
        self.assertTrue(result["deleted"])
        self.assertEqual(result, self.store.delete("scope", 1, fence=2))
        with self.assertRaises(ProjectVolumeError):
            self.create(fence=3)
        self.create(fence=3, generation=2)
        with self.assertRaises(ProjectVolumeError):
            self.store.delete("scope", 1, fence=4, generation=1)

    def test_grow_reconciles_after_utility_success_without_second_resize(self):
        self.create()
        self.host.after["grow"] = RuntimeError("resize ack lost")
        with self.assertRaisesRegex(RuntimeError, "resize ack lost"):
            self.store.grow("scope", 1, quota_bytes=96 * MIB, fence=2)
        self.assertEqual(self.record()["quota_bytes"], 64 * MIB)
        result = self.reopen().grow("scope", 1, quota_bytes=96 * MIB, fence=2)
        self.assertEqual(result["quota_bytes"], 96 * MIB)
        self.assertEqual(self.host.calls.count("grow"), 1)
        with self.assertRaises(ProjectVolumeError):
            self.store.grow("scope", 1, quota_bytes=64 * MIB, fence=3)

    def test_initialized_filesystem_cannot_use_raw_image_growth(self):
        self.create()
        record = self.record()
        record["guest_initialized"] = True
        write_json(self.root / "p1/volume.json", record)
        with self.assertRaisesRegex(ProjectVolumeError, "filesystem resize protocol"):
            self.store.grow("scope", 1, quota_bytes=96 * MIB, fence=2)
        self.assertEqual(self.record(), record)
        self.assertNotIn("grow", self.host.calls)

    def test_unjournalled_missing_disk_cannot_be_declared_deleted(self):
        self.create()
        self.image().unlink()
        with self.assertRaises(ProjectVolumeError):
            self.store.delete("scope", 1, fence=2)
        self.assertFalse(self.record().get("deleted"))

    def test_unknown_directory_files_and_uuid_path_traversal_fail_closed(self):
        self.create()
        self.store.delete("scope", 1, fence=2)
        (self.root / "p1/keep.txt").write_text("retained")
        with self.assertRaises(ProjectVolumeError):
            self.create(fence=3, generation=2)
        record = self.record()
        record["fs_uuid"] = "../../outside"
        write_json(self.root / "p1/volume.json", record)
        with self.assertRaises(ProjectVolumeError):
            self.store.status("scope", 1)


class QcowHostTests(unittest.TestCase):
    def test_consumers_inspects_live_and_inactive_definitions_and_backing_stores(self):
        host = QcowProjectHost()
        identifier = "11111111-1111-4111-8111-111111111111"
        root = ElementTree.Element("domain")
        ElementTree.SubElement(root, "name").text = "gaoji-vm-s123456"
        ElementTree.SubElement(root, "uuid").text = identifier
        disk = ElementTree.SubElement(ElementTree.SubElement(root, "devices"), "disk")
        ElementTree.SubElement(
            ElementTree.SubElement(disk, "backingStore"),
            "source",
            file="/project.qcow2",
        )
        xml = ElementTree.tostring(root, encoding="unicode")
        with patch.object(host, "run", side_effect=[identifier, xml, xml]) as run:
            self.assertEqual(
                host.consumers(Path("/project.qcow2")), {"gaoji-vm-s123456"}
            )
        self.assertEqual(run.call_args_list[-1].args[-1], "--inactive")

    def test_broken_domain_inventory_is_not_empty_inventory(self):
        with patch.object(QcowProjectHost, "run", return_value="not-a-uuid"):
            with self.assertRaises(ProjectVolumeError):
                QcowProjectHost().consumers(Path("/project.qcow2"))

    @unittest.skipUnless(
        shutil.which("qemu-img") and shutil.which("qemu-io"),
        "Local QEMU disk tools are unavailable",
    )
    def test_real_qcow2_creation_reopen_growth_and_delete(self):
        host = QcowProjectHost()
        with (
            tempfile.TemporaryDirectory(prefix="gaoji-real-qcow2-") as temporary,
            patch.object(host, "consumers", return_value=set()),
        ):
            root = Path(temporary) / "projects"
            store = VmProjectDisks(root, INSTANCE, host=host)
            result = store.create("scope", 1, quota_bytes=64 * MIB, fence=1)
            self.assertLess(result["allocated_bytes"], 4 * MIB)
            binding = store.claim("scope", 1, "s123456", fence=2)
            self.assertEqual(host.info(binding.path)["virtual-size"], 64 * MIB)
            host.run(
                "qemu-io",
                "-f",
                "qcow2",
                "-c",
                "write -P 0x5a 1048576 4096",
                str(binding.path),
            )
            store.release("scope", 1, "s123456", fence=3)
            reopened = VmProjectDisks(root, INSTANCE, host=host)
            self.assertEqual(
                reopened.grow("scope", 1, quota_bytes=96 * MIB, fence=4)["quota_bytes"],
                96 * MIB,
            )
            self.assertEqual(host.info(binding.path)["virtual-size"], 96 * MIB)
            host.run(
                "qemu-io",
                "-f",
                "qcow2",
                "-c",
                "read -P 0x5a 1048576 4096",
                str(binding.path),
            )
            self.assertTrue(reopened.delete("scope", 1, fence=5)["deleted"])
            self.assertFalse(binding.path.exists())
