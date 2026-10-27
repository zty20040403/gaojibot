"""Private VM mount bridge; no model tools or production registration.

The durable lifecycle owns its reservation and fences. Provisioning, writer
quiescence and version IO are separate adapters and must pass native acceptance
before this bridge can be composed into an enabled project runtime.
"""

import asyncio
import json
import uuid
from xml.etree import ElementTree

from .docker import SandboxError
from .project_vm_guest import HELPER_PATH, validate_config


async def _journal(method, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        worker.result()
        raise


class VmProjectMounts:
    def __init__(self, manager, disks):
        self.manager, self.disks = manager, disks

    async def reserve(self, action, owner, project_id, *, generation, fence,
                      reservation, sandbox_id=None):
        async with self.manager._create_lock:
            status = await _journal(self.disks.status, owner, project_id, generation=generation)
            actual = status.get("sandbox_id")
            if sandbox_id is not None and sandbox_id != actual:
                raise SandboxError("Project VM identity changed before maintenance")
            lock = self.manager._exec_locks.setdefault(actual, asyncio.Lock()) if actual else None
            if lock is not None and lock.locked():
                raise SandboxError("Project workspace has an active command or file transfer")
            if actual and any(item.sandbox_id == actual for item in self.manager._active_execs.values()):
                raise SandboxError("Project workspace has an active command")
            # A durable reservation blocks subsequent admission across restart.
            if lock is None:
                return await _journal(self.disks.reserve, owner, project_id, generation=generation,
                                      fence=fence, action=action, reservation=reservation)
            async with lock:
                return await _journal(self.disks.reserve, owner, project_id, generation=generation,
                                      fence=fence, action=action, reservation=reservation)

    async def _domain(self, binding, domain_uuid):
        identifiers = (await self.manager._virsh("list", "--all", "--uuid")).split()
        try:
            if (len(identifiers) > 256 or len(set(identifiers)) != len(identifiers)
                    or any(str(uuid.UUID(item)) != item for item in identifiers)
                    or domain_uuid not in identifiers):
                raise ValueError()
            for options in ((), ("--inactive",)):
                xml = await self.manager._virsh("dumpxml", domain_uuid, *options)
                self._validate_domain(ElementTree.fromstring(xml), binding, domain_uuid)
            state = await self.manager._virsh("domstate", domain_uuid)
            if state.strip() != "running":
                raise ValueError()
        except (ValueError, TypeError, AttributeError, ElementTree.ParseError):
            raise SandboxError("Live and persistent VM definitions did not verify the project disk") from None

    def _validate_domain(self, root, binding, domain_uuid):
        config = binding.guest_config()
        config.pop("initialize")
        metadata = root.findall("./metadata/{urn:gaoji:project:1}workspace")
        if (root.tag != "domain" or root.get("type") != "kvm"
                or root.findtext("uuid") != domain_uuid
                or root.findtext("name") != self.manager._name(binding.sandbox_id)
                or len(metadata) != 1 or metadata[0].attrib != {key: str(value) for key, value in config.items()}):
            raise ValueError("Domain identity mismatch")
        memory = root.find("memory")
        scale = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "bytes": 1}
        if (memory is None or memory.get("unit", "KiB") not in scale
                or int(memory.text) * scale[memory.get("unit", "KiB")] != self.manager.memory_mib * 1024**2
                or int(root.findtext("vcpu")) != self.manager.vcpus
                or root.find("./os/type").get("arch") != "x86_64"
                or root.findtext("./os/type") != "hvm"):
            raise ValueError("Project VM resource limits or guest architecture changed")
        devices = root.find("devices")
        if devices is None or any(devices.findall(tag) for tag in ("filesystem", "hostdev", "redirdev")):
            raise ValueError("Unexpected host device sharing")
        if any(item.tag.startswith("{") for item in root if item.tag != "metadata"):
            raise ValueError("Unexpected domain extension")
        directory = self.manager._directory(binding.sandbox_id).absolute()
        expected = {
            "vda": (directory / "disk.qcow2", "disk", "virtio", "qcow2"),
            "sda": (directory / "seed.iso", "cdrom", "sata", "raw"),
            "vdb": (binding.path, "disk", "virtio", "qcow2"),
        }
        disks = devices.findall("disk")
        if len(disks) != 3:
            raise ValueError("Unexpected domain disks")
        for disk in disks:
            target, driver, source = disk.find("target"), disk.find("driver"), disk.find("source")
            if target is None or driver is None or source is None:
                raise ValueError("Incomplete domain disk")
            name = target.get("dev")
            if name not in expected:
                raise ValueError("Duplicate or unknown domain disk")
            path, device, bus, fmt = expected.pop(name)
            if (disk.get("type") != "file" or disk.get("device") != device
                    or source.get("file") != str(path) or target.get("bus") != bus
                    or driver.get("name") != "qemu" or driver.get("type") != fmt):
                raise ValueError("Domain disk mismatch")
            if name == "vdb" and (driver.get("discard") != "unmap"
                    or disk.findtext("serial") != config["serial"] or disk.find("readonly") is not None
                    or any(node.find("source") is not None for node in disk.iter("backingStore"))):
                raise ValueError("Invalid project data attachment")
        networks = devices.findall("interface")
        if (len(networks) != 1 or networks[0].get("type") != "user"
                or networks[0].find("portForward") is not None):
            raise ValueError("Unexpected project network")
        channels = devices.findall("channel")
        if (len(channels) != 1 or channels[0].get("type") != "unix"
                or channels[0].find("source").get("path") != str(directory / "agent.sock")
                or channels[0].find("target").get("name") != "org.qemu.guest_agent.0"):
            raise ValueError("Guest agent channel differs from the owned VM")

    async def _binding(self, owner, sandbox_id, metadata, *, reservation=None,
                       require_initialized=True, acknowledge_fence=None):
        try:
            config = validate_config({**metadata["project"], "initialize": False})
            domain_uuid = metadata["project_domain_uuid"]
            if metadata.get("purpose") != "project":
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise SandboxError("Project VM metadata is incomplete") from None
        binding = await _journal(
            self.disks.guest_binding, owner, config["project_id"], sandbox_id, domain_uuid,
            generation=config["generation"], reservation=reservation, require_initialized=require_initialized,
            acknowledge_fence=acknowledge_fence,
        )
        if binding.guest_config() != config:
            raise SandboxError("Project VM metadata differs from the host disk claim")
        return binding, domain_uuid

    async def mount(self, owner, sandbox_id, metadata, *, reservation, fence):
        async with self.manager._exec_locks.setdefault(sandbox_id, asyncio.Lock()):
            binding, domain_uuid = await self._binding(
                owner, sandbox_id, metadata, reservation=reservation, require_initialized=False,
                acknowledge_fence=fence,
            )
            await self._domain(binding, domain_uuid)
            code, out, _ = await self.manager._guest_exec(
                sandbox_id, "/usr/bin/timeout",
                ["--kill-after=5s", "300s", "/usr/bin/python3", HELPER_PATH, "mount"], timeout=310,
            )
            if code:
                raise SandboxError("Guest project mount failed; initialization remains unacknowledged")
            try:
                receipt = json.loads(out)
            except ValueError:
                raise SandboxError("Guest mount returned an invalid receipt") from None
            # Do not acknowledge stale receipts after a concurrent domain replacement.
            await self._domain(binding, domain_uuid)
            return await _journal(
                self.disks.acknowledge_guest, owner, binding.project_id, sandbox_id, domain_uuid, receipt,
                generation=binding.generation, fence=fence, reservation=reservation,
            )

    async def authorize(self, owner, sandbox_id, metadata):
        """Caller holds the shared IO lock; membership is checked by the executor."""
        binding, domain_uuid = await self._binding(owner, sandbox_id, metadata)
        await self._domain(binding, domain_uuid)
        observed = await self.manager._verify_project_mount(sandbox_id, metadata)
        after, after_uuid = await self._binding(owner, sandbox_id, metadata)
        if after != binding or after_uuid != domain_uuid:
            raise SandboxError("Project VM claim changed during admission")
        return observed
