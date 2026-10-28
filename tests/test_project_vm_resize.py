"""Host and guest resize journals together; libvirt/block devices are fixtures."""

import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from xml.etree import ElementTree

from src.local_runtime.files import read_json
from src.sandbox_runtime import project_vm_guest as disk
from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_vm_gate import LinuxGuestCommands, ProjectGuestGate
from src.sandbox_runtime.project_vm_resize import NAMESPACE, VmProjectResize
from src.sandbox_runtime.project_vm_resize_guest import ProjectGuestResize
from src.sandbox_runtime.project_vm_runtime import VmProjectRuntime
from tests.test_project_vm_disks import MIB
from tests.test_project_vm_provision import RESERVATION, VmProvisionFixture
from tests.test_project_vm_resize_guest import ResizeDisk


class VmResizeTests(VmProvisionFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guest_disk = None
        self.persistent = {}
        await super().asyncSetUp()
        await self.boot()
        self.persistent = {key: value["xml"] for key, value in self.domains.items()}
        self.guest_root = self.root / "guest"
        self.guest_root.mkdir(mode=0o700)
        self.config_path = self.guest_root / "config.json"
        config = {**read_json(self.directory() / "metadata.json")["project"], "initialize": False}
        disk.write_private_json(self.config_path, config)
        self.enterContext(patch.object(disk, "CONFIG_PATH", self.config_path))
        self.enterContext(patch.object(disk, "load_config", side_effect=lambda: disk.validate_config(read_json(self.config_path))))
        self.guest_disk = ResizeDisk(config)
        self.guest_disk.fs = {"TYPE": "ext4", "UUID": config["filesystem_uuid"]}
        disk.ProjectGuestWorkspace(config, host=self.guest_disk).mount()
        self.commands = Mock(spec=LinuxGuestCommands)
        self.commands.empty.return_value = True
        self.commands.sandbox_processes.return_value = False
        self.gate().quiesce(3)
        self.manager._guest_exec_raw = AsyncMock(side_effect=self.guest)
        self.resizer = VmProjectResize(self.manager, self.store, host=self.host)
        self.effects.clear()
        self.guest_disk.calls.clear()

    def gate(self):
        config = disk.load_config()
        return ProjectGuestGate(config, state_path=self.guest_root / "gate.json", lock_root=self.guest_root,
            commands=self.commands, workspace=disk.ProjectGuestWorkspace(config, host=self.guest_disk))

    async def virsh(self, *args, **kwargs):
        action = args[0]
        if action == "dumpxml" and args[1] in self.persistent:
            return self.persistent[args[1]] if "--inactive" in args else self.domains[args[1]]["xml"]
        if action in {"domblkinfo", "blockresize"}:
            self.assertEqual(args[1], self.record()["workspace"]["domain_uuid"])
            self.assertEqual(args[2], "vdb")
            path = self.store._path(self.root / "data/p1", self.record())
            if action == "blockresize":
                self.assertTrue(args[3].endswith("B"))
                size = int(args[3][:-1])
                if size < self.guest_disk.disk["bytes"]:
                    self.assertEqual(self.guest_disk.geometry, size)
                    self.assertEqual(self.guest_disk.rows, [])
                self.host.images[str(path)]["virtual-size"] = size
                self.guest_disk.disk["bytes"] = size
                self.done("blockresize")
                return "resized"
            return f"Capacity: {self.host.images[str(path)]['virtual-size']}\nAllocation: 4096\nPhysical: 4096\n"
        if action == "metadata":
            flag = args[2]
            root = ElementTree.fromstring(self.domains[args[1]]["xml"] if flag == "--live" else self.persistent[args[1]])
            container = root.find("metadata")
            container.remove(container.find("{" + NAMESPACE + "}workspace"))
            container.append(ElementTree.fromstring(args[args.index("--set") + 1]))
            value = ElementTree.tostring(root, encoding="unicode")
            if flag == "--live":
                self.domains[args[1]]["xml"] = value
            else:
                self.persistent[args[1]] = value
            self.done("metadata" + flag)
            return "updated"
        return await super().virsh(*args, **kwargs)

    async def guest(self, sandbox_id, path, args, **kwargs):
        if self.guest_disk is None:
            return await super().guest(sandbox_id, path, args, **kwargs)
        if args[-1].endswith("project_gate.py"):
            request = json.loads(kwargs["input_bytes"])
            if request["action"] == "resize":
                self.assertEqual(kwargs["domain_uuid"], self.record()["workspace"]["domain_uuid"])
                result = ProjectGuestResize(self.gate()).operate(request["phase"], request["epoch"],
                    request["from_quota_bytes"], request["quota_bytes"])
                self.done("guest-" + request["phase"])
            else:
                result = getattr(self.gate(), request["action"])(request["epoch"])
            return 0, json.dumps(result).encode(), b""
        return await super().guest(sandbox_id, path, args, **kwargs)

    async def resize(self, target=96, fence=4, **extra):
        return await self.resizer.resize("scope", 1, **{
            "generation": 1, "reservation": RESERVATION, "fence": fence,
            "quota_bytes": target * MIB, **extra})

    def restart(self):
        self.reopen()
        self.resizer = VmProjectResize(self.manager, self.store, host=self.host)

    async def test_growth_commits_device_guest_domain_and_frozen_files(self):
        system = (self.directory() / "disk.qcow2").read_bytes()
        seed = (self.directory() / "seed.iso").read_bytes()
        result = await self.resize()
        self.assertEqual((result["state"], result["quota_bytes"], result["pending"]), ("resized", 96 * MIB, False))
        self.assertTrue(result["maintenance_active"])
        self.assertEqual(self.guest_disk.geometry, 96 * MIB)
        self.assertEqual(disk.load_config()["quota_bytes"], 96 * MIB)
        self.assertEqual((self.directory() / "disk.qcow2").read_bytes(), system)
        self.assertEqual((self.directory() / "seed.iso").read_bytes(), seed)
        self.assertLess(self.effects.index("blockresize"), self.effects.index("guest-apply"))
        self.assertEqual(self.gate()._state()["mode"], "quiesced")
        self.assertEqual(await self.resize(), result)
        self.assertEqual(self.effects.count("blockresize"), 1)
        self.assertEqual(self.guest_disk.calls.count("resize"), 1)
        await self.manager.project_mounts.reserve("release", "scope", 1,
            generation=1, fence=5, reservation=RESERVATION)
        await self.manager._owned_running("scope", result["sandbox_id"])

    async def test_shrink_finishes_filesystem_before_host_truncation(self):
        await self.resize()
        self.effects.clear()
        self.guest_disk.calls.clear()
        result = await self.resize(64, fence=5)
        self.assertEqual(result["quota_bytes"], 64 * MIB)
        self.assertLess(self.effects.index("guest-apply"), self.effects.index("blockresize"))
        self.assertEqual(self.guest_disk.calls.count("resize"), 1)
        self.assertTrue(self.guest_disk.rows)

    async def test_lost_device_ack_reconciles_without_second_resize(self):
        self.after["blockresize"] = RuntimeError("lost device ack")
        with self.assertRaises(RuntimeError):
            await self.resize()
        self.assertEqual(self.record()["vm_resize"]["stage"], "resizing_device")
        self.restart()
        await self.resize()
        self.assertEqual(self.effects.count("blockresize"), 1)

    async def test_lost_guest_ack_resumes_matching_guest_journal(self):
        self.after["guest-apply"] = RuntimeError("lost guest ack")
        with self.assertRaises(RuntimeError):
            await self.resize()
        self.restart()
        await self.resize()
        self.assertEqual(self.guest_disk.calls.count("resize"), 1)

    async def test_lost_commit_ack_does_not_revert_guest_configuration(self):
        self.after["guest-commit"] = RuntimeError("lost commit ack")
        with self.assertRaises(RuntimeError):
            await self.resize()
        self.assertEqual(disk.load_config()["quota_bytes"], 96 * MIB)
        self.assertEqual(self.record()["quota_bytes"], 64 * MIB)
        self.restart()
        await self.resize()
        self.assertEqual(self.record()["quota_bytes"], 96 * MIB)

    async def test_partial_live_metadata_publication_recovers_persistent_definition(self):
        self.after["metadata--live"] = RuntimeError("lost live ack")
        with self.assertRaises(RuntimeError):
            await self.resize()
        self.assertTrue(self.record()["pending"])
        self.restart()
        await self.resize()
        self.assertEqual(self.effects.count("metadata--live"), 1)
        self.assertEqual(self.effects.count("metadata--config"), 1)

    async def test_partial_file_publication_preserves_other_boot_artifacts(self):
        original = self.resizer._write
        def fail(path, content, staging):
            original(path, content, staging)
            raise RuntimeError("lost file publication ack")
        with patch.object(self.resizer, "_write", side_effect=fail), self.assertRaises(RuntimeError):
            await self.resize()
        self.assertEqual(self.record()["vm_resize"]["stage"], "publishing_files")
        self.restart()
        await self.resize()
        self.provisioner._verify_files(self.manager, self.store.boot_record(
            "scope", 1, generation=1, reservation=RESERVATION))
        self.assertFalse((self.directory() / ".quota-4").exists())

    async def test_interrupted_staged_file_is_recovered_and_removed(self):
        original = self.resizer._write
        def fail(path, content, staging):
            with patch("src.sandbox_runtime.project_vm_resize.os.replace", side_effect=RuntimeError("lost before rename")):
                original(path, content, staging)
        with patch.object(self.resizer, "_write", side_effect=fail), self.assertRaises(RuntimeError):
            await self.resize()
        self.assertTrue(list((self.directory() / ".quota-4").iterdir()))
        self.restart()
        await self.resize()
        self.assertFalse((self.directory() / ".quota-4").exists())

    async def test_unknown_staging_data_is_not_deleted(self):
        with patch.object(self.resizer, "_write", side_effect=RuntimeError("interrupted")), self.assertRaises(RuntimeError):
            await self.resize()
        path = self.directory() / ".quota-4" / "user-file"
        path.write_text("retain this")
        self.restart()
        with self.assertRaisesRegex(SandboxError, "Unrecognized"):
            await self.resize()
        self.assertEqual(path.read_text(), "retain this")

    async def test_completed_host_ack_replays_without_repeating_effects(self):
        original = self.store._finish
        def fail(*args):
            original(*args)
            raise RuntimeError("lost final ack")
        with patch.object(self.store, "_finish", side_effect=fail), self.assertRaises(RuntimeError):
            await self.resize()
        self.restart()
        await self.resize()
        self.assertEqual(self.effects.count("blockresize"), 1)
        self.assertEqual(self.effects.count("metadata--live"), 1)

    async def test_wrong_target_fence_and_reservation_cannot_replace_pending_resize(self):
        self.after["guest-prepare"] = RuntimeError("lost prepare ack")
        with self.assertRaises(RuntimeError):
            await self.resize()
        before = self.record()
        for changes in ({"target": 128}, {"fence": 5}, {"fence": 3}, {"reservation": "e" * 32}, {"generation": 2}):
            with self.subTest(changes=changes), self.assertRaises(SandboxError):
                await self.resize(**changes)
            self.assertEqual(self.record(), before)

    async def test_busy_guest_and_insufficient_usage_never_resize_device(self):
        self.commands.sandbox_processes.return_value = True
        with self.assertRaises(disk.GuestDiskError):
            await self.resize()
        self.assertNotIn("blockresize", self.effects)
        self.commands.sandbox_processes.return_value = False
        await self.resize()
        self.effects.clear()
        self.guest_disk.available = MIB
        with self.assertRaises(disk.GuestDiskError):
            await self.resize(64, fence=5)
        self.assertNotIn("blockresize", self.effects)

    async def test_unknown_domain_geometry_or_file_does_not_get_adopted(self):
        self.after["blockresize"] = RuntimeError("lost device ack")
        with self.assertRaises(RuntimeError):
            await self.resize()
        (self.directory() / "user-data").write_text("foreign bootstrap")
        with self.assertRaisesRegex(SandboxError, "bootstrap file changed"):
            await self.resize()
        self.assertEqual(self.effects.count("blockresize"), 1)

    async def test_foreign_disk_consumer_blocks_resize_before_guest_effect(self):
        self.host.attached.add("another-vm")
        with self.assertRaises(SandboxError):
            await self.resize()
        self.assertNotIn("blockresize", self.effects)
        self.assertNotIn("guest-prepare", self.effects)

    async def test_foreign_inactive_domain_cannot_hide_behind_valid_live_domain(self):
        identifier = self.record()["workspace"]["domain_uuid"]
        root = ElementTree.fromstring(self.persistent[identifier])
        for item in root.findall("./devices/disk"):
            if item.find("target").get("dev") == "vdb":
                item.find("source").set("file", "/foreign.qcow2")
        self.persistent[identifier] = ElementTree.tostring(root, encoding="unicode")
        with self.assertRaisesRegex(SandboxError, "definition changed"):
            await self.resize()
        self.assertNotIn("blockresize", self.effects)

    async def test_false_guest_shrink_receipt_cannot_authorize_device_truncation(self):
        await self.resize()
        self.effects.clear()
        original = self.guest
        async def invalid(*args, **kwargs):
            code, output, error = await original(*args, **kwargs)
            request = json.loads(kwargs["input_bytes"])
            if request.get("phase") == "apply":
                value = json.loads(output)
                value["filesystem_bytes"] = 96 * MIB
                output = json.dumps(value).encode()
            return code, output, error
        self.manager._guest_exec_raw.side_effect = invalid
        with self.assertRaisesRegex(SandboxError, "receipt"):
            await self.resize(64, fence=5)
        self.assertEqual(self.guest_disk.disk["bytes"], 96 * MIB)
        self.assertNotIn("blockresize", self.effects)
        self.manager._guest_exec_raw.side_effect = self.guest
        await self.resize(64, fence=5)
        self.assertEqual(self.effects.count("blockresize"), 1)

    async def test_unmounted_workspace_stays_unmounted_after_resize(self):
        self.guest_disk.rows = []
        result = await self.resize()
        self.assertEqual(result["quota_bytes"], 96 * MIB)
        self.assertEqual(self.guest_disk.rows, [])
        self.assertIsNotNone(self.record()["guest_unmounted"])

    async def test_corrupted_capacity_observation_does_not_issue_resize(self):
        original = self.virsh
        async def broken(*args, **kwargs):
            if args[0] == "domblkinfo":
                return "Capacity: unknown\n"
            return await original(*args, **kwargs)
        self.manager._virsh.side_effect = broken
        with self.assertRaisesRegex(SandboxError, "capacity"):
            await self.resize()
        self.assertNotIn("blockresize", self.effects)

    async def test_same_vm_restart_during_resize_does_not_recreate_its_disks(self):
        self.after["blockresize"] = RuntimeError("host lost")
        with self.assertRaises(RuntimeError):
            await self.resize()
        identifier = self.record()["workspace"]["domain_uuid"]
        self.domains[identifier]["state"] = "shut off"
        self.restart()
        await self.resize()
        self.assertEqual(self.effects.count("start"), 1)
        self.assertEqual(self.host.calls.count("create"), 1)
        self.assertEqual(self.host.calls.count("overlay"), 1)

    async def test_two_callers_share_one_physical_resize(self):
        other = copy.copy(self.manager)
        other._create_lock, other._exec_locks = asyncio.Lock(), {}
        second = VmProjectResize(other, self.store, host=self.host)
        results = await asyncio.gather(self.resize(), second.resize("scope", 1,
            generation=1, reservation=RESERVATION, fence=4, quota_bytes=96 * MIB))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.effects.count("blockresize"), 1)

    async def test_private_runtime_dispatches_resize_but_keeps_maintenance_held(self):
        runtime = VmProjectRuntime(self.manager, self.store, host=self.host)
        result = await runtime.operate("resize", "scope", 1, generation=1,
            fence=4, reservation=RESERVATION, quota_bytes=96 * MIB)
        self.assertTrue(result["maintenance_active"])
        self.assertEqual(result["state"], "resized")
