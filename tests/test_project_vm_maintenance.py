"""Real journal/file retirement with explicit libvirt and guest-action fixtures."""

import copy
import json
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.plugins.ai_chat.project_lifecycle import transition_plan
from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_vm_gate import LinuxGuestCommands, ProjectGuestGate
from src.sandbox_runtime.project_vm_guest import ProjectGuestWorkspace
from src.sandbox_runtime.project_vm_maintenance import VmProjectMaintenance
from tests.test_project_vm_guest import GuestFixture
from tests.test_project_vm_provision import RESERVATION, VmProvisionFixture


class MaintenanceTests(VmProvisionFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager._guest_exec_raw = AsyncMock(side_effect=self.guest_disk)
        self.maintenance = VmProjectMaintenance(self.manager, self.store, host=self.host, shutdown_timeout=0.05)
        self.mounted = True
        await self.boot()
        self.data_path = self.store._path(self.root / "data/p1", self.record())
        self.data_before = self.data_path.read_bytes()

    async def virsh(self, *args, **kwargs):
        if args[0] == "shutdown":
            self.assertEqual(args[2:], ("--mode", "agent"))
            self.domains[args[1]]["state"] = "shut off"
            self.done("shutdown")
            return "requested"
        if args[0] == "undefine":
            self.assertEqual(self.domains[args[1]]["state"], "shut off")
            del self.domains[args[1]]
            self.host.attached.clear()
            self.done("undefine")
            return "undefined"
        return await super().virsh(*args, **kwargs)

    async def guest_disk(self, sid, path, args, *, input_bytes, timeout):
        request = json.loads(input_bytes)
        action = request["action"]
        self.assertIn(action, {"trim", "unmount"})
        config = self.record()["workspace"]
        self.assertEqual(sid, config["sandbox_id"])
        metadata = json.loads((self.directory() / "metadata.json").read_text())
        values = metadata["project"]
        receipt = {k: values[k] for k in ("project_id", "generation", "filesystem_uuid")}
        receipt.update(epoch=request["epoch"], mode="quiesced", mounted=action == "trim", verified=True)
        if action == "trim":
            self.assertTrue(self.mounted)
            receipt.update(discard_completed=True, used_bytes=1024**2, quota_bytes=values["quota_bytes"], serial=values["serial"])
        else:
            self.mounted = False
        self.done(action)
        return 0, json.dumps(receipt).encode(), b""

    async def maintain(self, action, fence=4, **changes):
        return await self.maintenance.operate(action, "scope", 1, **{
            "generation": 1, "fence": fence, "reservation": RESERVATION, **changes})

    def reopen_maintenance(self):
        self.reopen()
        self.maintenance = VmProjectMaintenance(self.manager, self.store, host=self.host, shutdown_timeout=0.05)

    async def test_stop_preserves_system_and_data_then_resumes_same_vm(self):
        original = copy.deepcopy(self.record()["workspace"])
        system = self.directory() / "disk.qcow2"
        inode = system.stat().st_ino
        result = await self.maintain("stop")
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(result["pending"])
        self.assertEqual(self.data_path.read_bytes(), self.data_before)
        self.reopen_maintenance()
        self.assertEqual(await self.maintain("stop"), result)
        self.assertEqual(self.effects.count("shutdown"), 1)
        await self.boot(fence=5)
        self.assertEqual(self.record()["workspace"]["domain_uuid"], original["domain_uuid"])
        self.assertEqual(system.stat().st_ino, inode)
        self.assertEqual(self.effects.count("define"), 1)
        self.assertEqual(self.effects.count("start"), 2)

    async def test_trim_unmount_retire_and_reboot_preserve_data_without_reformat(self):
        original = copy.deepcopy(self.record()["workspace"])
        directory = self.directory()
        await self.maintain("trim")
        await self.maintain("unmount", fence=5)
        result = await self.maintain("remove", fence=6)
        self.assertEqual(result["state"], "absent")
        self.assertFalse(result["workspace_present"])
        self.assertFalse(directory.exists())
        self.assertEqual(self.data_path.read_bytes(), self.data_before)
        self.assertEqual(self.host.calls.count("create"), 1)
        self.reopen_maintenance()
        self.assertEqual(await self.maintain("remove", fence=6), result)
        self.assertEqual(self.effects.count("undefine"), 1)
        await self.boot(fence=7)
        self.assertNotEqual(self.record()["workspace"]["domain_uuid"], original["domain_uuid"])
        self.assertFalse(self.record()["workspace"]["initialize"])
        self.assertIsNone(self.record()["guest_unmounted"])
        self.assertEqual(self.host.calls.count("create"), 1)
        self.assertEqual(self.data_path.read_bytes(), self.data_before)

    async def test_remove_requires_verified_unmount(self):
        original = self.record()
        with self.assertRaisesRegex(SandboxError, "unmount"):
            await self.maintain("remove")
        self.assertEqual(self.record(), original)
        self.assertNotIn("shutdown", self.effects)

    async def test_lost_shutdown_receipt_does_not_issue_shutdown_twice(self):
        self.after["shutdown"] = RuntimeError("lost reply")
        with self.assertRaises(RuntimeError):
            await self.maintain("stop")
        self.assertEqual(self.record()["vm_maintenance"]["stage"], "shutdown_requested")
        self.reopen_maintenance()
        await self.maintain("stop")
        self.assertEqual(self.effects.count("shutdown"), 1)
        self.assertFalse(self.record()["pending"])

    async def test_crash_after_shutdown_intent_before_request_resumes(self):
        checkpoint = self.maintenance.journal.checkpoint
        def lost(*args, **kwargs):
            result = checkpoint(*args, **kwargs)
            if kwargs["stage"] == "shutdown_requested":
                raise OSError("process died before requesting shutdown")
            return result
        with patch.object(self.maintenance.journal, "checkpoint", side_effect=lost), self.assertRaises(OSError):
            await self.maintain("stop")
        self.assertNotIn("shutdown", self.effects)
        self.assertEqual(self.record()["vm_maintenance"]["stage"], "shutdown_requested")
        self.reopen_maintenance()
        await self.maintain("stop")
        self.assertEqual(self.effects.count("shutdown"), 1)
        self.assertFalse(self.record()["pending"])

    async def test_shutdown_timeout_never_forces_or_deletes(self):
        actual = self.virsh
        async def ignored(*args, **kwargs):
            if args[0] == "shutdown":
                self.effects.append("shutdown")
                return "requested"
            return await actual(*args, **kwargs)
        self.manager._virsh.side_effect = ignored
        for _ in range(2):
            with self.assertRaisesRegex(SandboxError, "unconfirmed"):
                await self.maintain("stop")
        self.assertEqual(self.effects.count("shutdown"), 2)
        self.assertTrue(self.record()["pending"])
        self.assertTrue((self.directory() / "disk.qcow2").exists())
        self.assertEqual(self.data_path.read_bytes(), self.data_before)
        for domain in self.domains.values():
            domain["state"] = "shut off"
        self.assertEqual((await self.maintain("stop"))["state"], "stopped")

    async def test_restarted_vm_does_not_reuse_stopped_receipt(self):
        await self.maintain("stop")
        self.domains[self.record()["workspace"]["domain_uuid"]]["state"] = "running"
        with self.assertRaisesRegex(SandboxError, "restarted"):
            await self.maintain("stop")
        self.assertEqual(self.effects.count("shutdown"), 1)

    async def test_lost_undefine_receipt_reconciles_absence(self):
        await self.maintain("unmount")
        self.after["undefine"] = RuntimeError("lost undefine reply")
        with self.assertRaises(RuntimeError):
            await self.maintain("remove", fence=5)
        self.assertEqual(self.record()["vm_maintenance"]["stage"], "undefining")
        self.reopen_maintenance()
        await self.maintain("remove", fence=5)
        self.assertEqual(self.effects.count("undefine"), 1)
        self.assertEqual(self.data_path.read_bytes(), self.data_before)

    async def test_partial_system_file_delete_retries_without_touching_data(self):
        await self.maintain("unmount")
        directory = self.directory()
        unlink = Path.unlink
        def lost(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path == directory / "seed.iso":
                raise OSError("lost deletion acknowledgement")
        with patch.object(Path, "unlink", lost), self.assertRaises(OSError):
            await self.maintain("remove", fence=5)
        self.assertEqual(self.record()["vm_maintenance"]["stage"], "deleting")
        self.reopen_maintenance()
        await self.maintain("remove", fence=5)
        self.assertFalse(directory.exists())
        self.assertEqual(self.data_path.read_bytes(), self.data_before)

    async def test_lost_final_journal_reply_keeps_idempotent_retirement(self):
        await self.maintain("unmount")
        save = self.store._save
        def lost(directory, record):
            save(directory, record)
            if not record["pending"] and record.get("workspace") is None:
                raise OSError("lost final reply")
        with patch.object(self.store, "_save", side_effect=lost), self.assertRaises(OSError):
            await self.maintain("remove", fence=5)
        self.reopen_maintenance()
        self.assertEqual((await self.maintain("remove", fence=5))["state"], "absent")
        self.assertEqual(self.effects.count("undefine"), 1)

    async def test_unknown_files_block_cleanup_without_recursive_delete(self):
        await self.maintain("unmount")
        path = self.directory() / "unrecognized.txt"
        path.write_text("retain me")
        with self.assertRaisesRegex(SandboxError, "Unknown"):
            await self.maintain("remove", fence=5)
        self.assertEqual(path.read_text(), "retain me")
        self.assertTrue((path.parent / "disk.qcow2").exists())
        self.assertEqual(self.data_path.read_bytes(), self.data_before)
        path.unlink()
        await self.maintain("remove", fence=5)

    async def test_replaced_bootstrap_files_or_domain_block_retirement(self):
        await self.maintain("unmount")
        path = self.directory() / "metadata.json"
        path.write_text("{}")
        with self.assertRaises(SandboxError):
            await self.maintain("remove", fence=5)
        self.assertNotIn("shutdown", self.effects)
        self.assertTrue(path.exists())

    async def test_foreign_system_disk_consumer_prevents_unlink(self):
        await self.maintain("unmount")
        directory = self.directory()
        consumers = self.host.consumers
        def shared(path):
            return {"other-service"} if path == directory / "disk.qcow2" else consumers(path)
        with patch.object(self.host, "consumers", side_effect=shared), self.assertRaisesRegex(SandboxError, "Another VM"):
            await self.maintain("remove", fence=5)
        self.assertTrue((directory / "disk.qcow2").exists())
        self.assertEqual(self.data_path.read_bytes(), self.data_before)

    async def test_invalid_guest_receipt_retains_pending_operation(self):
        async def bad(*args, **kwargs):
            return 0, b'{"verified":true,"mounted":false}', b""
        self.manager._guest_exec_raw.side_effect = bad
        with self.assertRaises(SandboxError):
            await self.maintain("unmount")
        self.assertTrue(self.record()["pending"])
        self.assertIsNone(self.record().get("guest_unmounted"))
        with self.assertRaises(SandboxError):
            await self.maintain("remove", fence=5)
        self.manager._guest_exec_raw.side_effect = self.guest_disk
        await self.maintain("unmount")

    async def test_actual_guest_receipts_and_lost_unmount_reply_reconcile(self):
        metadata = json.loads((self.directory() / "metadata.json").read_text())
        config = {**metadata["project"], "initialize": False}
        host = GuestFixture(config)
        workspace = ProjectGuestWorkspace({**config, "initialize": True}, host=host)
        workspace.mount()
        commands = Mock(spec=LinuxGuestCommands)
        commands.empty.return_value = True
        commands.sandbox_processes.return_value = False
        gate = ProjectGuestGate(config, workspace=workspace, commands=commands,
            state_path=self.root / "gate.json", lock_root=self.root)
        gate.quiesce(3)
        lose_unmount = True
        async def guest(*args, input_bytes, **kwargs):
            nonlocal lose_unmount
            request = json.loads(input_bytes)
            receipt = gate.disk_operation(request["action"], request["epoch"])
            if request["action"] == "unmount" and lose_unmount:
                lose_unmount = False
                raise SandboxError("QGA connection lost after unmount")
            return 0, json.dumps(receipt).encode(), b""
        self.manager._guest_exec_raw.side_effect = guest
        self.assertEqual((await self.maintain("trim"))["state"], "trimmed")
        with self.assertRaisesRegex(SandboxError, "connection lost"):
            await self.maintain("unmount", fence=5)
        self.assertEqual(host.rows, [])
        self.assertTrue(self.record()["pending"])
        self.reopen_maintenance()
        self.assertEqual((await self.maintain("unmount", fence=5))["state"], "unmounted")
        await self.maintain("remove", fence=6)
        self.assertEqual(host.calls.count("unmount"), 1)
        self.assertEqual(self.data_path.read_bytes(), self.data_before)

    async def test_socket_cleanup_checks_journalled_identity(self):
        await self.maintain("unmount")
        path = self.directory() / "agent.sock"
        with socket.socket(socket.AF_UNIX) as channel:
            channel.bind(str(path))
        checkpoint = self.maintenance.journal.checkpoint
        def lost(*args, **kwargs):
            result = checkpoint(*args, **kwargs)
            if kwargs["stage"] == "deleting":
                raise OSError("lost cleanup checkpoint response")
            return result
        with patch.object(self.maintenance.journal, "checkpoint", side_effect=lost), self.assertRaises(OSError):
            await self.maintain("remove", fence=5)
        path.rename(path.with_name("old.sock"))
        with socket.socket(socket.AF_UNIX) as channel:
            channel.bind(str(path))
        path.with_name("old.sock").unlink()
        with self.assertRaisesRegex(SandboxError, "socket identity"):
            await self.maintain("remove", fence=5)
        self.assertTrue(path.exists())
        self.assertEqual(self.data_path.read_bytes(), self.data_before)

    async def test_fifo_bootstrap_replacement_is_rejected_without_blocking(self):
        path = self.directory() / "seed.iso"
        path.unlink()
        os.mkfifo(path, mode=0o600)
        with self.assertRaisesRegex(SandboxError, "permissions"):
            await self.maintain("stop")
        self.assertNotIn("shutdown", self.effects)

    async def test_io_activity_and_wrong_reservations_prevent_effects(self):
        sid = self.record()["sandbox_id"]
        async with self.manager.io_lock(sid):
            with self.assertRaisesRegex(SandboxError, "active command"):
                await self.maintain("stop")
        self.assertNotIn("shutdown", self.effects)
        for parameters in ({"generation": 2}, {"fence": True}, {"fence": 2}, {"reservation": "a" * 32}):
            with self.assertRaises(SandboxError):
                await self.maintain("stop", **parameters)
        self.assertNotIn("shutdown", self.effects)

    async def test_recreated_retired_directory_is_not_removed_on_retry(self):
        await self.maintain("unmount")
        path = self.directory()
        await self.maintain("remove", fence=5)
        path.mkdir()
        (path / "do-not-delete").write_text("new identity")
        with self.assertRaises(SandboxError):
            await self.maintain("remove", fence=5)
        self.assertTrue((path / "do-not-delete").exists())


class VmPlanTests(unittest.TestCase):
    def plan(self, action, status, *, generation=1, baseline=None, rebuild=None):
        project = {"backend": "vm", "status": status, "workspace_generation": generation,
                   "pinned": False, "quota_bytes": 64 * 1024**2, "rebuild_command": "npm ci"}
        return transition_plan(project, action, baseline, rebuild=rebuild)

    def test_hibernate_finishes_guest_operations_before_retiring_vm(self):
        _, plan = self.plan("hibernate", "stopped")
        self.assertEqual(plan["protocol"], 3)
        self.assertEqual(plan["steps"], ["reserve_workspace", "boot_workspace", "snapshot_workspace", "prune_rebuildable",
                                        "trim_volume", "unmount_volume", "remove_workspace", "release_workspace", "verify_hibernated"])

    def test_archival_boots_for_snapshot_then_unmounts_before_removal(self):
        _, plan = self.plan("archive", "hibernated")
        self.assertEqual(plan["steps"], ["reserve_workspace", "boot_workspace", "snapshot_workspace", "unmount_volume",
                                        "remove_workspace", "delete_volume", "release_workspace", "verify_archived"])

    def test_wake_restores_before_dependencies_and_release(self):
        generation, plan = self.plan("wake", "archived", baseline={"version_no": 1}, rebuild={"command": "npm ci"})
        self.assertEqual(generation, 2)
        self.assertEqual(plan["steps"], ["create_volume", "reserve_workspace", "boot_workspace", "restore_version",
                                        "rebuild_dependencies", "release_workspace", "verify_active"])
        _, plan = self.plan("wake", "hibernated", baseline={"version_no": 1}, rebuild={"command": "npm ci"})
        self.assertNotIn("restore_version", plan["steps"])
        self.assertNotIn("create_volume", plan["steps"])
        self.assertLess(plan["steps"].index("boot_workspace"), plan["steps"].index("rebuild_dependencies"))

    def test_stop_saves_before_shutdown_without_removal(self):
        _, plan = self.plan("stop", "active")
        self.assertEqual(plan["steps"], ["reserve_workspace", "quiesce_workspace", "snapshot_workspace",
                                        "stop_workspace", "release_workspace", "verify_stopped"])


if __name__ == "__main__":
    unittest.main()
