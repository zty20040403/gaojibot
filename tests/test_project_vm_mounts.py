"""Host/guest acknowledgement recovery with explicit libvirt/guest fixtures."""

import asyncio
import copy
import json
import os
from pathlib import Path
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
from src.sandbox_runtime.project_vm_disks import VmProjectDisks
from src.sandbox_runtime.project_vm_mounts import VmProjectMounts, _journal
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumeError
from tests.test_project_vm_disks import DiskFixture, INSTANCE

DOMAIN = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
RESERVATION = "b" * 32
SID = "s123456"


class GuestJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-guest-journal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "disks"
        self.host = DiskFixture()
        self.store = self.reopen()
        self.store.create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.store.reserve("scope", 1, generation=1, fence=2, action="reserve", reservation=RESERVATION)
        self.binding = self.store.claim("scope", 1, SID, fence=3, reservation=RESERVATION)

    def reopen(self):
        return VmProjectDisks(self.root, INSTANCE, host=self.host)

    def record(self):
        return read_json(self.root / "p1/volume.json")

    def prepare(self, **changes):
        return self.store.prepare_guest("scope", 1, SID, DOMAIN, **{
            "generation": 1, "fence": 4, "reservation": RESERVATION, **changes,
        })

    def receipt(self):
        config = self.binding.guest_config()
        config.pop("initialize")
        return {**config, "mounted": True, "verified": True,
                "initialization_sealed": True, "used_bytes": MIB}

    def acknowledge(self, receipt=None, **changes):
        return self.store.acknowledge_guest("scope", 1, SID, DOMAIN,
            receipt if receipt is not None else self.receipt(), **{
                "generation": 1, "fence": 5, "reservation": RESERVATION, **changes,
            })

    def test_prepare_is_durable_and_does_not_grant_a_second_domain(self):
        first = self.prepare()
        self.assertTrue(first["initialize"])
        self.store = self.reopen()
        self.assertEqual(self.prepare(), first)
        self.store.release("scope", 1, SID, fence=5, reservation=RESERVATION)
        self.store.claim("scope", 1, "s654321", fence=6, reservation=RESERVATION)
        second = self.store.prepare_guest("scope", 1, "s654321", OTHER,
            generation=1, fence=7, reservation=RESERVATION)
        self.assertFalse(second["initialize"])
        self.assertEqual(second["binding"].filesystem_uuid, first["binding"].filesystem_uuid)
        self.assertEqual(self.host.calls.count("create"), 1)

    def test_prepare_crash_before_and_after_grant_is_reconciled(self):
        save = self.store._save
        for after_grant in (False, True):
            with self.subTest(after_grant=after_grant):
                if after_grant:
                    # Re-run on a separate project to inject the other boundary.
                    self.store.create("scope", 2, quota_bytes=64 * MIB, fence=1)
                    self.store.reserve("scope", 2, generation=1, fence=2, action="reserve", reservation=RESERVATION)
                    self.store.claim("scope", 2, SID, fence=3, reservation=RESERVATION)
                pid = 2 if after_grant else 1
                def crash(directory, record):
                    save(directory, record)
                    if record["intent"]["operation"] == "guest_prepare" and bool(record.get("guest_domain_uuid")) == after_grant:
                        raise RuntimeError("lost acknowledgement")
                with patch.object(self.store, "_save", side_effect=crash), self.assertRaises(RuntimeError):
                    self.store.prepare_guest("scope", pid, SID, DOMAIN, generation=1, fence=4, reservation=RESERVATION)
                result = self.reopen().prepare_guest("scope", pid, SID, DOMAIN, generation=1, fence=4, reservation=RESERVATION)
                self.assertTrue(result["initialize"])
                self.assertEqual(read_json(self.root / f"p{pid}/volume.json")["guest_domain_uuid"], DOMAIN)

    def test_acknowledgement_requires_current_consumer_and_sealed_mount(self):
        self.prepare()
        with self.assertRaises(ProjectVolumeError):
            self.acknowledge()
        self.host.attached = {"gaoji-vm-" + SID}
        for change in ({"initialization_sealed": False}, {"mounted": False}, {"generation": True},
                       {"filesystem_uuid": OTHER}, {"used_bytes": -1}, {"used_bytes": True}):
            with self.subTest(change=change), self.assertRaises(ProjectVolumeError):
                self.acknowledge({**self.receipt(), **change})
        self.assertFalse(self.record()["guest_initialized"])
        result = self.acknowledge()
        self.assertTrue(result["guest_initialized"])
        self.assertEqual(result, self.acknowledge())
        self.host.attached.clear()
        self.store.release("scope", 1, SID, fence=6, reservation=RESERVATION)
        self.store.claim("scope", 1, "s654321", fence=7, reservation=RESERVATION)
        result = self.reopen().prepare_guest("scope", 1, "s654321", OTHER,
            generation=1, fence=8, reservation=RESERVATION)
        self.assertFalse(result["initialize"])

    def test_reservation_and_unacknowledged_guest_block_normal_io(self):
        self.prepare()
        for options in ({}, {"reservation": "c" * 32}, {"reservation": RESERVATION}):
            with self.subTest(options=options), self.assertRaises(ProjectVolumeError):
                self.store.guest_binding("scope", 1, SID, DOMAIN, generation=1, **options)
        self.host.attached = {"gaoji-vm-" + SID}
        self.acknowledge()
        with self.assertRaises(ProjectVolumeError):
            self.store.guest_binding("scope", 1, SID, DOMAIN, generation=1)
        self.store.reserve("scope", 1, generation=1, fence=6, action="release", reservation=RESERVATION)
        self.assertEqual(self.reopen().guest_binding("scope", 1, SID, DOMAIN, generation=1), self.binding)
        for changes in ({"domain": OTHER}, {"owner": "other"}, {"generation": 2}):
            with self.subTest(changes=changes), self.assertRaises(ProjectVolumeError):
                self.store.guest_binding(changes.get("owner", "scope"), 1, SID,
                    changes.get("domain", DOMAIN), generation=changes.get("generation", 1))

    def test_cannot_adopt_preexisting_domain_or_mutate_during_maintenance(self):
        self.host.attached = {"gaoji-vm-" + SID}
        with self.assertRaises(ProjectVolumeError):
            self.prepare()
        self.host.attached.clear()
        for call in (
            lambda: self.store.release("scope", 1, SID, fence=4),
            lambda: self.store.claim("scope", 1, SID, fence=4),
            lambda: self.store.delete("scope", 1, fence=4),
            lambda: self.store.grow("scope", 1, fence=4, quota_bytes=96 * MIB),
            lambda: self.prepare(reservation=None),
            lambda: self.prepare(fence=True),
        ):
            with self.assertRaises(ProjectVolumeError):
                call()
        self.assertEqual(self.record()["fence"], 3)

    def test_bootstrap_identity_cannot_be_reused_for_another_sandbox(self):
        self.prepare()
        self.store.release("scope", 1, SID, fence=5, reservation=RESERVATION)
        self.store.claim("scope", 1, "s654321", fence=6, reservation=RESERVATION)
        grant = self.store.prepare_guest("scope", 1, "s654321", DOMAIN,
            generation=1, fence=7, reservation=RESERVATION)
        self.assertFalse(grant["initialize"])

    def test_stale_or_conflicting_fences_do_not_change_guest_identity(self):
        self.prepare()
        for domain, fence in ((OTHER, 4), (OTHER, 5), (DOMAIN, 3)):
            with self.subTest(domain=domain, fence=fence), self.assertRaises(ProjectVolumeError):
                self.store.prepare_guest("scope", 1, SID, domain,
                    generation=1, fence=fence, reservation=RESERVATION)
        self.assertEqual(self.record()["guest_domain_uuid"], DOMAIN)
        self.assertEqual(self.record()["fence"], 4)


class MountBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = GuestJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.host = self.fixture.store, self.fixture.host
        grant = self.fixture.prepare()
        self.manager = VmSandboxManager(image="/base.qcow2", root=str(self.fixture.root.parent / "vm"))
        self.bridge = VmProjectMounts(self.manager, self.store)
        self.manager.project_mounts = self.bridge
        config = grant["binding"].guest_config()
        config.pop("initialize")
        self.metadata = {"purpose": "project", "project": config, "project_domain_uuid": DOMAIN}
        self.manager._metadata = AsyncMock(return_value=self.metadata)
        self.manager._running = AsyncMock(return_value=True)
        self.manager._guest_exec = AsyncMock(side_effect=self.guest)
        with patch("shutil.which", return_value="/test/qemu"):
            self.xml = self.manager._domain_xml(SID, self.manager._directory(SID),
                project_disk=grant["binding"], project_domain_uuid=DOMAIN)
        self.manager._virsh = AsyncMock(side_effect=self.virsh)
        self.host.attached = {"gaoji-vm-" + SID}

    async def guest(self, sandbox_id, path, args, **kwargs):
        if args[-1].endswith("project_gate.py"):
            request = json.loads(kwargs["input_bytes"])
            receipt = {key: self.metadata["project"][key]
                       for key in ("project_id", "generation", "filesystem_uuid", "serial")}
            receipt.update(epoch=request["epoch"], verified=True,
                           mode="active" if request["action"] == "activate" else "quiesced")
            return 0, json.dumps(receipt).encode(), b""
        return 0, json.dumps(self.fixture.receipt()).encode(), b""

    async def virsh(self, *args, **kwargs):
        if args[0] == "list":
            return DOMAIN
        if args[0] == "dumpxml":
            return self.xml
        if args[0] == "domstate":
            return "running"
        raise AssertionError(args)

    async def mount(self):
        return await self.bridge.mount("scope", SID, self.metadata, reservation=RESERVATION, fence=5)

    async def ready(self):
        await self.mount()
        await self.bridge.reserve("release", "scope", 1, generation=1, fence=6, reservation=RESERVATION)

    async def test_mount_acknowledges_host_then_normal_admission_rechecks(self):
        result = await self.mount()
        self.assertTrue(result["guest_initialized"])
        with self.assertRaises(SandboxError):
            await self.manager._owned_running("scope", SID)
        await self.bridge.reserve("release", "scope", 1, generation=1, fence=6, reservation=RESERVATION)
        self.assertEqual(await self.manager._owned_running("scope", SID), self.metadata)
        self.host.attached.add("foreign")
        before = self.manager._guest_exec.await_count
        with self.assertRaises(SandboxError):
            await self.manager._owned_running("scope", SID)
        self.assertEqual(self.manager._guest_exec.await_count, before)

    async def test_lost_guest_reply_and_host_ack_both_retry_without_new_grant(self):
        self.manager._guest_exec.side_effect = SandboxError("lost guest reply")
        with self.assertRaises(SandboxError):
            await self.mount()
        self.assertFalse(self.fixture.record()["guest_initialized"])
        self.manager._guest_exec.side_effect = self.guest
        save = self.store._save
        def crash(directory, record):
            save(directory, record)
            if record["pending"] and record["intent"]["operation"] == "guest_acknowledge":
                raise RuntimeError("lost host acknowledgement")
        with patch.object(self.store, "_save", side_effect=crash), self.assertRaises(RuntimeError):
            await self.mount()
        self.bridge.disks = self.fixture.reopen()
        self.assertTrue((await self.mount())["guest_initialized"])
        self.assertEqual(self.host.calls.count("create"), 1)

    async def test_live_or_inactive_domain_mismatch_never_calls_guest(self):
        original = ElementTree.fromstring(self.xml)
        for change in ("uuid", "source", "discard", "serial", "generation", "network", "hostfs", "channel", "memory", "vcpus"):
            root = copy.deepcopy(original)
            data = root.findall("./devices/disk")[2]
            if change == "uuid":
                root.find("uuid").text = OTHER
            elif change == "source":
                data.find("source").set("file", "/other.qcow2")
            elif change == "discard":
                data.find("driver").set("discard", "ignore")
            elif change == "serial":
                data.find("serial").text = "foreign"
            elif change == "generation":
                root.find("./metadata/{urn:gaoji:project:1}workspace").set("generation", "2")
            elif change == "network":
                root.find("./devices/interface").set("type", "bridge")
            elif change == "hostfs":
                ElementTree.SubElement(root.find("devices"), "filesystem")
            elif change == "memory":
                root.find("memory").text = "32768"
            elif change == "vcpus":
                root.find("vcpu").text = "32"
            else:
                root.find("./devices/channel/source").set("path", "/other.sock")
            bad = ElementTree.tostring(root, encoding="unicode")
            async def observed(*args, **kwargs):
                return bad if args[0] == "dumpxml" and "--inactive" in args else await self.virsh(*args, **kwargs)
            self.manager._virsh.side_effect = observed
            with self.subTest(change=change), self.assertRaises(SandboxError):
                await self.mount()
        self.manager._guest_exec.assert_not_awaited()

    async def test_inventory_failure_is_not_treated_as_absence_or_success(self):
        for result in ("", "not-a-uuid", OTHER, DOMAIN + "\n" + DOMAIN):
            self.manager._virsh.side_effect = None
            self.manager._virsh.return_value = result
            with self.subTest(result=result), self.assertRaises(SandboxError):
                await self.mount()
        self.manager._guest_exec.assert_not_awaited()

    async def test_file_transfer_holds_same_lock_and_blocks_maintenance(self):
        await self.ready()
        entered, release = asyncio.Event(), asyncio.Event()
        async def read(*args):
            entered.set()
            await release.wait()
            return b"retained"
        self.manager._read_guest_file = AsyncMock(side_effect=read)
        reader = asyncio.create_task(self.manager.read_file("scope", SID, "result.txt"))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with self.assertRaisesRegex(SandboxError, "file transfer"):
                await self.bridge.reserve("reserve", "scope", 1, generation=1, fence=7, reservation="c" * 32)
            self.assertEqual(self.fixture.record()["fence"], 6)
        finally:
            release.set()
            self.assertEqual(await reader, b"retained")

    async def test_file_export_does_not_reenter_lock(self):
        self.manager._owned_running = AsyncMock(return_value={})
        self.manager._guest_exec.side_effect = None
        self.manager._guest_exec.return_value = (0, b"file", b"")
        self.manager._read_guest_file = AsyncMock(return_value=b"pdf")
        self.assertEqual(await asyncio.wait_for(self.manager.export_artifact("scope", SID, "a.pdf"), 2), (b"pdf", False))

    async def test_write_and_readonly_import_hold_the_same_io_lock(self):
        calls = []
        async def write(*args, **kwargs):
            calls.append(self.manager._exec_locks[SID].locked())
            return len(args[3])
        self.manager._write_content_locked = AsyncMock(side_effect=write)
        self.assertEqual(await self.manager.write_file("scope", SID, "a", b"text"), 4)
        await self.manager.install_readonly_file("scope", SID, "upstream/a", b"text")
        self.assertEqual(calls, [True, True])
        self.assertFalse(self.manager._exec_locks[SID].locked())

    async def test_invalid_or_unsealed_receipt_does_not_acknowledge(self):
        self.manager._guest_exec.side_effect = None
        for receipt in (b"not-json", b"null", b"{}", json.dumps({**self.fixture.receipt(), "initialization_sealed": False}).encode()):
            self.manager._guest_exec.return_value = (0, receipt, b"")
            with self.subTest(receipt=receipt), self.assertRaises(SandboxError):
                await self.mount()
            self.assertFalse(self.fixture.record()["guest_initialized"])

    async def test_domain_replacement_during_mount_does_not_acknowledge(self):
        async def replaced(*args, **kwargs):
            self.xml = self.xml.replace(DOMAIN, OTHER)
            return 0, json.dumps(self.fixture.receipt()).encode(), b""
        self.manager._guest_exec.side_effect = replaced
        with self.assertRaises(SandboxError):
            await self.mount()
        self.assertFalse(self.fixture.record()["guest_initialized"])

    async def test_reservation_created_during_admission_blocks_user_work(self):
        await self.ready()
        async def reserved(*args, **kwargs):
            self.store.reserve("scope", 1, generation=1, fence=7, action="reserve", reservation="c" * 32)
            return 0, json.dumps(self.fixture.receipt()).encode(), b""
        self.manager._guest_exec.side_effect = reserved
        with self.assertRaises(SandboxError):
            await self.manager.exec("scope", SID, "should-not-run")
        self.assertEqual(self.manager._active_execs, {})

    async def test_cancelled_journal_finishes_before_releasing_caller(self):
        entered, release = threading.Event(), threading.Event()
        saved = []
        def write():
            entered.set()
            release.wait(3)
            saved.append(True)
        operation = asyncio.create_task(_journal(write))
        await asyncio.to_thread(entered.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        self.assertFalse(operation.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(saved, [True])
