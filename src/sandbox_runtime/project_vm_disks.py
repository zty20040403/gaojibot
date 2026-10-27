"""Persistent project data disks, independent of disposable VM system overlays.

This is a private host API, not a model tool. The lifecycle executor must still
check live project membership, tasks and undelivered artifacts before mutation.
"""

from dataclasses import dataclass
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import secrets
import subprocess
import uuid
from xml.etree import ElementTree

from .docker import SANDBOX_ID_PATTERN
from .project_volume_journal import MIB, ProjectVolumeError, ProjectVolumeJournal
from .project_vm_guest import disk_serial, validate_config


class QcowProjectHost:
    """Fixed local QEMU/libvirt operations; no guest-provided command or path."""

    @staticmethod
    def run(*args, timeout=60):
        try:
            result = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProjectVolumeError(
                f"Project disk utility unavailable: {Path(args[0]).name}"
            ) from None
        if result.returncode:
            raise ProjectVolumeError(
                f"Project disk utility failed: {Path(args[0]).name} ({result.returncode})"
            )
        return result.stdout

    def create(self, path, quota):
        self.run(
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-o",
            "preallocation=off",
            str(path),
            str(quota),
        )

    def info(self, path, *, shared=False):
        args = ["qemu-img", "info", "--output=json", "-f", "qcow2"]
        if shared:
            args.append("--force-share")
        try:
            return json.loads(self.run(*args, str(path)))
        except ValueError:
            raise ProjectVolumeError("Invalid QEMU disk metadata") from None

    def check(self, path):
        # Do not repair automatically, especially not lost or partial images.
        self.run("qemu-img", "check", "-f", "qcow2", str(path), timeout=120)

    def grow(self, path, quota):
        self.run("qemu-img", "resize", "-f", "qcow2", str(path), str(quota))

    def consumers(self, path):
        prefix = ("virsh", "-c", "qemu:///session")
        identifiers = self.run(*prefix, "list", "--all", "--uuid").split()
        try:
            if len(identifiers) > 256 or any(
                str(uuid.UUID(item)) != item for item in identifiers
            ):
                raise ValueError("Invalid domain inventory")
            found = set()
            for identifier in identifiers:
                # Inspect both definitions: a stopped domain or a pending config
                # change can still reopen this disk after the current VM exits.
                for suffix in ((), ("--inactive",)):
                    xml = ElementTree.fromstring(
                        self.run(*prefix, "dumpxml", identifier, *suffix)
                    )
                    name = xml.findtext("name")
                    if (
                        xml.tag != "domain"
                        or not name
                        or xml.findtext("uuid") != identifier
                    ):
                        raise ValueError("Invalid domain identity")
                    for node in xml.findall("./devices/disk//source"):
                        source = node.get("file")
                        if source and (
                            Path(source) == path or os.path.samefile(source, path)
                        ):
                            found.add(name)
            return found
        except (OSError, ValueError, ElementTree.ParseError):
            raise ProjectVolumeError(
                "Cannot verify VM project disk consumers"
            ) from None


@dataclass(frozen=True)
class VmProjectDiskBinding:
    project_id: int
    generation: int
    sandbox_id: str
    path: Path
    filesystem_uuid: str
    quota_bytes: int

    def guest_config(self, *, initialize=False):
        return validate_config({
            "project_id": self.project_id, "generation": self.generation,
            "filesystem_uuid": self.filesystem_uuid, "quota_bytes": self.quota_bytes,
            "serial": disk_serial(self.project_id, self.generation, self.filesystem_uuid),
            "initialize": initialize,
        })


