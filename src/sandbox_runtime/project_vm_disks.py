"""Persistent project data disks, independent of disposable VM system overlays.

This is a private host API, not a model tool. The lifecycle executor must still
check live project membership, tasks and undelivered artifacts before mutation.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid
from xml.etree import ElementTree

from .docker import SANDBOX_ID_PATTERN
from .project_volume_journal import MIB, ProjectVolumeError, ProjectVolumeJournal


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

    def claim(self, owner, project_id, sandbox_id, *, fence, generation=1):
        if not isinstance(sandbox_id, str) or not SANDBOX_ID_PATTERN.fullmatch(
            sandbox_id
        ):
            raise ProjectVolumeError("Invalid project VM identity")
        with self._locked(owner, project_id) as (directory, record):
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, None)
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

    def release(self, owner, project_id, sandbox_id, *, fence, generation=1):
        if not isinstance(sandbox_id, str) or not SANDBOX_ID_PATTERN.fullmatch(
            sandbox_id
        ):
            raise ProjectVolumeError("Invalid project VM identity")
        with self._locked(owner, project_id) as (directory, record):
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, None)
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
            return self._finish(directory, record)

    def grow(self, owner, project_id, *, quota_bytes, fence, generation=1):
        quota_bytes = self._quota(quota_bytes)
        with self._locked(owner, project_id) as (directory, record):
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, None)
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

    def delete(self, owner, project_id, *, fence, generation=1):
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project disk does not exist")
            self._generation(record, generation)
            self._require_reservation(record, None)
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
