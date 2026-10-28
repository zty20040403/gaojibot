"""Journalled VM retirement. Data disks are never removed by this adapter.

Private lifecycle callers must first enforce membership, active-task protection
and durable snapshot publication. Shutdown/undefine success is not evidence:
each effect is reconciled against the immutable domain and filesystem journal.
"""

import asyncio
import copy
import json
import os
import stat
import time

from .errors import SandboxError
from .project_vm_disks import VmProjectDiskBinding
from .project_vm_gate import HELPER_PATH
from .project_vm_browser_guest import SOCKET_NAME
from .project_vm_io import _journal
from .project_vm_mounts import VmProjectMounts
from .project_vm_provision import FILES, VmProjectProvisioner

ACTIONS = frozenset({"quiesce", "trim", "unmount", "stop", "remove"})
STAGES = {"prepared": {"shutdown_requested", "stopped"},
          "shutdown_requested": {"stopped"}, "stopped": {"undefining"},
          "undefining": {"undefined"}, "undefined": {"deleting"}, "deleting": {"deleted"}}
SOCKETS = {"agent.sock", SOCKET_NAME}


def _socket_identities(value):
    # The original retirement journal stored only the QGA socket pair.
    if isinstance(value, list):
        value = {"agent.sock": value}
    if value is None:
        return {}
    if (not isinstance(value, dict) or set(value) - SOCKETS
            or any(not isinstance(pair, list) or len(pair) != 2
                   or any(type(n) is not int or n < 0 for n in pair) for pair in value.values())):
        raise SandboxError("Invalid retired VM socket identity")
    return value


class VmMaintenanceJournal:
    def __init__(self, disks):
        self.disks = disks

    def _context(self, directory, record, generation, reservation, fence):
        d = self.disks
        d._generation(record, generation)
        d._require_reservation(record, reservation)
        if not record or not record.get("maintenance") or record.get("deleted"):
            raise SandboxError("VM maintenance requires a reserved project disk")
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise SandboxError("Invalid VM maintenance fence")
        d._status(directory, record)

    def _view(self, directory, record):
        state = record["vm_maintenance"]
        workspace = state["workspace"]
        binding = VmProjectDiskBinding(record["project_id"], record["generation"], workspace["sandbox_id"],
                                       self.disks._path(directory, record), record["fs_uuid"], record["quota_bytes"])
        return {"binding": binding, "workspace": copy.deepcopy(workspace), "maintenance": copy.deepcopy(state),
                "pending": record["pending"], "fence": record["fence"]}

    def begin(self, action, owner, project_id, *, generation, reservation, fence):
        if action not in ACTIONS:
            raise SandboxError("Unsupported VM maintenance action")
        d = self.disks
        with d._locked(owner, project_id) as (directory, record):
            self._context(directory, record, generation, reservation, fence)
            if record["fence"] == fence:
                intent = record["intent"]
                if intent != {"operation": "vm_maintenance", "arguments": {"action": action}}:
                    raise SandboxError("VM maintenance fence belongs to another operation")
                state = record.get("vm_maintenance")
                if not state or state["fence"] != fence or state["action"] != action:
                    raise SandboxError("VM maintenance journal is incomplete")
                return self._view(directory, record)
            if fence < record["fence"] or record["pending"]:
                raise SandboxError("Reconcile the current VM operation first")
            boot = d._boot_binding(directory, record)
            if boot["workspace"]["stage"] != "ready" or not record.get("guest_initialized"):
                raise SandboxError("VM maintenance cannot adopt an incomplete boot")
            if action == "remove":
                expected = {"sandbox_id": boot["binding"].sandbox_id,
                            "domain_uuid": boot["workspace"]["domain_uuid"], "filesystem_uuid": record["fs_uuid"]}
                if record.get("guest_unmounted") != expected:
                    raise SandboxError("VM retirement requires an acknowledged project unmount")
            record["vm_maintenance"] = {"action": action, "fence": fence, "stage": "prepared",
                                         "workspace": boot["workspace"], "socket_identity": None}
            d._begin(directory, record, "vm_maintenance", fence, {"action": action})
            return self._view(directory, record)

    def checkpoint(self, owner, project_id, *, generation, reservation, fence, stage, socket_identity=None):
        d = self.disks
        with d._locked(owner, project_id) as (directory, record):
            self._context(directory, record, generation, reservation, fence)
            state = record.get("vm_maintenance")
            if (not record["pending"] or record["fence"] != fence or not state or state["fence"] != fence
                    or stage not in STAGES.get(state["stage"], set())):
                raise SandboxError("VM maintenance checkpoint is stale or out of order")
            state["stage"] = stage
            if socket_identity is not None:
                if stage != "deleting":
                    raise SandboxError("Invalid retired VM socket identity")
                state["socket_identity"] = _socket_identities(socket_identity)
            d._save(directory, record)
            return self._view(directory, record)

    def finish(self, owner, project_id, *, generation, reservation, fence, evidence):
        d = self.disks
        with d._locked(owner, project_id) as (directory, record):
            self._context(directory, record, generation, reservation, fence)
            state = record.get("vm_maintenance")
            if (record["fence"] != fence or not state or state["fence"] != fence
                    or record["intent"] != {"operation": "vm_maintenance", "arguments": {"action": state["action"]}}):
                raise SandboxError("VM maintenance completion was superseded")
            if state["action"] == "remove":
                if state["stage"] != "deleted" or d.host.consumers(d._path(directory, record)):
                    raise SandboxError("Retired VM still has project disk consumers")
                record.update(workspace=None, sandbox_id=None, guest_domain_uuid=None, guest_unmounted=None)
            elif state["action"] == "stop" and state["stage"] != "stopped":
                raise SandboxError("VM shutdown was not verified")
            elif state["action"] == "unmount":
                record["guest_unmounted"] = {"sandbox_id": state["workspace"]["sandbox_id"],
                                             "domain_uuid": state["workspace"]["domain_uuid"],
                                             "filesystem_uuid": record["fs_uuid"]}
            state["evidence"] = dict(evidence)
            result = d._finish(directory, record)
            return {**result, **evidence, "verified": True}


