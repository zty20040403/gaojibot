"""Resumable private VM provisioning; production composition is deliberately opt-in.

The database lifecycle holds a durable reservation and one effect fence for this
boot. Every external mutation is preceded by a journalled stage. No task cleanup
or retry path removes a system disk which might already have executed guest code.
"""

import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
from xml.etree import ElementTree

from src.local_runtime.files import file_lock, write_json
from .docker import SandboxError
from .project_vm_disks import QcowProjectHost
from .project_vm_guest import HELPER_PATH
from .project_vm_mounts import VmProjectMounts, _journal

FILES = frozenset({"disk.qcow2", "seed.iso", "user-data", "meta-data", "domain.xml", "metadata.json"})


@asynccontextmanager
async def _host_lock(path):
    context = file_lock(path, timeout=10)
    entering = asyncio.create_task(asyncio.to_thread(context.__enter__))
    try:
        await asyncio.shield(entering)
    except asyncio.CancelledError:
        while not entering.done():
            try:
                await asyncio.shield(entering)
            except asyncio.CancelledError:
                continue
        # If acquisition failed there is no descriptor owned by this context.
        if entering.exception() is None:
            await _journal(context.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await _journal(context.__exit__, None, None, None)


class VmProjectProvisioner:
    def __init__(self, manager, disks, *, host=None):
        if not manager.root.is_absolute():
            raise ValueError("VM project root must be absolute")
        self.manager, self.disks = manager, disks
        self.host = host or QcowProjectHost()

    @staticmethod
    def _identity(info):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    @classmethod
    def _file(cls, path, *, private=True, digest=True):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or (private and info.st_nlink != 1)
                    or info.st_uid not in ({os.geteuid()} if private else {0, os.geteuid()})
                    or info.st_mode & (0o077 if private else 0o022)):
                raise SandboxError("VM file ownership or permissions are unsafe")
            value = hashlib.sha256()
            if digest:
                while block := os.read(fd, 1024**2):
                    value.update(block)
            if cls._identity(os.fstat(fd)) != cls._identity(info):
                raise SandboxError("VM file changed while being verified")
            return {"identity": cls._identity(info), "sha256": value.hexdigest() if digest else None}
        finally:
            os.close(fd)

    def _image_info(self, path, *, shared=False):
        metadata = self.host.info(path, shared=shared)
        specific = metadata.get("format-specific") if isinstance(metadata, dict) else None
        details = specific.get("data") if isinstance(specific, dict) else None
        if (not isinstance(details, dict) or specific.get("type") != "qcow2"
                or metadata.get("format") != "qcow2" or metadata.get("encrypted") or metadata.get("snapshots")
                or any(key in details for key in ("data-file", "external-data-file", "encrypt"))
                or details.get("corrupt")
                or type(metadata.get("virtual-size")) is not int
                or metadata["virtual-size"] <= 0):
            raise SandboxError("VM image has invalid or unsupported QEMU metadata")
        return metadata

    def _spec(self):
        image = self.manager.image.resolve(strict=True)
        pinned = self._file(image, private=False)
        metadata = self._image_info(image)
        if (metadata.get("backing-filename") or metadata.get("full-backing-filename")
                or metadata["virtual-size"] > self.manager.disk_gib * 1024**3):
            raise SandboxError("Project VM needs a standalone base image within its system disk quota")
        return self.disks._boot_spec({
            "image": str(image), "image_identity": pinned["identity"], "image_sha256": pinned["sha256"],
            **{key: getattr(self.manager, key) for key in ("memory_mib", "vcpus", "pids_limit", "disk_gib")},
        })

    def _manager(self, boot):
        manager = copy.copy(self.manager)
        spec = boot["workspace"]["spec"]
        manager.image = Path(spec["image"])
        for key in ("memory_mib", "vcpus", "pids_limit", "disk_gib"):
            setattr(manager, key, spec[key])
        return manager

    async def _inventory(self, manager):
        raw = (await manager._virsh("list", "--all", "--uuid")).split()
        import uuid
        try:
            if len(raw) > 256 or len(set(raw)) != len(raw) or any(str(uuid.UUID(item)) != item for item in raw):
                raise ValueError()
            records = {}
            names = set()
            for identifier in raw:
                root = ElementTree.fromstring(await manager._virsh("dumpxml", identifier, "--inactive"))
                name = root.findtext("name")
                if root.tag != "domain" or root.findtext("uuid") != identifier or not name or name in names:
                    raise ValueError()
                names.add(name)
                state = (await manager._virsh("domstate", identifier)).strip()
                if state not in {"running", "shut off"}:
                    raise SandboxError("A VM has an ambiguous state; provisioning admission is paused")
                records[identifier] = (root, state)
            return records
        except (ValueError, TypeError, ElementTree.ParseError):
            raise SandboxError("Cannot verify the complete libvirt VM inventory") from None

    async def _observe(self, manager, boot):
        records = await self._inventory(manager)
        workspace, binding = boot["workspace"], boot["binding"]
        expected = workspace["domain_uuid"]
        name = manager._name(binding.sandbox_id)
        if any(root.findtext("name") == name and identifier != expected
               for identifier, (root, _) in records.items()):
            raise SandboxError("Project VM name belongs to another immutable domain")
        if expected not in records:
            return None
        root, state = records[expected]
        bridge = VmProjectMounts(manager, self.disks)
        try:
            bridge._validate_domain(root, binding, expected)
            if state == "running":
                bridge._validate_domain(ElementTree.fromstring(await manager._virsh("dumpxml", expected)), binding, expected)
        except (ValueError, TypeError, AttributeError, ElementTree.ParseError):
            raise SandboxError("Project VM definition differs from its frozen boot contract") from None
        return state

    async def _capacity(self, manager, boot, owner):
        records = await self._inventory(manager)
        running = [(identifier, root) for identifier, (root, state) in records.items() if state == "running"]
        target = boot["workspace"]["domain_uuid"]
        if any(identifier == target for identifier, _ in running):
            return
        if len(running) >= manager.max_total:
            raise SandboxError("VM sandbox capacity has been reached")
        owned = 0
        for _, root in running:
            name = root.findtext("name", "")
            if not name.startswith("gaoji-vm-"):
                continue
            identifier = name.removeprefix("gaoji-vm-")
            try:
                data = json.loads((manager._directory(identifier) / "metadata.json").read_text())
            except (OSError, ValueError):
                raise SandboxError("Cannot account for the owner of a running sandbox") from None
            owned += data.get("owner_hash") == manager._owner_hash(owner)
        if owned >= manager.max_per_owner:
            raise SandboxError("Owner VM sandbox capacity has been reached")

    def _directory(self, manager, boot, *, create=False):
        directory = manager._directory(boot["binding"].sandbox_id)
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        expected = boot["workspace"]["directory_identity"]
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or (expected is not None and [info.st_dev, info.st_ino] != expected)):
            raise SandboxError("VM system directory ownership or identity changed")
        if expected is None and any(directory.iterdir()):
            raise SandboxError("Refusing to adopt an unjournalled nonempty VM directory")
        return directory, [info.st_dev, info.st_ino]

    def _prepare(self, manager, boot, owner):
        directory, _ = self._directory(manager, boot)
        spec = boot["workspace"]["spec"]
        base = self._file(manager.image, private=False)
        if base != {"identity": spec["image_identity"], "sha256": spec["image_sha256"]}:
            raise SandboxError("Pinned VM base image was replaced or modified")
        # Called only in 'preparing', after independent proof of domain absence.
        # Only these pre-boot generated files may be discarded; never the data disk.
        paths = list(directory.iterdir())
        if any(path.name not in FILES for path in paths):
            raise SandboxError("Unrecognized data in an incomplete VM system directory")
        for path in paths:
            self._file(path, digest=False)
        for path in paths:
            path.unlink()
        system = directory / "disk.qcow2"
        system.touch(mode=0o600, exist_ok=False)
        self.host.run("qemu-img", "create", "-f", "qcow2", "-b", str(manager.image), "-F", "qcow2",
                      str(system), str(spec["disk_gib"] * 1024**3), timeout=120)
        system.chmod(0o600)
        binding, workspace = boot["binding"], boot["workspace"]
        metadata = {
            "id": binding.sandbox_id, "owner_hash": manager._owner_hash(owner),
            "owner_ref": manager._owner_ref(owner), "purpose": "project", "runtime": "debian",
            "state": "provisioning", "project_domain_uuid": workspace["domain_uuid"],
            "project": {key: value for key, value in binding.guest_config().items() if key != "initialize"},
            "project_spec": spec,
        }
        contents = {
            "user-data": manager._cloud_config(project_disk=binding, initialize_project=workspace["initialize"]),
            "meta-data": f"instance-id: {workspace['domain_uuid']}\nlocal-hostname: {manager._name(binding.sandbox_id)}\n",
            "domain.xml": manager._domain_xml(binding.sandbox_id, directory, project_disk=binding,
                                               project_domain_uuid=workspace["domain_uuid"]),
        }
        for name, content in contents.items():
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        write_json(directory / "metadata.json", metadata)
        seed = directory / "seed.iso"
        seed.touch(mode=0o600, exist_ok=False)
        self.host.run("cloud-localds", str(seed), str(directory / "user-data"), str(directory / "meta-data"), timeout=120)
        seed.chmod(0o600)
        for path in (system, seed):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        artifacts = {name: self._file(directory / name, digest=name != "disk.qcow2") for name in sorted(FILES)}
        self._system_image(manager, boot, artifacts)
        return artifacts

    def _system_image(self, manager, boot, artifacts):
        path = manager._directory(boot["binding"].sandbox_id) / "disk.qcow2"
        observed = self._file(path, digest=False)
        if observed["identity"][:2] != artifacts["disk.qcow2"]["identity"][:2]:
            raise SandboxError("VM system disk was replaced")
        metadata = self._image_info(path, shared=True)
        if (metadata.get("virtual-size") != boot["workspace"]["spec"]["disk_gib"] * 1024**3
                or metadata.get("full-backing-filename", metadata.get("backing-filename")) != str(manager.image)
                or metadata.get("backing-filename-format") != "qcow2"):
            raise SandboxError("VM system overlay differs from the pinned boot image or quota")

    def _verify_files(self, manager, boot):
        directory, _ = self._directory(manager, boot)
        artifacts = boot["workspace"]["artifacts"]
        if set(artifacts) != FILES:
            raise SandboxError("VM boot artifacts are incomplete")
        spec = boot["workspace"]["spec"]
        if self._file(manager.image, private=False, digest=False)["identity"] != spec["image_identity"]:
            raise SandboxError("Pinned VM base image identity changed")
        for name in FILES - {"disk.qcow2"}:
            if self._file(directory / name) != artifacts[name]:
                raise SandboxError("A prepared VM bootstrap file changed")
        self._system_image(manager, boot, artifacts)

    async def boot(self, owner, project_id, *, generation, fence, reservation):
        # Validate journal ownership before deriving the host operation lock path.
        await _journal(self.disks.status, owner, project_id, generation=generation)
        self.disks._private_directory(self.manager.root.absolute())
        async with self.manager.allocation_lock():
            status = await _journal(self.disks.status, owner, project_id, generation=generation)
            spec = None if status["workspace_present"] else await _journal(self._spec)
            boot = await _journal(self.disks.begin_boot, owner, project_id, generation=generation,
                                  fence=fence, reservation=reservation, spec=spec)
            manager = self._manager(boot)
            binding = boot["binding"]
            async with manager._exec_locks.setdefault(binding.sandbox_id, asyncio.Lock()):
                async def checkpoint(stage, **kwargs):
                    return await _journal(self.disks.checkpoint_boot, owner, project_id, generation=generation,
                                          fence=fence, reservation=reservation, stage=stage, **kwargs)
                stage = boot["workspace"]["stage"]
                state = await self._observe(manager, boot)
                if stage in {"allocated", "preparing", "prepared"} and state is not None:
                    raise SandboxError("A VM exists before its journalled definition stage")
                if stage == "allocated":
                    await self._capacity(manager, boot, owner)
                    _, identity = await _journal(self._directory, manager, boot, create=True)
                    boot = await checkpoint("preparing", directory_identity=identity)
                    stage = "preparing"
                if stage == "preparing":
                    artifacts = await _journal(self._prepare, manager, boot, owner)
                    boot = await checkpoint("prepared", artifacts=artifacts)
                    stage = "prepared"
                await _journal(self._verify_files, manager, boot)
                if stage == "prepared":
                    boot = await checkpoint("defining")
                    stage = "defining"
                if stage == "defining":
                    if await self._observe(manager, boot) is None:
                        await manager._virsh("define", str(manager._directory(binding.sandbox_id) / "domain.xml"))
                    if await self._observe(manager, boot) is None:
                        raise SandboxError("VM definition was not observed")
                    boot = await checkpoint("defined")
                    stage = "defined"
                if stage == "resuming":
                    if state is None:
                        raise SandboxError("A previously booted VM disappeared; refusing silent replacement")
                    boot = await checkpoint("defined")
                    stage = "defined"
                if stage == "defined":
                    await self._capacity(manager, boot, owner)
                    boot = await checkpoint("starting")
                    stage = "starting"
                if stage in {"starting", "started", "ready"}:
                    state = await self._observe(manager, boot)
                    if state == "shut off":
                        await self._capacity(manager, boot, owner)
                        await manager._virsh("start", boot["workspace"]["domain_uuid"])
                    if await self._observe(manager, boot) != "running":
                        raise SandboxError("VM start was not observed")
                if stage == "starting":
                    boot = await checkpoint("started")
                    stage = "started"
                if stage not in {"started", "ready"} or await self._observe(manager, boot) != "running":
                    raise SandboxError("Project VM is not running at the expected boot stage")
                await manager._wait_guest(binding.sandbox_id)
                code, _, _ = await manager._guest_exec(binding.sandbox_id, "/usr/bin/timeout",
                    ["--kill-after=5s", "240s", "/usr/bin/cloud-init", "status", "--wait"], timeout=250)
                if code:
                    raise SandboxError("Project cloud-init did not complete; the VM and data disk were retained")
                code, output, _ = await manager._guest_exec(binding.sandbox_id, "/usr/bin/timeout",
                    ["--kill-after=5s", "300s", "/usr/bin/python3", HELPER_PATH, "mount"], timeout=310)
                if code:
                    raise SandboxError("Project workspace did not mount; the VM and data disk were retained")
                try:
                    receipt = json.loads(output)
                except ValueError:
                    raise SandboxError("Invalid guest mount acknowledgement") from None
                if await self._observe(manager, boot) != "running":
                    raise SandboxError("Project VM changed before acknowledgement")
                result = await _journal(self.disks.finish_boot, owner, project_id, receipt,
                    generation=generation, fence=fence, reservation=reservation)
                return {**result, "verified": True, "state": "running", "used_bytes": receipt["used_bytes"]}
