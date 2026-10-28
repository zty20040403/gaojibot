"""Reserved VM quota changes, reconciled across guest, libvirt and host files.

Only the lifecycle executor may call this adapter after access/capacity checks.
It never selects an arbitrary device, recreates a VM or releases its reservation.
"""

import copy
from dataclasses import replace
import hashlib
import json
import os
import stat
from xml.etree import ElementTree

from .errors import SandboxError
from .project_vm_disks import VmProjectDiskBinding
from .project_vm_gate import HELPER_PATH
from .project_vm_io import _journal
from .project_vm_mounts import VmProjectMounts
from .project_vm_provision import VmProjectProvisioner

NAMESPACE = "urn:gaoji:project:1"
MUTABLE_FILES = frozenset({"domain.xml", "metadata.json"})


def stages(state):
    grow = state["quota_bytes"] > state["from_quota_bytes"]
    middle = ["device", "filesystem"] if grow else ["filesystem", "device"]
    return ["preparing_guest", "guest_prepared", *(
        step for kind in middle for step in ("resizing_" + kind, kind + "_ready")
    ), "committing_guest", "guest_committed", "publishing_files", "files_ready",
        "publishing_metadata", "metadata_ready", "completed"]


class VmResizeJournal:
    def __init__(self, disks):
        self.disks = disks

    def _context(self, record, generation, reservation, fence, quota_bytes):
        d = self.disks
        d._generation(record, generation)
        d._require_reservation(record, reservation)
        d._quota(quota_bytes)
        if (record is None or record.get("deleted") or not record.get("maintenance")
                or not record.get("guest_initialized") or not record.get("workspace")
                or record["workspace"]["stage"] != "ready"
                or record["sandbox_id"] != record["workspace"]["sandbox_id"]
                or record["guest_domain_uuid"] != record["workspace"]["domain_uuid"]
                or type(fence) is not int or not record["fence"] <= fence < 2**53):
            raise SandboxError("Resize requires the current reserved, initialized VM")
        expected = {"operation": "vm_resize", "arguments": {"quota_bytes": quota_bytes}}
        if (record["pending"] or record["fence"] == fence) and record["intent"] != expected:
            raise SandboxError("Reconcile the original VM operation before resizing")

    def _view(self, directory, record):
        state = record["vm_resize"]
        self.disks._quota(state["from_quota_bytes"])
        self.disks._quota(state["quota_bytes"])
        if (state["fence"] != record["fence"] or state["stage"] not in stages(state)
                or record["intent"] != {"operation": "vm_resize", "arguments": {"quota_bytes": state["quota_bytes"]}}):
            raise SandboxError("VM resize journal is inconsistent")
        d = self.disks
        path, _ = d._image(directory, record)
        size = d.host.info(path, shared=True).get("virtual-size")
        if type(size) is not int or size not in {state["from_quota_bytes"], state["quota_bytes"]}:
            raise SandboxError("VM resize has unexpected image geometry")
        d._inspect(directory, record, shared=True, expected_quota=size)
        if d.host.consumers(path) != {"gaoji-vm-" + record["sandbox_id"]}:
            raise SandboxError("VM resize requires exactly its original disk consumer")
        return {"binding": VmProjectDiskBinding(record["project_id"], record["generation"],
                    record["sandbox_id"], path, record["fs_uuid"], state["from_quota_bytes"]),
                "workspace": copy.deepcopy(record["workspace"]), "resize": copy.deepcopy(state)}

    def begin(self, owner, project_id, *, generation, reservation, fence, quota_bytes):
        d = self.disks
        with d._locked(owner, project_id) as (directory, record):
            self._context(record, generation, reservation, fence, quota_bytes)
            if record["fence"] == fence:
                return self._view(directory, record)
            if record["pending"] or record["quota_bytes"] == quota_bytes:
                raise SandboxError("Resize requires a settled workspace and a new capacity")
            d._status(directory, record)
            d._admit(min(max(0, quota_bytes - record["quota_bytes"]), 256 * 1024**2))
            record["vm_resize"] = {"fence": fence, "from_quota_bytes": record["quota_bytes"],
                "quota_bytes": quota_bytes, "stage": "preparing_guest", "was_mounted": None,
                "file_contents": None, "file_artifacts": None}
            d._begin(directory, record, "vm_resize", fence, {"quota_bytes": quota_bytes})
            return self._view(directory, record)

    def checkpoint(self, owner, project_id, *, generation, reservation, fence, quota_bytes, stage, **changes):
        d = self.disks
        with d._locked(owner, project_id) as (directory, record):
            self._context(record, generation, reservation, fence, quota_bytes)
            view = self._view(directory, record)
            state, order = record["vm_resize"], stages(view["resize"])
            if (not record["pending"] or record["fence"] != fence
                    or state["stage"] == "completed"
                    or stage != order[order.index(state["stage"]) + 1]):
                raise SandboxError("VM resize checkpoint is stale or out of order")
            allowed = {"guest_prepared": {"was_mounted"}, "publishing_files": {"file_contents"},
                       "files_ready": {"file_artifacts"}}.get(stage, set())
            if set(changes) != allowed:
                raise SandboxError("Invalid VM resize checkpoint evidence")
            if "was_mounted" in changes and type(changes["was_mounted"]) is not bool:
                raise SandboxError("Original mount state is unverified")
            for key in ("file_contents", "file_artifacts"):
                if key in changes and (not isinstance(changes[key], dict) or set(changes[key]) != MUTABLE_FILES):
                    raise SandboxError("VM quota publication must only change its two owned metadata files")
            state.update(stage=stage, **copy.deepcopy(changes))
            d._save(directory, record)
            return self._view(directory, record)

    def finish(self, owner, project_id, *, generation, reservation, fence, quota_bytes):
        d = self.disks
        with d._locked(owner, project_id) as (directory, record):
            self._context(record, generation, reservation, fence, quota_bytes)
            self._view(directory, record)
            state = record["vm_resize"]
            if record["fence"] != fence or state["stage"] not in {"metadata_ready", "completed"}:
                raise SandboxError("VM quota has not reached verified publication")
            d._inspect(directory, record, shared=True, expected_quota=quota_bytes)
            record["quota_bytes"] = quota_bytes
            record["workspace"]["artifacts"].update(state["file_artifacts"])
            record["guest_unmounted"] = None if state["was_mounted"] else {
                "sandbox_id": record["sandbox_id"], "domain_uuid": record["guest_domain_uuid"],
                "filesystem_uuid": record["fs_uuid"],
            }
            state["stage"] = "completed"
            result = d._finish(directory, record)
            return {**result, "verified": True, "state": "resized", "from_quota_bytes": state["from_quota_bytes"]}