class VmProjectDisks(ProjectVolumeJournal):
    storage_kind = "qcow2"

    def __init__(self, root: Path, instance_id: str, *, host=None, capacity_check=None):
        super().__init__(root, instance_id, capacity_check=capacity_check)
        self.host = host or QcowProjectHost()

    @staticmethod
    def _path(directory, record):
        value = record.get("fs_uuid")
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError("Invalid UUID")
        except ValueError:
            raise ProjectVolumeError("Invalid VM project filesystem identity") from None
        return directory / f"{value}.qcow2"

    def _image(self, directory, record):
        path = self._path(directory, record)
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise ProjectVolumeError(
                "Project disk is missing; refusing an empty replacement"
            ) from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or record["image_identity"] != [info.st_dev, info.st_ino]
        ):
            raise ProjectVolumeError("VM project disk ownership or identity changed")
        return path, info

    def _inspect(self, directory, record, *, shared=False, expected_quota=None):
        path, info = self._image(directory, record)
        metadata = self.host.info(path, shared=shared)
        if not isinstance(metadata, dict):
            raise ProjectVolumeError("Invalid QEMU disk metadata")
        specific = metadata.get("format-specific", {})
        if not isinstance(specific, dict) or not isinstance(specific.get("data"), dict):
            raise ProjectVolumeError("Invalid QEMU disk format metadata")
        details = specific["data"]
        if (
            not isinstance(metadata, dict)
            or metadata.get("format") != "qcow2"
            or type(metadata.get("virtual-size")) is not int
            or metadata["virtual-size"] != (expected_quota or record["quota_bytes"])
            or metadata.get("backing-filename")
            or metadata.get("full-backing-filename")
            or metadata.get("encrypted")
            or metadata.get("snapshots")
            or specific.get("type") != "qcow2"
            or not isinstance(details, dict)
            or "data-file" in details
            or "external-data-file" in details
            or "encrypt" in details
            or details.get("corrupt")
        ):
            raise ProjectVolumeError(
                "VM project disk format or quota differs from its journal"
            )
        return path, info

    def _idle(self, path, record):
        if record.get("sandbox_id") or self.host.consumers(path):
            raise ProjectVolumeError(
                "Project disk still has a VM consumer or attachment claim"
            )

    def _status(self, directory, record):
        result = {
            "project_id": record["project_id"],
            "generation": record["generation"],
            "fence": record["fence"],
            "pending": record["pending"],
            "pending_operation": record["intent"]["operation"]
            if record["pending"]
            else None,
            "deleted": bool(record.get("deleted")),
            "quota_mode": "hard",
            "quota_bytes": record["quota_bytes"],
            "sandbox_id": record.get("sandbox_id"),
            "maintenance_active": bool(record.get("maintenance")),
            "guest_initialized": record.get("guest_initialized") is True,
            "workspace_present": bool(record.get("workspace")),
        }
        if record.get("deleted"):
            if (
                self._path(directory, record).exists()
                or self._path(directory, record).is_symlink()
            ):
                raise ProjectVolumeError("Deleted project disk unexpectedly reappeared")
            return result
        path, info = self._inspect(directory, record, shared=True)
        consumers = self.host.consumers(path)
        expected = (
            {"gaoji-vm-" + record["sandbox_id"]} if record.get("sandbox_id") else set()
        )
        if consumers - expected:
            raise ProjectVolumeError("Project disk is attached to an unexpected VM")
        # Allocation includes qcow2 metadata, not guest filesystem usage.
        result.update(allocated_bytes=info.st_blocks * 512, attached=bool(consumers))
        return result

    def status(self, owner, project_id, *, generation=1):
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            return self._status(directory, record)

    def create(self, owner, project_id, *, quota_bytes, fence, generation=1):
        quota_bytes = self._quota(quota_bytes)
        self._generation(None, generation)
        with self._locked(owner, project_id) as (directory, record):
            self._require_reservation(record, None)
            previous = record["fence"] if record else 0
            replacing = record is not None and record.get("deleted")
            if replacing:
                if (
                    generation != record["generation"] + 1
                    or fence <= previous
                    or record["pending"]
                ):
                    raise ProjectVolumeError(
                        "Deleted workspace needs the next generation and a newer fence"
                    )
            else:
                self._generation(record, generation)
                if record is None and generation != 1:
                    raise ProjectVolumeError("Initial workspace generation must be one")
            if record is None or replacing:
                self._admit(min(quota_bytes, 256 * MIB))
                if any(path.name != "volume.json" for path in directory.iterdir()):
                    raise ProjectVolumeError(
                        "Unrecognized data exists in VM project directory"
                    )
                record = {
                    "schema": 1,
                    "storage_kind": self.storage_kind,
                    "instance_id": self.instance_id,
                    "project_id": project_id,
                    "generation": generation,
                    "owner_hash": hashlib.sha256(owner.encode()).hexdigest(),
                    "fs_uuid": str(uuid.uuid4()),
                    "quota_bytes": quota_bytes,
                    "image_identity": None,
                    "fence": previous,
                    "pending": False,
                    "intent": None,
                    "initialized": False,
                    "sandbox_id": None,
                    "guest_initialized": False,
                    "bootstrap_domain_uuid": None,
                    "bootstrap_sandbox_id": None,
                    "guest_domain_uuid": None,
                }
            if record.get("deleted") or (
                record["intent"] and record["intent"]["operation"] != "create"
            ):
                raise ProjectVolumeError("Project disk already exists or was deleted")
            if record["quota_bytes"] != quota_bytes:
                raise ProjectVolumeError("Cannot change capacity during disk creation")
            if not self._begin(
                directory, record, "create", fence, {"quota_bytes": quota_bytes}
            ):
                return self._status(directory, record)
            path = self._path(directory, record)
            if record["image_identity"] is None:
                if not path.exists() and not path.is_symlink():
                    fd = os.open(
                        path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size != 0
                ):
                    raise ProjectVolumeError(
                        "Cannot adopt interrupted VM disk creation"
                    )
                record["image_identity"] = [info.st_dev, info.st_ino]
                self._save(directory, record)
            path, info = self._image(directory, record)
            self._idle(path, record)
            if not info.st_size and not record["initialized"]:
                self.host.create(path, quota_bytes)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._inspect(directory, record)
            self.host.check(path)
            record["initialized"] = True
            return self._finish(directory, record)

    def claim(self, owner, project_id, sandbox_id, *, fence, generation=1, reservation=None):
        if not isinstance(sandbox_id, str) or not SANDBOX_ID_PATTERN.fullmatch(
            sandbox_id
        ):
            raise ProjectVolumeError("Invalid project VM identity")
        with self._locked(owner, project_id) as (directory, record):
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record.get("sandbox_id") not in (None, sandbox_id):
                raise ProjectVolumeError("Project disk already claimed by another VM")
            path, _ = self._inspect(directory, record, shared=True)
            consumers = self.host.consumers(path)
            if (not record.get("sandbox_id") and consumers) or consumers - {
                "gaoji-vm-" + sandbox_id
            }:
                raise ProjectVolumeError("Project disk has an unexpected VM consumer")
            if self._begin(
                directory, record, "claim", fence, {"sandbox_id": sandbox_id}
            ):
                record["sandbox_id"] = sandbox_id
                self._finish(directory, record)
            else:
                self._status(directory, record)
            return VmProjectDiskBinding(
                project_id,
                generation,
                sandbox_id,
                path,
                record["fs_uuid"],
                record["quota_bytes"],
            )

    def release(self, owner, project_id, sandbox_id, *, fence, generation=1, reservation=None):
        if not isinstance(sandbox_id, str) or not SANDBOX_ID_PATTERN.fullmatch(
            sandbox_id
        ):
            raise ProjectVolumeError("Invalid project VM identity")
        with self._locked(owner, project_id) as (directory, record):
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record.get("sandbox_id") != sandbox_id and not (
                record.get("sandbox_id") is None
                and record["fence"] == fence
                and record["intent"]
                == {"operation": "release", "arguments": {"sandbox_id": sandbox_id}}
            ):
                raise ProjectVolumeError("Project VM claim does not match")
            path, _ = self._inspect(directory, record)
            if self.host.consumers(path):
                raise ProjectVolumeError(
                    "VM must be undefined before releasing its project disk"
                )
            if not self._begin(
                directory, record, "release", fence, {"sandbox_id": sandbox_id}
            ):
                return self._status(directory, record)
            record["sandbox_id"] = None
            record["guest_domain_uuid"] = None
            return self._finish(directory, record)

    def grow(self, owner, project_id, *, quota_bytes, fence, generation=1, reservation=None):
        quota_bytes = self._quota(quota_bytes)
        with self._locked(owner, project_id) as (directory, record):
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if quota_bytes < record["quota_bytes"]:
                raise ProjectVolumeError(
                    "VM disk shrinking requires a separate offline filesystem migration"
                )
            path, _ = self._image(directory, record)
            self._idle(path, record)
            self._admit(min(max(0, quota_bytes - record["quota_bytes"]), 256 * MIB))
            if not self._begin(
                directory, record, "grow", fence, {"quota_bytes": quota_bytes}
            ):
                return self._status(directory, record)
            size = self.host.info(path).get("virtual-size")
            if size == record["quota_bytes"]:
                self._inspect(directory, record)
                self.host.check(path)
                self.host.grow(path, quota_bytes)
            elif size != quota_bytes:
                raise ProjectVolumeError(
                    "Interrupted VM resize has unexpected geometry"
                )
            self._inspect(directory, record, expected_quota=quota_bytes)
            self.host.check(path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            record["quota_bytes"] = quota_bytes
            return self._finish(directory, record)

    def delete(self, owner, project_id, *, fence, generation=1, reservation=None):
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record.get("deleted"):
                if self._begin(directory, record, "delete", fence, {}):
                    return self._finish(directory, record)
                return self._status(directory, record)
            path = self._path(directory, record)
            if path.exists() or path.is_symlink():
                self._inspect(directory, record)
                self._idle(path, record)
            elif not (
                record["pending"]
                and record["intent"] == {"operation": "delete", "arguments": {}}
            ):
                raise ProjectVolumeError(
                    "Project disk disappeared outside a journalled deletion"
                )
            if not self._begin(directory, record, "delete", fence, {}):
                return self._status(directory, record)
            path.unlink(missing_ok=True)
            record["deleted"] = True
            return self._finish(directory, record)

    @staticmethod
    def _domain_uuid(value):
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except ValueError:
            raise ProjectVolumeError("Invalid project domain UUID") from None
        return value

    def _guest_binding(self, directory, record, sandbox_id, *, allow_pending=False):
        if (record is None or record.get("deleted") or (record["pending"] and not allow_pending)
                or not sandbox_id or record.get("sandbox_id") != sandbox_id):
            raise ProjectVolumeError("Project disk has no settled claim for this VM")
        self._status(directory, record)
        return VmProjectDiskBinding(
            record["project_id"], record["generation"], sandbox_id,
            self._path(directory, record), record["fs_uuid"], record["quota_bytes"],
        )

    def prepare_guest(self, owner, project_id, sandbox_id, domain_uuid, *,
                      generation, fence, reservation):
        """Issue first-format permission to one immutable VM identity only.

        Replacing an unacknowledged system disk never issues another permission.
        The replacement may recover an already formatted filesystem, but a blank
        or damaged disk needs explicit recovery instead of silent data loss.
        """
        domain_uuid = self._domain_uuid(domain_uuid)
        self._reservation_hash(reservation)
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            intent = {"operation": "guest_prepare", "arguments": {
                "sandbox_id": sandbox_id, "domain_uuid": domain_uuid,
            }}
            replay = bool(record and record["intent"] == intent and record["fence"] == fence)
            binding = self._guest_binding(directory, record, sandbox_id, allow_pending=replay)
            existing = record.get("guest_domain_uuid")
            if existing not in (None, domain_uuid):
                raise ProjectVolumeError("Release the old VM claim before replacing its identity")
            if not existing and self.host.consumers(binding.path):
                raise ProjectVolumeError("Cannot adopt a VM defined before its initialization journal")
            if self._begin(directory, record, "guest_prepare", fence, intent["arguments"]):
                record["guest_domain_uuid"] = domain_uuid
                # Store the grant before returning cloud-init configuration.
                if not record.get("bootstrap_domain_uuid") and not record.get("guest_initialized"):
                    record["bootstrap_domain_uuid"] = domain_uuid
                    record["bootstrap_sandbox_id"] = sandbox_id
                self._finish(directory, record)
            return {
                "binding": binding,
                "domain_uuid": domain_uuid,
                "initialize": record.get("guest_initialized") is not True
                and record.get("bootstrap_domain_uuid") == domain_uuid
                and record.get("bootstrap_sandbox_id") == sandbox_id,
            }

    def guest_binding(self, owner, project_id, sandbox_id, domain_uuid, *,
                      generation, reservation=None, require_initialized=True,
                      acknowledge_fence=None, spec=None):
        """Revalidate the host claim immediately before each guest operation."""
        self._domain_uuid(domain_uuid)
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            retry = (record is not None and type(acknowledge_fence) is int
                     and acknowledge_fence == record["fence"]
                     and record["intent"] == {"operation": "guest_acknowledge", "arguments": {
                         "sandbox_id": sandbox_id, "domain_uuid": domain_uuid,
                     }})
            binding = self._guest_binding(directory, record, sandbox_id, allow_pending=retry)
            if record.get("guest_domain_uuid") != domain_uuid:
                raise ProjectVolumeError("VM identity differs from its project claim")
            workspace = record.get("workspace")
            if workspace:
                if self._boot_spec(spec) != workspace["spec"]:
                    raise ProjectVolumeError("VM metadata differs from its frozen resource contract")
            elif spec is not None:
                raise ProjectVolumeError("Unjournalled VM resource contract")
            if require_initialized and record.get("guest_initialized") is not True:
                raise ProjectVolumeError("Project guest initialization is not acknowledged")
            return binding

    def acknowledge_guest(self, owner, project_id, sandbox_id, domain_uuid, receipt, *,
                          generation, fence, reservation):
        """Persist a verified and sealed guest mount, not merely a boot command."""
        self._domain_uuid(domain_uuid)
        self._reservation_hash(reservation)
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            intent = {"operation": "guest_acknowledge", "arguments": {
                "sandbox_id": sandbox_id, "domain_uuid": domain_uuid,
            }}
            replay = bool(record and record["intent"] == intent and record["fence"] == fence)
            binding = self._guest_binding(directory, record, sandbox_id, allow_pending=replay)
            if record.get("guest_domain_uuid") != domain_uuid or self.host.consumers(binding.path) != {"gaoji-vm-" + sandbox_id}:
                raise ProjectVolumeError("Cannot acknowledge an absent or different project VM")
            self._check_guest_receipt(binding, receipt)
            if self._begin(directory, record, "guest_acknowledge", fence, intent["arguments"]):
                record["guest_initialized"] = True
                return self._finish(directory, record)
            return self._status(directory, record)

    @staticmethod
    def _check_guest_receipt(binding, receipt):
        expected = binding.guest_config()
        expected.pop("initialize")
        if (not isinstance(receipt, dict)
                or any(type(receipt.get(key)) is not type(value) or receipt[key] != value for key, value in expected.items())
                or any(receipt.get(key) is not True for key in ("mounted", "verified", "initialization_sealed"))
                or type(receipt.get("used_bytes")) is not int
                or not 0 <= receipt["used_bytes"] <= binding.quota_bytes):
            raise ProjectVolumeError("Guest mount receipt does not prove sealed initialization")

    @staticmethod
    def _boot_spec(spec):
        fields = {"image", "image_identity", "image_sha256", "memory_mib", "vcpus", "pids_limit", "disk_gib"}
        if not isinstance(spec, dict) or set(spec) != fields:
            raise ProjectVolumeError("Incomplete immutable VM boot specification")
        if (not isinstance(spec["image"], str) or not Path(spec["image"]).is_absolute()
                or any(c in spec["image"] for c in "\0\r\n")
                or not isinstance(spec["image_identity"], list) or len(spec["image_identity"]) != 5
                or any(type(value) is not int or value < 0 for value in spec["image_identity"])
                or not isinstance(spec["image_sha256"], str) or len(spec["image_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in spec["image_sha256"])):
            raise ProjectVolumeError("VM base image is not pinned")
        for key, low, high in (("memory_mib", 512, 8192), ("vcpus", 1, 4),
                               ("pids_limit", 32, 4096), ("disk_gib", 4, 100)):
            if type(spec[key]) is not int or not low <= spec[key] <= high:
                raise ProjectVolumeError("VM resource contract is invalid")
        return copy.deepcopy(spec)

    def _boot_binding(self, directory, record):
        workspace = record.get("workspace") if record else None
        if not workspace or record.get("guest_domain_uuid") != workspace["domain_uuid"]:
            raise ProjectVolumeError("VM provisioning identity is unavailable")
        binding = self._guest_binding(directory, record, workspace["sandbox_id"], allow_pending=True)
        return {"binding": binding, "workspace": copy.deepcopy(workspace),
                "pending": record["pending"], "fence": record["fence"]}

    def boot_record(self, owner, project_id, *, generation, reservation, fence=None):
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record and record["pending"] and (
                    type(fence) is not int or record["fence"] != fence
                    or record["intent"]["operation"] != "vm_boot"):
                raise ProjectVolumeError("VM boot is reserved by another pending operation")
            return self._boot_binding(directory, record)

    def begin_boot(self, owner, project_id, *, generation, fence, reservation, spec):
        """Freeze one resumable boot under a single lifecycle effect fence."""
        self._reservation_hash(reservation)
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project disk does not exist")
            if record["pending"] or fence == record["fence"]:
                if (record["fence"] != fence or record["intent"]["operation"] != "vm_boot"):
                    raise ProjectVolumeError("Reconcile the current project operation first")
                self._begin(directory, record, "vm_boot", fence, record["intent"]["arguments"])
                return self._boot_binding(directory, record)
            workspace = record.get("workspace")
            if workspace is None:
                path, _ = self._inspect(directory, record)
                self._idle(path, record)
                workspace = {
                    "sandbox_id": "s" + secrets.token_hex(3), "domain_uuid": str(uuid.uuid4()),
                    "spec": self._boot_spec(spec), "stage": "allocated",
                    "directory_identity": None, "artifacts": {},
                }
                record["workspace"] = workspace
                record["sandbox_id"] = workspace["sandbox_id"]
                record["guest_domain_uuid"] = workspace["domain_uuid"]
                if not record.get("bootstrap_domain_uuid") and not record.get("guest_initialized"):
                    record["bootstrap_domain_uuid"] = workspace["domain_uuid"]
                    record["bootstrap_sandbox_id"] = workspace["sandbox_id"]
                workspace["initialize"] = (record.get("guest_initialized") is not True
                    and record.get("bootstrap_domain_uuid") == workspace["domain_uuid"]
                    and record.get("bootstrap_sandbox_id") == workspace["sandbox_id"])
            else:
                if workspace["stage"] != "ready":
                    raise ProjectVolumeError("Incomplete VM boot cannot be replaced by a new fence")
                self._boot_binding(directory, record)
                workspace["stage"] = "resuming"
            self._begin(directory, record, "vm_boot", fence, {
                "sandbox_id": workspace["sandbox_id"], "domain_uuid": workspace["domain_uuid"],
                "spec": workspace["spec"],
            })
            return self._boot_binding(directory, record)

    def checkpoint_boot(self, owner, project_id, *, generation, fence, reservation,
                        stage, directory_identity=None, artifacts=None):
        transitions = {
            "allocated": {"preparing"}, "preparing": {"preparing", "prepared"},
            "prepared": {"defining"}, "defining": {"defined"},
            "defined": {"starting"}, "starting": {"started"},
            "resuming": {"defined"},
        }
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if (record is None or not record["pending"] or record["fence"] != fence
                    or record["intent"]["operation"] != "vm_boot"):
                raise ProjectVolumeError("VM boot checkpoint is stale")
            current = self._boot_binding(directory, record)["workspace"]
            if stage not in transitions.get(current["stage"], set()):
                raise ProjectVolumeError("VM boot checkpoint would skip or rewind a stage")
            if directory_identity is not None:
                if (current["directory_identity"] is not None
                        or stage != "preparing" or not isinstance(directory_identity, list)
                        or len(directory_identity) != 2
                        or any(type(value) is not int or value < 0 for value in directory_identity)):
                    raise ProjectVolumeError("VM boot directory identity cannot be replaced")
                current["directory_identity"] = list(directory_identity)
            if artifacts is not None:
                if stage != "prepared" or not isinstance(artifacts, dict) or not artifacts:
                    raise ProjectVolumeError("VM boot artifacts must be frozen before definition")
                current["artifacts"] = copy.deepcopy(artifacts)
            if current["directory_identity"] is None or (stage != "preparing" and not current["artifacts"]):
                raise ProjectVolumeError("VM boot checkpoint lacks its owned files")
            current["stage"] = stage
            record["workspace"] = current
            self._save(directory, record)
            return self._boot_binding(directory, record)

    def finish_boot(self, owner, project_id, receipt, *, generation, fence, reservation):
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if (record is None or record["fence"] != fence
                    or record["intent"]["operation"] != "vm_boot"):
                raise ProjectVolumeError("VM boot completion is stale")
            boot = self._boot_binding(directory, record)
            if boot["workspace"]["stage"] not in {"started", "ready"}:
                raise ProjectVolumeError("VM boot has not reached guest verification")
            binding = boot["binding"]
            if self.host.consumers(binding.path) != {"gaoji-vm-" + binding.sandbox_id}:
                raise ProjectVolumeError("VM boot consumer is not verified")
            self._check_guest_receipt(binding, receipt)
            record["guest_initialized"] = True
            record["guest_unmounted"] = None
            record["workspace"]["stage"] = "ready"
            return self._finish(directory, record)