class VmProjectMaintenance:
    def __init__(self, manager, disks, *, host=None, shutdown_timeout=60):
        self.manager, self.disks = manager, disks
        self.provisioner = VmProjectProvisioner(manager, disks, host=host)
        self.journal = VmMaintenanceJournal(disks)
        if not 0 < shutdown_timeout <= 300:
            raise ValueError("Invalid VM shutdown wait")
        self.shutdown_timeout = shutdown_timeout

    @staticmethod
    def _guest_receipt(action, output, binding, fence):
        try:
            if len(output) > 8192:
                raise ValueError()
            receipt = json.loads(output)
            expected = {k: binding.guest_config()[k] for k in ("project_id", "generation", "filesystem_uuid")}
            expected.update(epoch=fence, mode="quiesced", mounted=action != "unmount", verified=True)
            fields = set(expected) | ({"discard_completed", "used_bytes", "quota_bytes", "serial"} if action == "trim" else set())
            if not isinstance(receipt, dict) or set(receipt) != fields:
                raise ValueError()
            if any(type(receipt.get(k)) is not type(v) or receipt[k] != v for k, v in expected.items()):
                raise ValueError()
            if action == "trim" and (receipt.get("discard_completed") is not True
                    or type(receipt.get("used_bytes")) is not int or not 0 <= receipt["used_bytes"] <= binding.quota_bytes
                    or type(receipt.get("quota_bytes")) is not int or receipt["quota_bytes"] != binding.quota_bytes
                    or receipt.get("serial") != binding.guest_config()["serial"]):
                raise ValueError()
            return receipt
        except (ValueError, TypeError):
            raise SandboxError("Guest did not verify the requested disk maintenance") from None

    async def _disk(self, manager, boot, action, fence):
        code, output, _ = await manager._guest_exec_raw(boot["binding"].sandbox_id, "/usr/bin/python3", [HELPER_PATH],
            input_bytes=json.dumps({"action": action, "epoch": fence}).encode(), timeout=180)
        if code:
            raise SandboxError("Guest disk maintenance failed; preserve the reservation")
        return self._guest_receipt(action, output, boot["binding"], fence)

    async def _shutdown(self, manager, boot, checkpoint, fence):
        stage = boot["maintenance"]["stage"]
        state = await self.provisioner._observe(manager, boot)
        if state is None:
            raise SandboxError("Project VM disappeared before shutdown was verified")
        if stage in {"prepared", "shutdown_requested"}:
            if state == "running":
                await VmProjectMounts(manager, self.disks)._control(boot["binding"].sandbox_id, boot["binding"], "quiesce", fence)
                if boot["maintenance"]["action"] == "remove":
                    await self._disk(manager, boot, "unmount", fence)
                if stage == "prepared":
                    boot = await checkpoint("shutdown_requested")
                # A crash may precede the actual request. Reissue only while the
                # same domain is still running and its guest gate is closed.
                await manager._virsh("shutdown", boot["workspace"]["domain_uuid"], "--mode", "agent")
            else:
                return await checkpoint("stopped")
        elif state != "shut off":
            raise SandboxError("VM restarted after its shutdown was verified")
        deadline = time.monotonic() + self.shutdown_timeout
        while await self.provisioner._observe(manager, boot) != "shut off":
            if time.monotonic() >= deadline:
                raise SandboxError("VM shutdown is unconfirmed; no disk was removed")
            await asyncio.sleep(0.2)
        if boot["maintenance"]["stage"] == "shutdown_requested":
            boot = await checkpoint("stopped")
        return boot

    def _cleanup_files(self, manager, boot, *, deleting=False):
        directory = manager._directory(boot["binding"].sandbox_id)
        if not directory.exists() and not directory.is_symlink():
            if deleting:
                return None
            raise SandboxError("VM system directory disappeared before retirement")
        self.provisioner._directory(manager, boot)
        artifacts = boot["workspace"]["artifacts"]
        paths = list(directory.iterdir())
        names = {p.name for p in paths}
        if names - FILES - SOCKETS or (not deleting and not FILES <= names):
            raise SandboxError("Unknown or missing VM files prevent retirement")
        socket_identity = {}
        expected_sockets = _socket_identities(boot["maintenance"]["socket_identity"])
        for path in paths:
            if path.name in SOCKETS:
                info = path.lstat()
                socket_identity[path.name] = [info.st_dev, info.st_ino]
                if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                        or (deleting and socket_identity[path.name] != expected_sockets.get(path.name))):
                    raise SandboxError("Retired guest socket identity changed")
            else:
                observed = self.provisioner._file(path, digest=path.name != "disk.qcow2")
                expected = artifacts[path.name]
                if (observed["identity"][:2] != expected["identity"][:2]
                        or (path.name != "disk.qcow2" and observed != expected)):
                    raise SandboxError("Retired VM file identity changed")
            if path.name in {"disk.qcow2", "seed.iso"} and self.provisioner.host.consumers(path):
                raise SandboxError("Another VM still references the retired system files")
        if deleting:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for path in paths:
                    if path.name in SOCKETS:
                        info = path.lstat()
                        if not stat.S_ISSOCK(info.st_mode) or [info.st_dev, info.st_ino] != socket_identity[path.name]:
                            raise SandboxError("Guest socket changed before removal")
                    else:
                        observed = self.provisioner._file(path, digest=path.name != "disk.qcow2")
                        expected = artifacts[path.name]
                        if (observed["identity"][:2] != expected["identity"][:2]
                                or (path.name != "disk.qcow2" and observed != expected)):
                            raise SandboxError("VM file changed before removal")
                    path.unlink()
                    os.fsync(fd)
            finally:
                os.close(fd)
            directory.rmdir()
            parent = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        return socket_identity

    async def operate(self, action, owner, project_id, *, generation, fence, reservation):
        await _journal(self.disks.status, owner, project_id, generation=generation)
        self.disks._private_directory(self.manager.root.absolute())
        options = dict(generation=generation, fence=fence, reservation=reservation)
        async with self.manager.allocation_lock():
            # Journal lock precedes effects; all VM operations also share io_lock.
            status = await _journal(self.disks.status, owner, project_id, generation=generation)
            sid = status.get("sandbox_id")
            if sid is None:
                # Completed removal retries keep the original immutable identity.
                boot = await _journal(self.journal.begin, action, owner, project_id, **options)
                sid = boot["binding"].sandbox_id
            async with self.manager.io_lock(sid, timeout=0):
                boot = await _journal(self.journal.begin, action, owner, project_id, **options)
                manager = self.provisioner._manager(boot)
                async def checkpoint(stage, **extra):
                    return await _journal(self.journal.checkpoint, owner, project_id, **options, stage=stage, **extra)
                state = await self.provisioner._observe(manager, boot)
                stage = boot["maintenance"]["stage"]
                if action in {"quiesce", "trim", "unmount"}:
                    if state != "running":
                        raise SandboxError("Guest maintenance requires the verified running VM")
                    await _journal(self.provisioner._verify_files, manager, boot)
                    if action == "quiesce":
                        await VmProjectMounts(manager, self.disks)._control(sid, boot["binding"], "quiesce", fence)
                        evidence = {"state": "quiesced"}
                    else:
                        receipt = await self._disk(manager, boot, action, fence)
                        evidence = {"state": "unmounted" if action == "unmount" else "trimmed", "guest": receipt}
                    if await self.provisioner._observe(manager, boot) != "running":
                        raise SandboxError("VM changed during guest maintenance")
                else:
                    if stage in {"prepared", "shutdown_requested", "stopped"}:
                        await _journal(self.provisioner._verify_files, manager, boot)
                        boot = await self._shutdown(manager, boot, checkpoint, fence)
                        stage = boot["maintenance"]["stage"]
                    if action == "stop":
                        evidence = {"state": "stopped", "sandbox_id": sid}
                    else:
                        if stage == "stopped":
                            boot = await checkpoint("undefining")
                            stage = "undefining"
                        if stage == "undefining":
                            state = await self.provisioner._observe(manager, boot)
                            if state == "running":
                                raise SandboxError("VM restarted during retirement")
                            if state is not None:
                                await manager._virsh("undefine", boot["workspace"]["domain_uuid"])
                            if await self.provisioner._observe(manager, boot) is not None:
                                raise SandboxError("VM definition is still present")
                            boot = await checkpoint("undefined")
                            stage = "undefined"
                        if await self.provisioner._observe(manager, boot) is not None:
                            raise SandboxError("VM reappeared before system file cleanup")
                        if stage == "undefined":
                            socket_identity = await _journal(self._cleanup_files, manager, boot)
                            boot = await checkpoint("deleting", socket_identity=socket_identity)
                            stage = "deleting"
                        if stage == "deleting":
                            await _journal(self._cleanup_files, manager, boot, deleting=True)
                            boot = await checkpoint("deleted")
                            stage = "deleted"
                        directory = manager._directory(sid)
                        if stage != "deleted" or directory.exists() or directory.is_symlink():
                            raise SandboxError("Retired VM system files remain or were replaced")
                        evidence = {"state": "absent", "sandbox_id": None}
                return await _journal(self.journal.finish, owner, project_id, **options, evidence=evidence)
