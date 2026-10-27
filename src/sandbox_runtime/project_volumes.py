"""Host-owned sparse ext4 volumes; no caller-controlled paths or devices.

This module belongs in the privileged Linux runner, never in a user sandbox.
Every mutation is fenced and journalled before invoking filesystem utilities.
Application lifecycle guards must separately protect tasks and undelivered data.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import uuid

from src.local_runtime.files import file_lock, read_json, write_json
from src.project_content import MAX_MANIFEST_BYTES
from .docker import SandboxError

MIB = 1024**2
MIN_QUOTA = 64 * MIB
MAX_QUOTA = 100 * 1024**3
_DEVICE = re.compile(r"/dev/loop[0-9]+")


class ProjectVolumeError(SandboxError):
    pass


class LinuxVolumeHost:
    """Fixed host commands only. Constructor configuration is operator-owned."""

    def __init__(self, *, docker="docker", docker_host="unix:///var/run/docker.sock"):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ProjectVolumeError("Native project quota helper requires Linux root")
        if docker != "docker" and not Path(docker).is_absolute():
            raise ValueError("Docker executable must be absolute")
        if not re.fullmatch(r"unix:///[^\x00-\x20]+", docker_host):
            raise ValueError("A local Docker socket is required")
        self.docker, self.docker_host = docker, docker_host

    @staticmethod
    def run(*args, accepted=(0,), timeout=120):
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode not in accepted:
            raise ProjectVolumeError(
                f"Project volume utility failed: {Path(args[0]).name} ({result.returncode})"
            )
        return result.stdout

    def format(self, image, fs_uuid):
        self.run("mkfs.ext4", "-q", "-F", "-m", "0", "-U", fs_uuid, str(image))

    def boot_id(self):
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if str(uuid.UUID(value)) != value:
                raise ValueError("Noncanonical boot identity")
        except (OSError, ValueError):
            raise ProjectVolumeError("Cannot identify the host boot") from None
        return value

    def filesystem(self, image):
        result = self.run("blkid", "-p", "-o", "export", str(image), accepted=(0, 2))
        return dict(line.split("=", 1) for line in result.splitlines() if "=" in line)

    def filesystem_bytes(self, image):
        result = self.run("dumpe2fs", "-h", str(image))
        values = dict(line.split(":", 1) for line in result.splitlines() if ":" in line)
        try:
            return int(values["Block count"].strip()) * int(
                values["Block size"].strip()
            )
        except (KeyError, ValueError):
            raise ProjectVolumeError("Cannot verify ext4 capacity") from None

    def used_bytes(self, image, root, quota):
        if root is not None:
            usage = os.fstatvfs(root)
            available = usage.f_bavail * usage.f_frsize
        else:
            result = self.run("dumpe2fs", "-h", str(image), timeout=10)
            values = dict(line.split(":", 1) for line in result.splitlines() if ":" in line)
            try:
                if values["Filesystem state"].strip() != "clean":
                    raise ValueError("Unclean filesystem")
                blocks, size, free = (int(values[key].strip()) for key in ("Block count", "Block size", "Free blocks"))
                reserved = int(values["Reserved block count"].strip())
                if size <= 0 or blocks * size != quota or not 0 <= free <= blocks or not 0 <= reserved <= blocks:
                    raise ValueError("Invalid filesystem geometry")
                available = max(0, free - reserved) * size
            except (KeyError, ValueError):
                raise ProjectVolumeError("Cannot measure offline ext4 usage") from None
        if not 0 <= available <= quota:
            raise ProjectVolumeError("Filesystem available space differs from project quota")
        return quota - available

    def loops(self):
        result = self.run(
            "losetup",
            "--json",
            "--list",
            "--output",
            "NAME,BACK-FILE,BACK-INO,BACK-MAJ:MIN,OFFSET,SIZELIMIT,RO",
        )
        return json.loads(result).get("loopdevices", [])

    def mounts(self):
        result = self.run(
            "findmnt",
            "--json",
            "--list",
            "--output",
            "TARGET,SOURCE,FSTYPE,OPTIONS,FSROOT,MAJ:MIN",
        )
        return json.loads(result).get("filesystems", [])

    def attach(self, image):
        device = self.run(
            "losetup", "--find", "--show", "--nooverlap", str(image)
        ).strip()
        if not _DEVICE.fullmatch(device):
            raise ProjectVolumeError("Unexpected loop device identity")
        return device

    def detach(self, device):
        self.run("losetup", "--detach", device)

    def mount(self, device, target):
        self.run("mount", "-t", "ext4", "-o", "nosuid,nodev", device, str(target))

    def unmount(self, target):
        # Never lazy-unmount or force-detach a filesystem still held by a writer.
        self.run("umount", str(target))

    def device_number(self, device):
        info = os.stat(device)
        if not stat.S_ISBLK(info.st_mode):
            raise ProjectVolumeError("Loop device is not a block device")
        return f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"

    def in_use(self, target):
        prefix = (self.docker, "--host=" + self.docker_host)
        # A stopped container can restart with its old bind mount. Its reference
        # must be removed before a volume can be detached or deleted.
        ids = self.run(*prefix, "ps", "-a", "--no-trunc", "-q").split()
        if len(ids) > 256 or any(
            not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids
        ):
            raise ProjectVolumeError("Cannot safely enumerate project consumers")
        if not ids:
            return False
        result = self.run(
            *prefix,
            "inspect",
            "--type",
            "container",
            "--format",
            "{{json .Mounts}}",
            *ids,
        )
        lines = result.splitlines()
        if len(lines) != len(ids):
            raise ProjectVolumeError("Incomplete container mount inventory")
        target = Path(target)
        for line in lines:
            for mount in json.loads(line):
                source = Path(mount.get("Source", ""))
                if source.is_absolute() and (
                    source == target
                    or source.is_relative_to(target)
                    or target.is_relative_to(source)
                ):
                    return True
        return False

    def initialize_owner(self, target):
        os.chown(target, 1000, 1000)
        os.chmod(target, 0o700)

    def resize(self, image_or_device, quota):
        self.run("resize2fs", str(image_or_device), f"{quota // 1024}K", timeout=300)

    def refresh_capacity(self, device):
        self.run("losetup", "--set-capacity", device)

    def fsck(self, image):
        # 1 means errors corrected. Reboot-needed or uncorrected states are not success.
        self.run("e2fsck", "-p", str(image), accepted=(0, 1), timeout=300)

    def trim(self, target):
        self.run("fstrim", str(target))


class ProjectVolumes:
    """Private runner journal and fixed-operation host filesystem adapter.

    Fence numbers increase per project *operation*, not per transient RPC retry.
    Retrying the same fence requires exactly the same operation and arguments.
    A new fence cannot overtake an unfinished filesystem mutation.
    """

    def __init__(self, root: Path, instance_id: str, *, host=None, capacity_check=None):
        if not re.fullmatch(r"[a-f0-9]{32}", instance_id):
            raise ValueError("Invalid project-volume instance")
        self.root, self.instance_id = root.absolute(), instance_id
        self.host = host or LinuxVolumeHost()
        self.capacity_check = capacity_check
        self._private_directory(self.root)
        self.root = self.root.resolve()

    @staticmethod
    def _private_directory(path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ProjectVolumeError(
                "Project-volume directory must be private and runner-owned"
            )
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _quota(value):
        if type(value) is not int or not MIN_QUOTA <= value <= MAX_QUOTA or value % MIB:
            raise ProjectVolumeError(
                "Project quota must be 64 MiB to 100 GiB, aligned to MiB"
            )
        return value

    @staticmethod
    def _generation(record, generation):
        if type(generation) is not int or not 1 <= generation < 2**53:
            raise ProjectVolumeError("Invalid workspace generation")
        if record is not None and record.get("generation", 1) != generation:
            raise ProjectVolumeError("Workspace generation was superseded")

    @staticmethod
    def _reservation_hash(reservation):
        if not isinstance(reservation, str) or not re.fullmatch(
            r"[a-f0-9]{32}", reservation
        ):
            raise ProjectVolumeError("Invalid project maintenance reservation")
        return hashlib.sha256(reservation.encode()).hexdigest()

    @classmethod
    def _require_reservation(cls, record, reservation):
        expected = record.get("maintenance") if record else None
        supplied = (
            cls._reservation_hash(reservation) if reservation is not None else None
        )
        if expected != supplied:
            raise ProjectVolumeError("Project is reserved by a lifecycle operation")

    @contextmanager
    def _locked(self, owner, project_id):
        if (
            not isinstance(owner, str)
            or not owner.strip()
            or len(owner) > 200
            or "\0" in owner
            or type(project_id) is not int
            or not 1 <= project_id < 2**53
        ):
            raise ProjectVolumeError("Invalid project-volume owner or ID")
        with file_lock(self.root / ".volumes.lock", timeout=15):
            directory = self.root / f"p{project_id}"
            self._private_directory(directory)
            record_path = directory / "volume.json"
            record = None
            if record_path.exists() or record_path.is_symlink():
                info = record_path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size > 16384
                ):
                    raise ProjectVolumeError("Unsafe project-volume journal")
                record = read_json(record_path)
                if (
                    record.get("schema") != 1
                    or record.get("instance_id") != self.instance_id
                    or record.get("project_id") != project_id
                    or record.get("owner_hash")
                    != hashlib.sha256(owner.encode()).hexdigest()
                ):
                    raise ProjectVolumeError(
                        "Project volume belongs to another owner or instance"
                    )
            yield directory, record

    def _save(self, directory, record):
        write_json(directory / "volume.json", record)

    @staticmethod
    def _paths(directory, record):
        fs_uuid = record.get("fs_uuid", "")
        try:
            valid = isinstance(fs_uuid, str) and str(uuid.UUID(fs_uuid)) == fs_uuid
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise ProjectVolumeError("Invalid filesystem identity")
        return directory / f"{fs_uuid}.ext4", directory / "mount"

    def _image(self, directory, record):
        image, target = self._paths(directory, record)
        info = image.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ProjectVolumeError("Unsafe project backing image")
        if record.get("image_identity") != [info.st_dev, info.st_ino]:
            raise ProjectVolumeError("Project backing image identity changed")
        return image, target, info

    def _observed(self, directory, record, *, require_filesystem=True):
        image, target, info = self._image(directory, record)
        if target.is_symlink():
            raise ProjectVolumeError("Project mount target is a symlink")
        backing_device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
        loops = [
            row
            for row in self.host.loops()
            if row.get("back-file", "").removesuffix(" (deleted)") == str(image)
            or (
                row.get("back-maj:min") == backing_device
                and row.get("back-ino") in (info.st_ino, str(info.st_ino))
            )
        ]
        if len(loops) > 1:
            raise ProjectVolumeError("Multiple loop mappings refer to the same project")
        device = None
        if loops:
            row = loops[0]
            device = row.get("name", "")
            if (
                not _DEVICE.fullmatch(device)
                or row.get("back-file") != str(image)
                or int(row.get("back-ino", -1)) != info.st_ino
                or row.get("back-maj:min") != backing_device
                or int(row.get("offset", -1)) != 0
                or int(row.get("sizelimit", -1)) != 0
                or row.get("ro") not in (False, 0)
            ):
                raise ProjectVolumeError("Loop backing identity or geometry changed")
        observed_mount = None
        number = self.host.device_number(device) if device else None
        for mount in self.host.mounts():
            same_target = mount.get("target") == str(target)
            same_device = number is not None and mount.get("maj:min") == number
            if same_target or same_device:
                if (
                    observed_mount is not None
                    or not same_target
                    or not same_device
                    or mount.get("fstype") != "ext4"
                    or mount.get("fsroot") != "/"
                    or not {"rw", "nosuid", "nodev"}
                    <= set(mount.get("options", "").split(","))
                ):
                    raise ProjectVolumeError(
                        "Project filesystem mounted at an unexpected location or with unsafe options"
                    )
                observed_mount = mount
        if require_filesystem:
            filesystem = self.host.filesystem(image)
            if (
                filesystem.get("TYPE") != "ext4"
                or filesystem.get("UUID") != record["fs_uuid"]
            ):
                raise ProjectVolumeError("Project filesystem identity changed")
            if (
                not record["pending"]
                and self.host.filesystem_bytes(image) != record["quota_bytes"]
            ):
                raise ProjectVolumeError(
                    "Project filesystem capacity differs from its journal"
                )
        return image, target, info, device, observed_mount

    def _admit(self, size):
        if self.capacity_check:
            self.capacity_check(size)

    def _begin(self, directory, record, operation, fence, arguments):
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise ProjectVolumeError("Invalid project operation fence")
        intent = {"operation": operation, "arguments": arguments}
        if fence < record["fence"]:
            raise ProjectVolumeError("Stale project operation fence")
        if fence == record["fence"]:
            if record.get("intent") != intent:
                raise ProjectVolumeError(
                    "Project fence was already used for different arguments"
                )
            return record["pending"]
        if record["pending"]:
            raise ProjectVolumeError(
                "Previous project-volume operation must be reconciled first"
            )
        record.update(fence=fence, intent=intent, pending=True)
        self._save(directory, record)
        return True

    def _finish(self, directory, record):
        record["pending"] = False
        result = self._status(directory, record)
        self._save(directory, record)
        return result

    def _status(self, directory, record):
        if record.get("deleted"):
            return {
                "project_id": record["project_id"],
                "generation": record.get("generation", 1),
                "fence": record["fence"],
                "deleted": True,
                "maintenance_active": bool(record.get("maintenance")),
            }
        image, target, info, device, mount = self._observed(directory, record)
        if not record["pending"] and info.st_size != record["quota_bytes"]:
            raise ProjectVolumeError("Project image capacity differs from its journal")
        result = {
            "project_id": record["project_id"],
            "generation": record.get("generation", 1),
            "fence": record["fence"],
            "quota_mode": "hard",
            "quota_bytes": record["quota_bytes"],
            "allocated_bytes": info.st_blocks * 512,
            "mounted": mount is not None,
            "attached": device is not None,
            "pending": record["pending"],
            "maintenance_active": bool(record.get("maintenance")),
            "pending_operation": record["intent"]["operation"]
            if record["pending"]
            else None,
            "sandbox_id": (record.get("workspace") or {}).get("sandbox_id"),
        }
        if mount:
            usage = os.statvfs(target)
            result["filesystem_used_bytes"] = (
                usage.f_blocks - usage.f_bfree
            ) * usage.f_frsize
        return result

    def status(self, owner, project_id, *, generation=1):
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project volume does not exist")
            self._generation(record, generation)
            return self._status(directory, record)

    def create(self, owner, project_id, *, quota_bytes, fence, generation=1):
        quota_bytes = self._quota(quota_bytes)
        self._generation(None, generation)
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise ProjectVolumeError("Invalid project operation fence")
        with self._locked(owner, project_id) as (directory, record):
            previous_fence = record["fence"] if record else 0
            self._require_reservation(record, None)
            replacing = record is not None and record.get("deleted", False)
            if replacing:
                if (
                    generation != record.get("generation", 1) + 1
                    or fence <= previous_fence
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
                        "Unrecognized data exists in project volume directory"
                    )
                record = {
                    "schema": 1,
                    "instance_id": self.instance_id,
                    "project_id": project_id,
                    "generation": generation,
                    "owner_hash": hashlib.sha256(owner.encode()).hexdigest(),
                    "fs_uuid": str(uuid.uuid4()),
                    "quota_bytes": quota_bytes,
                    "image_identity": None,
                    "fence": previous_fence,
                    "pending": False,
                    "intent": None,
                    "initialized": False,
                }
                # Publish the new incarnation and its first intent atomically.
                # The previous generation's high-water fence is never reset.
            if record.get("deleted") or (
                record["intent"] and record["intent"]["operation"] != "create"
            ):
                raise ProjectVolumeError("Project volume already exists or was deleted")
            if record["quota_bytes"] != quota_bytes:
                raise ProjectVolumeError(
                    "Cannot change capacity during volume creation"
                )
            if not self._begin(
                directory, record, "create", fence, {"quota_bytes": quota_bytes}
            ):
                return self._status(directory, record)
            image, target = self._paths(directory, record)
            if not image.exists() and not image.is_symlink():
                if record["image_identity"] is not None:
                    raise ProjectVolumeError(
                        "Project image was lost; not creating an empty replacement"
                    )
                fd = os.open(
                    image, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                try:
                    os.ftruncate(fd, quota_bytes)
                    os.fsync(fd)
                    info = os.fstat(fd)
                    record["image_identity"] = [info.st_dev, info.st_ino]
                finally:
                    os.close(fd)
                self._save(directory, record)
            elif record["image_identity"] is None:
                info = image.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size != quota_bytes
                ):
                    raise ProjectVolumeError(
                        "Interrupted image creation cannot be verified"
                    )
                record["image_identity"] = [info.st_dev, info.st_ino]
                self._save(directory, record)
            image, target, info, device, mount = self._observed(
                directory, record, require_filesystem=False
            )
            if device or mount:
                raise ProjectVolumeError("Cannot initialize an attached project image")
            filesystem = self.host.filesystem(image)
            if not filesystem and info.st_blocks == 0:
                self.host.format(image, record["fs_uuid"])
            elif (
                filesystem.get("TYPE") == "ext4"
                and filesystem.get("UUID") == record["fs_uuid"]
            ):
                self.host.fsck(image)
            else:
                raise ProjectVolumeError(
                    "Interrupted format requires inspection; refusing to overwrite data"
                )
            self._observed(directory, record)
            return self._finish(directory, record)

    def _idle(self, target):
        if self.host.in_use(target):
            raise ProjectVolumeError("Project has an active filesystem consumer")

    def mutate(
        self,
        owner,
        project_id,
        operation,
        *,
        fence,
        quota_bytes=None,
        generation=1,
        reservation=None,
    ):
        if operation not in {"mount", "unmount", "resize", "fsck", "trim", "delete"}:
            raise ProjectVolumeError("Unsupported project-volume operation")
        arguments = (
            {"quota_bytes": self._quota(quota_bytes)} if operation == "resize" else {}
        )
        if operation != "resize" and quota_bytes is not None:
            raise ProjectVolumeError("Unexpected project capacity argument")
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project volume does not exist")
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record.get("deleted"):
                if operation == "delete" and fence == record["fence"]:
                    return self._status(directory, record)
                raise ProjectVolumeError("Deleted volume cannot be reused")
            if record.get("workspace"):
                raise ProjectVolumeError(
                    "Remove the project workspace before volume maintenance"
                )
            if record["pending"] and record["intent"]["operation"] == "delete":
                self._begin(directory, record, operation, fence, arguments)
                self._delete(directory, record)
                return self._finish(directory, record)
            observed = self._observed(directory, record)
            image, target, info, device, mount = observed
            if operation not in {"mount", "resize"}:
                self._idle(target)
            if operation in {"fsck", "delete"} and (mount or device):
                raise ProjectVolumeError("Project volume must be fully detached first")
            if operation == "trim" and not mount:
                raise ProjectVolumeError("Trim requires a mounted, idle project")
            if operation == "resize" and quota_bytes < record["quota_bytes"]:
                self._idle(target)
                if mount or device:
                    raise ProjectVolumeError(
                        "Shrinking a project requires an offline detached filesystem"
                    )
            if operation in {"mount", "resize"}:
                self._admit(min(quota_bytes or record["quota_bytes"], 256 * MIB))
            if not self._begin(directory, record, operation, fence, arguments):
                return self._status(directory, record)
            if operation == "mount":
                self._mount(directory, record)
            elif operation == "unmount":
                if mount:
                    self.host.unmount(target)
                _, _, _, device, mount = self._observed(directory, record)
                if mount:
                    raise ProjectVolumeError("Unmount did not complete")
                if device:
                    self.host.detach(device)
                if self._observed(directory, record)[3] is not None:
                    raise ProjectVolumeError("Loop detach did not complete")
            elif operation == "resize":
                self._resize(directory, record, observed, quota_bytes)
            elif operation == "fsck":
                self.host.fsck(image)
                self._observed(directory, record)
            elif operation == "trim":
                self.host.trim(target)
            else:
                self._delete(directory, record)
            return self._finish(directory, record)

    def _mount(self, directory, record):
        image, target, _, device, mount = self._observed(directory, record)
        if mount is None:
            self._private_directory(target)
            if any(target.iterdir()):
                raise ProjectVolumeError("Mount would hide unexpected project files")
            if device is None:
                self.host.attach(image)
            _, _, _, device, mount = self._observed(directory, record)
            if not device:
                raise ProjectVolumeError("Loop attachment not confirmed")
            self.host.mount(device, target)
        if self._observed(directory, record)[4] is None:
            raise ProjectVolumeError("Project mount was not observed")
        if not record["initialized"]:
            self.host.initialize_owner(target)
            record["initialized"] = True

    def _delete(self, directory, record):
        image, target = self._paths(directory, record)
        self._idle(target)
        if image.exists() or image.is_symlink():
            _, _, _, device, mount = self._observed(directory, record)
            if device or mount:
                raise ProjectVolumeError("Project volume must be fully detached first")
        elif any(
            row.get("back-file", "").removesuffix(" (deleted)") == str(image)
            for row in self.host.loops()
        ) or any(row.get("target") == str(target) for row in self.host.mounts()):
            raise ProjectVolumeError(
                "Deleted image is still attached; manual inspection required"
            )
        journals = []
        for name, limit in (
            ("files.json", MAX_MANIFEST_BYTES * 2),
            ("prune.json", MAX_MANIFEST_BYTES * 2),
            ("dependency-run.json", 32768),
        ):
            path = directory / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > limit
            ):
                raise ProjectVolumeError("Unsafe retired project journal")
            document = read_json(path)
            if (
                not isinstance(document, dict)
                or document.get("generation") != record["generation"]
                or type(document.get("fence")) is not int
                or not 0 < document["fence"] < record["fence"]
            ):
                raise ProjectVolumeError("Retired journal belongs to another operation")
            journals.append(path)
        if target.exists():
            self._private_directory(target)
            target.rmdir()
        image.unlink(missing_ok=True)
        for path in journals:
            path.unlink()
        record["deleted"] = True

    def _resize(self, directory, record, observed, quota):
        image, target, info, device, mount = observed
        if quota < record["quota_bytes"]:
            self.host.fsck(image)
            self.host.resize(image, quota)
            if self.host.filesystem_bytes(image) > quota:
                raise ProjectVolumeError(
                    "Filesystem still exceeds requested capacity; image was not truncated"
                )
        elif quota > info.st_size:
            self._truncate(image, quota)
        if quota >= record["quota_bytes"]:
            if device:
                self.host.refresh_capacity(device)
            self.host.resize(device or image, quota)
        if self.host.filesystem_bytes(image) != quota:
            raise ProjectVolumeError("Filesystem capacity verification failed")
        if image.stat().st_size != quota:
            self._truncate(image, quota)
        record["quota_bytes"] = quota

    @staticmethod
    def _truncate(image, size):
        fd = os.open(image, os.O_RDWR | os.O_NOFOLLOW)
        try:
            os.ftruncate(fd, size)
            os.fsync(fd)
        finally:
            os.close(fd)

    def binding(self, owner, project_id, *, generation=1):
        """Trusted local Docker driver only; never expose this path in RPC responses."""
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, None)
            if record is None or record.get("deleted") or record["pending"]:
                raise ProjectVolumeError("Project volume is not ready for a writer")
            _, target, _, _, mount = self._observed(directory, record)
            if mount is None:
                raise ProjectVolumeError("Project volume is not mounted")
            return target

    def workspace(
        self,
        owner,
        project_id,
        *,
        generation,
        allow_pending=False,
        maintenance=False,
        reservation=None,
    ):
        """Private local driver data. Never send this binding over the RPC socket."""
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project workspace is unavailable")
            if record["pending"] and not allow_pending:
                raise ProjectVolumeError("Project workspace is undergoing maintenance")
            if not maintenance:
                self._require_reservation(record, reservation)
            return self._workspace_binding(directory, record, maintenance=maintenance)

    def _workspace_binding(self, directory, record, *, maintenance=False):
        _, target, _, _, mount = self._observed(directory, record)
        workspace = record.get("workspace")
        matches = bool(mount)
        boot_id = self.host.boot_id()
        if workspace:
            identity = target.stat() if mount else None
            matches = (
                identity is not None
                and workspace["root_identity"] == f"{identity.st_dev}:{identity.st_ino}"
                and workspace.get("boot_id", boot_id) == boot_id
            )
            if not matches and not maintenance:
                raise ProjectVolumeError(
                    "Project workspace mount identity changed; recovery is required"
                )
        return {
            "workspace": workspace,
            "path": str(target),
            "project_id": record["project_id"],
            "generation": record.get("generation", 1),
            "fence": record["fence"],
            "pending": record["pending"],
            "mounted": mount is not None,
            "mount_matches": matches,
            "boot_id": boot_id,
            "maintenance_active": bool(record.get("maintenance")),
        }

    def reserve(self, owner, project_id, *, generation, fence, action, reservation):
        """Cross-step exclusion; only the allocation-locked driver may call this."""
        digest = self._reservation_hash(reservation)
        if action not in {"reserve", "release"}:
            raise ProjectVolumeError("Unsupported project reservation operation")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if record is None or (record.get("deleted") and action == "reserve"):
                raise ProjectVolumeError("Project volume does not exist")
            operation = "workspace_" + action
            arguments = {"reservation_hash": digest}
            held = record.get("maintenance")
            if held is not None and held != digest:
                raise ProjectVolumeError("Another lifecycle operation owns the project")
            replay = fence == record["fence"] and record["intent"] == {
                "operation": operation,
                "arguments": arguments,
            }
            if not replay and (
                (action == "reserve" and held) or (action == "release" and held is None)
            ):
                raise ProjectVolumeError("Reservation ownership cannot be inferred")
            # Publish ownership and the effect fence in one durable write.
            if not replay:
                if record["pending"]:
                    raise ProjectVolumeError("Reconcile the pending operation first")
                record["maintenance"] = digest if action == "reserve" else None
            if self._begin(directory, record, operation, fence, arguments):
                return self._finish(directory, record)
            return self._status(directory, record)

    def begin_workspace(
        self, owner, project_id, *, generation, fence, action, image, reservation=None
    ):
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise ProjectVolumeError("Invalid project operation fence")
        if action not in {"start", "stop", "remove"}:
            raise ProjectVolumeError("Unsupported project workspace operation")
        if not isinstance(image, str) or not re.fullmatch(
            r"(?:gaoji-app-sandbox:[a-f0-9]{64}|[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64})",
            image,
        ):
            raise ProjectVolumeError("A pinned project image is required")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project volume does not exist")
            self._require_reservation(record, reservation)
            binding = self._workspace_binding(directory, record, maintenance=True)
            workspace = record.get("workspace")
            recoverable = (
                workspace
                and workspace.get("boot_id")
                and workspace["boot_id"] != binding["boot_id"]
            )
            if action == "start" and not binding["mount_matches"] and not recoverable:
                raise ProjectVolumeError(
                    "Mount the project before starting a workspace"
                )
            operation = "workspace_" + action
            if fence == record["fence"]:
                intent = record["intent"]
                if intent["operation"] != operation:
                    raise ProjectVolumeError("Project fence was already used")
                # A restart may have a newer configured image; the persisted
                # operation still reconciles its original immutable image.
                self._begin(directory, record, operation, fence, intent["arguments"])
                return self._workspace_binding(directory, record, maintenance=True)
            if record["pending"] or fence < record["fence"]:
                raise ProjectVolumeError(
                    "Previous project operation must be reconciled first"
                )
            if action == "start" and not record.get("workspace"):
                info = Path(binding["path"]).stat()
                record["workspace"] = {
                    "sandbox_id": "s" + secrets.token_hex(3),
                    "token": secrets.token_hex(16),
                    "image": image,
                    "root_identity": f"{info.st_dev}:{info.st_ino}",
                    "boot_id": binding["boot_id"],
                }
            self._begin(
                directory,
                record,
                operation,
                fence,
                {"workspace": record.get("workspace")},
            )
            return self._workspace_binding(directory, record, maintenance=True)

    def recover_workspace(
        self, owner, project_id, *, generation, fence, reservation=None
    ):
        """Rematerialize a stopped, removed binding after a verified host reboot.

        Docker removal is checked by the driver and independently by _idle.
        The existing start fence remains pending throughout recovery.
        """
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise ProjectVolumeError("Invalid workspace recovery fence")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if (
                record is None
                or record.get("deleted")
                or not record["pending"]
                or record["fence"] != fence
                or record["intent"]["operation"] != "workspace_start"
            ):
                raise ProjectVolumeError(
                    "No pending start authorizes workspace recovery"
                )
            binding = self._workspace_binding(directory, record, maintenance=True)
            previous = record.get("workspace")
            if (
                not previous
                or not previous.get("boot_id")
                or previous["boot_id"] == binding["boot_id"]
            ):
                raise ProjectVolumeError(
                    "Workspace recovery requires a different verified host boot"
                )
            image, target, info, device, mount = self._observed(directory, record)
            self._idle(target)
            if (
                info.st_size != record["quota_bytes"]
                or self.host.filesystem_bytes(image) != record["quota_bytes"]
            ):
                raise ProjectVolumeError("Recovery cannot change project capacity")
            if mount is None:
                self.host.fsck(device or image)
            self._mount(directory, record)
            identity = target.stat()
            record["workspace"] = {
                "sandbox_id": "s" + secrets.token_hex(3),
                "token": secrets.token_hex(16),
                "image": previous["image"],
                "root_identity": f"{identity.st_dev}:{identity.st_ino}",
                "boot_id": binding["boot_id"],
                "recovered_from": previous["sandbox_id"],
            }
            self._save(directory, record)
            return self._workspace_binding(directory, record)

    def finish_workspace(
        self, owner, project_id, *, generation, fence, action, reservation=None
    ):
        if (
            action not in {"start", "stop", "remove"}
            or type(fence) is not int
            or not 1 <= fence < 2**53
        ):
            raise ProjectVolumeError("Invalid workspace completion")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record is None or record.get("deleted") or record["fence"] != fence:
                raise ProjectVolumeError("Workspace operation was superseded")
            if record["intent"]["operation"] != "workspace_" + action:
                raise ProjectVolumeError("Workspace operation does not match journal")
            if not record["pending"]:
                return self._status(directory, record)
            if action == "remove":
                # The driver independently verified container absence. Also
                # reject any remaining (including stopped) external consumer.
                self._idle(self._paths(directory, record)[1])
                record["workspace"] = None
            return self._finish(directory, record)