class VmProjectResize:
    def __init__(self, manager, disks, *, host=None):
        self.manager, self.disks = manager, disks
        self.provisioner = VmProjectProvisioner(manager, disks, host=host)
        self.journal = VmResizeJournal(disks)

    async def _domains(self, manager, view):
        binding, state = view["binding"], view["resize"]
        identifier = view["workspace"]["domain_uuid"]
        inventory = await self.provisioner._inventory(manager)
        if identifier not in inventory:
            raise SandboxError("The original resize VM disappeared; no replacement will be created")
        runtime = inventory[identifier][1]
        publishing = state["stage"] in {"publishing_metadata", "metadata_ready", "completed"}
        allowed = {state["quota_bytes"]} if state["stage"] in {"metadata_ready", "completed"} else {binding.quota_bytes}
        if publishing and state["stage"] == "publishing_metadata":
            allowed.add(state["quota_bytes"])
        capacities = []
        try:
            for suffix in ((), ("--inactive",)):
                xml = ElementTree.fromstring(await manager._virsh("dumpxml", identifier, *suffix))
                node = xml.find("./metadata/{" + NAMESPACE + "}workspace")
                quota = int(node.get("quota_bytes"))
                if quota not in allowed:
                    raise ValueError()
                VmProjectMounts(manager, self.disks)._validate_domain(xml, replace(binding, quota_bytes=quota),
                    identifier, spec=view["workspace"]["spec"])
                capacities.append(quota)
        except (ValueError, TypeError, AttributeError, ElementTree.ParseError):
            raise SandboxError("VM definition changed outside the journalled quota publication") from None
        return runtime, capacities

    def _verify_files(self, manager, view):
        state, observed = view["resize"], copy.deepcopy(view)
        artifacts = state["file_artifacts"]
        if state["stage"] == "publishing_files":
            directory, _ = self.provisioner._directory(manager, view)
            artifacts = {}
            for name in MUTABLE_FILES:
                actual = self.provisioner._file(directory / name)
                content = state["file_contents"][name].encode()
                if actual != view["workspace"]["artifacts"][name] and (
                        actual["sha256"] != hashlib.sha256(content).hexdigest() or actual["identity"][2] != len(content)):
                    raise SandboxError("VM quota file differs from both journalled versions")
                artifacts[name] = actual
        if artifacts:
            observed["workspace"]["artifacts"].update(artifacts)
        self.provisioner._verify_files(manager, observed)

    def _contents(self, manager, view):
        self._verify_files(manager, view)
        directory = manager._directory(view["binding"].sandbox_id)
        metadata = json.loads((directory / "metadata.json").read_text())
        xml = ElementTree.fromstring((directory / "domain.xml").read_text())
        quota = view["resize"]["quota_bytes"]
        metadata["project"]["quota_bytes"] = quota
        xml.find("./metadata/{" + NAMESPACE + "}workspace").set("quota_bytes", str(quota))
        return {"metadata.json": json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                "domain.xml": ElementTree.tostring(xml, encoding="unicode")}

    @staticmethod
    def _write(path, content, staging):
        # The durable intent owns this bounded staging directory. Unlike a
        # random tempfile, its remnants can be identified after process loss.
        temporary = staging / path.name
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise SandboxError("Unsafe VM quota staging file")
            os.ftruncate(descriptor, 0)
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise SandboxError("VM quota staging write did not make progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        for parent in (staging, path.parent):
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _staging(self, manager, view):
        directory = manager._directory(view["binding"].sandbox_id) / f".quota-{view['resize']['fence']}"
        self.disks._private_directory(directory)
        for path in directory.iterdir():
            if path.name not in MUTABLE_FILES:
                raise SandboxError("Unrecognized VM quota staging data must be retained")
            info = self.provisioner._file(path)
            expected = view["resize"]["file_contents"][path.name].encode()
            if info["identity"][2] > len(expected) or not expected.startswith(path.read_bytes()):
                raise SandboxError("VM quota staging data differs from its recorded intent")
        return directory

    def _publish_files(self, manager, view):
        self._verify_files(manager, view)
        directory = manager._directory(view["binding"].sandbox_id)
        staging = self._staging(manager, view)
        result = {}
        for name in sorted(MUTABLE_FILES):
            content = view["resize"]["file_contents"][name].encode()
            actual = self.provisioner._file(directory / name)
            if actual["sha256"] != hashlib.sha256(content).hexdigest():
                self._write(directory / name, content, staging)
            result[name] = self.provisioner._file(directory / name)
            if result[name]["sha256"] != hashlib.sha256(content).hexdigest():
                raise SandboxError("VM quota file publication was not verified")
        self._staging(manager, view)
        for path in staging.iterdir():
            path.unlink()
        staging.rmdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return result

    async def _capacity(self, manager, view):
        state = view["resize"]
        output = await manager._virsh("domblkinfo", view["workspace"]["domain_uuid"], "vdb")
        try:
            values = dict(line.split(":", 1) for line in output.splitlines() if ":" in line)
            capacity = int(values["Capacity"])
        except (KeyError, ValueError):
            raise SandboxError("Cannot read the live project block capacity") from None
        if capacity not in {state["from_quota_bytes"], state["quota_bytes"]}:
            raise SandboxError("Live project disk has unexpected capacity")
        await _journal(self._check_capacity, manager, view, capacity)
        return capacity

    def _check_capacity(self, manager, view, capacity):
        # Verify ownership, inode, format and actual image geometry together.
        binding = view["binding"]
        with self.disks._locked(view["owner"], binding.project_id) as (directory, record):
            self.disks._inspect(directory, record, shared=True, expected_quota=capacity)
            if self.disks.host.consumers(binding.path) != {manager._name(binding.sandbox_id)}:
                raise SandboxError("Project disk gained another consumer during resize")

    async def _guest(self, manager, view, phase):
        binding, state = view["binding"], view["resize"]
        request = {"action": "resize", "phase": phase, "epoch": state["fence"],
                   "from_quota_bytes": state["from_quota_bytes"], "quota_bytes": state["quota_bytes"]}
        code, output, _ = await manager._guest_exec_raw(binding.sandbox_id, "/usr/bin/python3", [HELPER_PATH],
            input_bytes=json.dumps(request).encode(), timeout=540, domain_uuid=view["workspace"]["domain_uuid"])
        expected = {k: v for k, v in binding.guest_config().items() if k not in {"initialize", "quota_bytes"}}
        expected.update(epoch=state["fence"], from_quota_bytes=state["from_quota_bytes"],
            quota_bytes=state["quota_bytes"], mode="quiesced", verified=True,
            phase={"prepare": "prepared", "apply": "filesystem_ready", "commit": "completed"}[phase])
        try:
            if code or len(output) > 8192:
                raise ValueError()
            result = json.loads(output)
            if (not isinstance(result, dict) or set(result) != {*expected, "mounted", "was_mounted", "device_bytes", "filesystem_bytes"}
                    or any(type(result.get(k)) is not type(v) or result[k] != v for k, v in expected.items())
                    or any(type(result[k]) is not bool for k in ("mounted", "was_mounted"))
                    or any(type(result[k]) is not int for k in ("device_bytes", "filesystem_bytes"))
                    or (state["was_mounted"] is not None and result["was_mounted"] != state["was_mounted"])):
                raise ValueError()
            shrinking = state["quota_bytes"] < state["from_quota_bytes"]
            device = state["quota_bytes"] if phase == "commit" or (phase == "apply" and not shrinking) else state["from_quota_bytes"]
            if phase == "apply" and shrinking:
                device = await self._capacity(manager, view)
            filesystem = state["from_quota_bytes"] if phase == "prepare" else state["quota_bytes"]
            mounted = False if shrinking and phase != "commit" else result["was_mounted"]
            if (result["device_bytes"] != device or result["filesystem_bytes"] != filesystem
                    or result["mounted"] != mounted):
                raise ValueError()
            return result
        except (ValueError, TypeError, KeyError):
            raise SandboxError("Guest resize receipt does not prove the requested phase") from None

    async def resize(self, owner, project_id, *, generation, fence, reservation, quota_bytes):
        options = dict(generation=generation, fence=fence, reservation=reservation, quota_bytes=quota_bytes)
        self.disks._private_directory(self.manager.root.absolute())
        async with self.manager.allocation_lock():
            view = await _journal(self.journal.begin, owner, project_id, **options)
            manager = self.provisioner._manager(view)
            async with manager.io_lock(view["binding"].sandbox_id, timeout=0):
                async def checkpoint(stage, **changes):
                    value = await _journal(self.journal.checkpoint, owner, project_id, **options, stage=stage, **changes)
                    return {**value, "owner": owner}
                view["owner"] = owner
                await _journal(self._verify_files, manager, view)
                runtime, _ = await self._domains(manager, view)
                if runtime == "shut off":
                    await self.provisioner._capacity(manager, view, owner)
                    await manager._virsh("start", view["workspace"]["domain_uuid"])
                    await manager._wait_guest(view["binding"].sandbox_id)
                if (await self._domains(manager, view))[0] != "running":
                    raise SandboxError("Resize requires its original running guest")
                # This leaves every replay, including a restarted guest, closed to writers.
                await VmProjectMounts(manager, self.disks)._control(view["binding"].sandbox_id,
                    view["binding"], "quiesce", fence)
                while view["resize"]["stage"] != "completed":
                    state = view["resize"]
                    stage = state["stage"]
                    if (await self._domains(manager, view))[0] != "running":
                        raise SandboxError("VM stopped during resize; its reservation is retained")
                    await _journal(self._verify_files, manager, view)
                    if stage == "preparing_guest":
                        receipt = await self._guest(manager, view, "prepare")
                        view = await checkpoint("guest_prepared", was_mounted=receipt["was_mounted"])
                    elif stage == "resizing_device":
                        current = await self._capacity(manager, view)
                        if current != quota_bytes:
                            if quota_bytes < state["from_quota_bytes"]:
                                await self._guest(manager, view, "apply")
                            await manager._virsh("blockresize", view["workspace"]["domain_uuid"], "vdb", f"{quota_bytes}B")
                        if await self._capacity(manager, view) != quota_bytes:
                            raise SandboxError("Host block resize did not reach its target")
                        view = await checkpoint("device_ready")
                    elif stage == "resizing_filesystem":
                        await self._guest(manager, view, "apply")
                        view = await checkpoint("filesystem_ready")
                    elif stage == "committing_guest":
                        await self._guest(manager, view, "commit")
                        view = await checkpoint("guest_committed")
                    elif stage == "guest_committed":
                        contents = await _journal(self._contents, manager, view)
                        view = await checkpoint("publishing_files", file_contents=contents)
                    elif stage == "publishing_files":
                        artifacts = await _journal(self._publish_files, manager, view)
                        view = await checkpoint("files_ready", file_artifacts=artifacts)
                    elif stage == "publishing_metadata":
                        _, capacities = await self._domains(manager, view)
                        xml = ElementTree.fromstring(state["file_contents"]["domain.xml"])
                        node = xml.find("./metadata/{" + NAMESPACE + "}workspace")
                        payload = ElementTree.tostring(node, encoding="unicode")
                        for flag, current in zip(("--live", "--config"), capacities):
                            if current != quota_bytes:
                                await manager._virsh("metadata", view["workspace"]["domain_uuid"], flag,
                                    "--uri", NAMESPACE, "--key", "gaoji", "--set", payload)
                        if (await self._domains(manager, view))[1] != [quota_bytes, quota_bytes]:
                            raise SandboxError("Live and persistent quota metadata were not published")
                        view = await checkpoint("metadata_ready")
                    elif stage == "metadata_ready":
                        break
                    else:
                        order = stages(state)
                        view = await checkpoint(order[order.index(stage) + 1])
                await self._guest(manager, view, "commit")
                if (await self._domains(manager, view)) != ("running", [quota_bytes, quota_bytes]):
                    raise SandboxError("VM quota changed before final verification")
                if await self._capacity(manager, view) != quota_bytes:
                    raise SandboxError("VM quota is not physically committed")
                await _journal(self._verify_files, manager, view)
                return await _journal(self.journal.finish, owner, project_id, **options)
