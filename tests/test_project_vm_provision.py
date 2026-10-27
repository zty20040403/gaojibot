"""Resumable host provisioning with fixture libvirt, QEMU and guest responses."""

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

import nonebot

os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.local_runtime.files import read_json
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
from src.sandbox_runtime.docker import SandboxError
from src.sandbox_runtime.project_vm_disks import QcowProjectHost, VmProjectDisks
from src.sandbox_runtime.project_vm_mounts import VmProjectMounts
from src.sandbox_runtime.project_vm_provision import VmProjectProvisioner
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumeError
from tests.test_project_vm_disks import DiskFixture, INSTANCE

RESERVATION = "d" * 32


class ProvisionHost(DiskFixture):
    def run(self, *args, **kwargs):
        if args[0] == "qemu-img":
            path = Path(args[-2])
            path.write_bytes(b"fixture-system-overlay")
            self.images[str(path)] = {
                "format": "qcow2", "virtual-size": int(args[-1]),
                "full-backing-filename": args[args.index("-b") + 1],
                "backing-filename-format": "qcow2", "format-specific": {"type": "qcow2", "data": {}},
            }
            self.done("overlay")
        elif args[0] == "cloud-localds":
            Path(args[1]).write_bytes(b"fixture-seed\n" + Path(args[2]).read_bytes())
            self.done("seed")
        else:
            raise AssertionError(args)
        return ""


class ProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-vm-provision-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.host = ProvisionHost()
        self.base = self.root / "base.qcow2"
        self.base.write_bytes(b"fixture-pinned-base")
        self.base.chmod(0o600)
        self.host.images[str(self.base)] = {"format": "qcow2", "virtual-size": 4 * 1024**3,
                                           "format-specific": {"type": "qcow2", "data": {}}}
        self.store = VmProjectDisks(self.root / "data", INSTANCE, host=self.host)
        self.store.create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.store.reserve("scope", 1, generation=1, fence=2, action="reserve", reservation=RESERVATION)
        self.manager = VmSandboxManager(image=str(self.base), root=str(self.root / "vm"), disk_gib=4)
        self.manager.project_mounts = VmProjectMounts(self.manager, self.store)
        self.manager._virsh = AsyncMock(side_effect=self.virsh)
        self.manager._running = AsyncMock(side_effect=self.running)
        self.manager._wait_guest = AsyncMock()
        self.manager._guest_exec = AsyncMock(side_effect=self.guest)
        self.domains = {}
        self.after = {}
        self.effects = []
        self.provisioner = VmProjectProvisioner(self.manager, self.store, host=self.host)
        patcher = patch("shutil.which", return_value="/test/qemu")
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self):
        return read_json(self.root / "data/p1/volume.json")

    def directory(self):
        return self.manager._directory(self.record()["workspace"]["sandbox_id"])

    def done(self, action):
        self.effects.append(action)
        if action in self.after:
            raise self.after.pop(action)

    async def running(self, sandbox_id):
        name = self.manager._name(sandbox_id)
        return any(ElementTree.fromstring(domain["xml"]).findtext("name") == name
                   and domain["state"] == "running" for domain in self.domains.values())

    async def virsh(self, *args, **kwargs):
        action = args[0]
        if action == "list":
            return "\n".join(self.domains)
        if action == "dumpxml":
            return self.domains[args[1]]["xml"]
        if action == "domstate":
            return self.domains[args[1]]["state"]
        if action == "define":
            xml = Path(args[1]).read_text()
            root = ElementTree.fromstring(xml)
            identity = root.findtext("uuid")
            self.assertNotIn(identity, self.domains)
            self.domains[identity] = {"xml": xml, "state": "shut off"}
            self.host.attached = {root.findtext("name")}
            self.done("define")
            return "defined"
        if action == "start":
            self.assertEqual(self.domains[args[1]]["state"], "shut off")
            self.domains[args[1]]["state"] = "running"
            self.done("start")
            return "started"
        raise AssertionError(args)

    async def guest(self, sandbox_id, path, args, **kwargs):
        if "cloud-init" in " ".join(args):
            self.done("cloud-init")
            return 0, b"done", b""
        self.assertIn(args[-1], {"mount", "verify"})
        metadata = json.loads((self.directory() / "metadata.json").read_text())
        receipt = {**metadata["project"], "mounted": True, "verified": True,
                   "initialization_sealed": True, "used_bytes": MIB}
        self.done("mount")
        return 0, json.dumps(receipt).encode(), b""

    async def boot(self, **changes):
        return await self.provisioner.boot("scope", 1, **{
            "generation": 1, "fence": 3, "reservation": RESERVATION, **changes,
        })

    def reopen(self):
        self.store = VmProjectDisks(self.root / "data", INSTANCE, host=self.host)
        self.provisioner = VmProjectProvisioner(self.manager, self.store, host=self.host)
        self.manager.project_mounts = VmProjectMounts(self.manager, self.store)

    async def test_complete_boot_keeps_data_separate_and_replay_does_not_recreate(self):
        result = await self.boot()
        self.assertEqual(result["state"], "running")
        self.assertTrue(result["guest_initialized"])
        self.assertTrue(result["maintenance_active"])
        self.assertFalse(result["pending"])
        first = self.record()
        self.reopen()
        self.assertEqual(await self.boot(), result)
        self.assertEqual(self.record()["workspace"], first["workspace"])
        self.assertEqual(self.host.calls.count("overlay"), 1)
        self.assertEqual(self.effects.count("define"), 1)
        self.assertEqual(self.effects.count("start"), 1)
        self.assertEqual(self.host.calls.count("create"), 1)
        self.store.reserve("scope", 1, generation=1, fence=4, action="release", reservation=RESERVATION)
        metadata = await self.manager._owned_running("scope", result["sandbox_id"])
        self.assertEqual(metadata["purpose"], "project")

    async def test_interrupted_seed_rebuilds_only_proven_preboot_system_files(self):
        self.host.after["seed"] = RuntimeError("lost seed creation reply")
        with self.assertRaises(RuntimeError):
            await self.boot()
        first = self.record()
        self.assertEqual(first["workspace"]["stage"], "preparing")
        self.assertEqual(self.domains, {})
        data = self.store._path(self.root / "data/p1", first)
        data_before = data.read_bytes()
        self.reopen()
        result = await self.boot()
        self.assertEqual(result["sandbox_id"], first["sandbox_id"])
        self.assertEqual(data.read_bytes(), data_before)
        self.assertEqual(self.host.calls.count("overlay"), 2)
        self.assertEqual(self.host.calls.count("create"), 1)

    async def test_lost_define_reply_reuses_existing_immutable_domain(self):
        self.after["define"] = RuntimeError("lost define reply")
        with self.assertRaises(RuntimeError):
            await self.boot()
        self.assertEqual(self.record()["workspace"]["stage"], "defining")
        system = self.directory() / "disk.qcow2"
        before = system.stat().st_ino
        self.reopen()
        await self.boot()
        self.assertEqual(system.stat().st_ino, before)
        self.assertEqual(self.effects.count("define"), 1)
        self.assertEqual(self.host.calls.count("overlay"), 1)

    async def test_lost_start_reply_does_not_start_twice_or_erase_system_disk(self):
        self.after["start"] = RuntimeError("lost start reply")
        with self.assertRaises(RuntimeError):
            await self.boot()
        self.assertEqual(self.record()["workspace"]["stage"], "starting")
        system = self.directory() / "disk.qcow2"
        system.write_bytes(system.read_bytes() + b"guest retained this")
        before = system.read_bytes()
        self.reopen()
        await self.boot()
        self.assertEqual(system.read_bytes(), before)
        self.assertEqual(self.effects.count("start"), 1)
        self.assertEqual(self.host.calls.count("overlay"), 1)

    async def test_new_fence_resumes_stopped_domain_and_uses_frozen_configuration(self):
        await self.boot()
        original = copy.deepcopy(self.record()["workspace"])
        self.domains[original["domain_uuid"]]["state"] = "shut off"
        self.manager.memory_mib = 2048
        self.manager.image = self.root / "new-not-installed-base.qcow2"
        self.reopen()
        await self.boot(fence=4)
        self.assertEqual(self.record()["workspace"]["spec"], original["spec"])
        self.assertEqual(self.effects.count("start"), 2)
        self.assertEqual(self.effects.count("define"), 1)
        self.assertEqual(self.host.calls.count("overlay"), 1)
        self.store.reserve("scope", 1, generation=1, fence=5, action="release", reservation=RESERVATION)
        await self.manager._owned_running("scope", original["sandbox_id"])

    async def test_missing_previously_booted_domain_fails_without_replacement(self):
        await self.boot()
        self.domains.clear()
        self.host.attached.clear()
        with self.assertRaisesRegex(SandboxError, "disappeared"):
            await self.boot(fence=4)
        self.assertEqual(self.effects.count("define"), 1)
        self.assertEqual(self.host.calls.count("overlay"), 1)
        self.assertTrue((self.directory() / "disk.qcow2").is_file())

    async def test_missing_or_replaced_prepared_disk_is_not_recreated(self):
        self.after["define"] = RuntimeError("lost define reply")
        with self.assertRaises(RuntimeError):
            await self.boot()
        system = self.directory() / "disk.qcow2"
        system.rename(system.with_suffix(".retained"))
        with self.assertRaises(OSError):
            await self.boot()
        system.write_bytes(b"replacement")
        system.chmod(0o600)
        with self.assertRaisesRegex(SandboxError, "replaced"):
            await self.boot()
        self.assertEqual(self.host.calls.count("overlay"), 1)

    async def test_unknown_preboot_files_prevent_cleanup(self):
        self.host.after["seed"] = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            await self.boot()
        unknown = self.directory() / "user-file"
        unknown.write_text("retain me")
        system = (self.directory() / "disk.qcow2").read_bytes()
        with self.assertRaisesRegex(SandboxError, "Unrecognized"):
            await self.boot()
        self.assertEqual(unknown.read_text(), "retain me")
        self.assertEqual((self.directory() / "disk.qcow2").read_bytes(), system)

    async def test_prepared_bootstrap_changes_are_rejected(self):
        self.after["start"] = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            await self.boot()
        (self.directory() / "user-data").write_text("foreign bootstrap")
        with self.assertRaisesRegex(SandboxError, "bootstrap file changed"):
            await self.boot()
        self.assertEqual(self.host.calls.count("overlay"), 1)

    async def test_old_fences_and_other_reservations_cannot_mutate_boot(self):
        self.after["start"] = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            await self.boot()
        before = copy.deepcopy(self.record())
        for changes in ({"fence": 2}, {"fence": 4}, {"reservation": "e" * 32}, {"generation": 2}):
            with self.subTest(changes=changes), self.assertRaises(ProjectVolumeError):
                await self.boot(**changes)
            self.assertEqual(self.record(), before)

    async def test_host_restart_after_start_or_guest_ack_resumes_the_same_disk(self):
        self.after["cloud-init"] = RuntimeError("host interrupted before guest verification")
        with self.assertRaises(RuntimeError):
            await self.boot()
        self.assertEqual(self.record()["workspace"]["stage"], "started")
        system = self.directory() / "disk.qcow2"
        before = system.stat().st_ino
        identity = self.record()["workspace"]["domain_uuid"]
        self.domains[identity]["state"] = "shut off"
        self.reopen()
        await self.boot()
        self.domains[identity]["state"] = "shut off"
        self.reopen()
        await self.boot()
        self.assertEqual(system.stat().st_ino, before)
        self.assertEqual(self.host.calls.count("overlay"), 1)
        self.assertEqual(self.effects.count("define"), 1)
        self.assertEqual(self.effects.count("start"), 3)

    async def test_start_retry_rechecks_capacity_after_an_interruption(self):
        original = self.virsh
        async def fail_start(*args, **kwargs):
            if args[0] == "start":
                raise RuntimeError("interrupted before start")
            return await original(*args, **kwargs)
        self.manager._virsh.side_effect = fail_start
        with self.assertRaises(RuntimeError):
            await self.boot()
        self.manager._virsh.side_effect = self.virsh
        self.assertEqual(self.record()["workspace"]["stage"], "starting")
        self.manager.max_total = 1
        identity = "33333333-3333-4333-8333-333333333333"
        self.domains[identity] = {"state": "running",
            "xml": f"<domain><name>other-workload</name><uuid>{identity}</uuid></domain>"}
        with self.assertRaisesRegex(SandboxError, "capacity"):
            await self.boot()
        self.assertNotIn("start", self.effects)
        del self.domains[identity]
        await self.boot()
        self.assertEqual(self.effects.count("start"), 1)

    async def test_invalid_base_metadata_is_rejected_before_journalling_boot(self):
        before = self.record()
        for metadata in (None, [], {"format-specific": None}, {"format-specific": {"data": []}},
                         {"format": "qcow2", "virtual-size": 4 * 1024**3,
                          "format-specific": {"type": "qcow2", "data": {"data-file": "/foreign"}}}):
            self.host.images[str(self.base)] = metadata
            with self.subTest(metadata=metadata), self.assertRaises(SandboxError):
                await self.boot()
            self.assertEqual(self.record(), before)

    async def test_boot_lock_serializes_repeated_callers(self):
        other = copy.copy(self.manager)
        other._create_lock, other._exec_locks = asyncio.Lock(), {}
        second = VmProjectProvisioner(other, self.store, host=self.host)
        results = await asyncio.gather(self.boot(), second.boot(
            "scope", 1, generation=1, fence=3, reservation=RESERVATION))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.effects.count("start"), 1)
        self.assertEqual(self.host.calls.count("overlay"), 1)

    async def test_cancel_during_preparation_finishes_local_write_before_unlock(self):
        entered, release = threading.Event(), threading.Event()
        prepare = self.provisioner._prepare
        def slow(*args):
            entered.set()
            release.wait(5)
            return prepare(*args)
        with patch.object(self.provisioner, "_prepare", side_effect=slow):
            operation = asyncio.create_task(self.boot())
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            operation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
        self.assertEqual(self.domains, {})
        self.assertEqual(self.record()["workspace"]["stage"], "preparing")
        await self.boot()
        self.assertEqual(self.effects.count("start"), 1)

    async def test_host_subprocess_is_reaped_on_cancellation(self):
        created = asyncio.Event()
        captured = []
        spawn = asyncio.create_subprocess_exec
        async def start(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            captured.append(process)
            created.set()
            return process
        with patch.object(asyncio, "create_subprocess_exec", side_effect=start):
            operation = asyncio.create_task(self.manager._run(sys.executable, "-c", "import time; time.sleep(30)"))
            await asyncio.wait_for(created.wait(), 3)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
        self.assertIsNotNone(captured[0].returncode)

    async def test_cancellation_while_spawning_waits_for_pid_then_reaps_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        captured = []
        spawn = asyncio.create_subprocess_exec
        async def delayed(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            captured.append(process)
            entered.set()
            await release.wait()
            return process
        with patch.object(asyncio, "create_subprocess_exec", side_effect=delayed):
            operation = asyncio.create_task(self.manager._run(sys.executable, "-c", "import time; time.sleep(30)"))
            await asyncio.wait_for(entered.wait(), 3)
            operation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
        self.assertIsNotNone(captured[0].returncode)

    async def test_host_timeout_reaps_process_and_reports_failure(self):
        captured = []
        spawn = asyncio.create_subprocess_exec
        async def track(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            captured.append(process)
            return process
        with patch.object(asyncio, "create_subprocess_exec", side_effect=track):
            with self.assertRaisesRegex(SandboxError, "超时"):
                await self.manager._run(sys.executable, "-c", "import time; time.sleep(30)", timeout=0.05)
        self.assertIsNotNone(captured[0].returncode)


@unittest.skipUnless(shutil.which("qemu-img") and shutil.which("qemu-io"), "Local QEMU image utilities required")
class RealSystemOverlayTests(unittest.TestCase):
    def test_real_overlay_bootstrap_and_guest_writes_preserve_independent_project_disk(self):
        class Host(QcowProjectHost):
            def consumers(self, path):
                return set()

            def run(self, *args, **kwargs):
                if args[0] == "cloud-localds":
                    # ISO creation and VM boot are a separate native-Linux gate.
                    Path(args[1]).write_bytes(b"seed-fixture")
                    return ""
                return super().run(*args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="gaoji-real-overlay-") as temporary:
            root = Path(temporary).resolve()
            host = Host()
            base = root / "base.qcow2"
            host.create(base, 64 * MIB)
            base.chmod(0o600)
            manager = VmSandboxManager(image=str(base), root=str(root / "vm"), disk_gib=4)
            manager.root.mkdir(mode=0o700)
            disks = VmProjectDisks(root / "data", INSTANCE, host=host)
            disks.create("scope", 1, quota_bytes=64 * MIB, fence=1)
            disks.reserve("scope", 1, generation=1, fence=2, action="reserve", reservation=RESERVATION)
            provisioner = VmProjectProvisioner(manager, disks, host=host)
            boot = disks.begin_boot("scope", 1, generation=1, fence=3, reservation=RESERVATION,
                                    spec=provisioner._spec())
            directory, identity = provisioner._directory(manager, boot, create=True)
            boot = disks.checkpoint_boot("scope", 1, generation=1, fence=3, reservation=RESERVATION,
                                         stage="preparing", directory_identity=identity)
            data = boot["binding"].path
            original_data = data.read_bytes()
            with patch("shutil.which", return_value="/fixture/qemu-system-x86_64"):
                artifacts = provisioner._prepare(manager, boot, "scope")
            boot = disks.checkpoint_boot("scope", 1, generation=1, fence=3, reservation=RESERVATION,
                                         stage="prepared", artifacts=artifacts)
            provisioner._verify_files(manager, boot)
            system = directory / "disk.qcow2"
            self.assertLess(system.stat().st_blocks * 512, 2 * MIB)
            host.run("qemu-io", "-f", "qcow2", "-c", "write -P 0x5a 1048576 4096", str(system))
            host.run("qemu-io", "-f", "qcow2", "-c", "read -P 0x5a 1048576 4096", str(system))
            provisioner._verify_files(manager, boot)
            self.assertEqual(data.read_bytes(), original_data)
            self.assertEqual(host.info(system)["virtual-size"], 4 * 1024**3)
            self.assertEqual(host.info(data)["virtual-size"], 64 * MIB)
