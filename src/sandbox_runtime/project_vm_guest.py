"""Standalone root helper installed inside project VMs, with no shell input.

Only the lifecycle host may call mount/trim/unmount after quiescing writers.
Normal sandbox commands run as uid 1000 and cannot change this helper or config.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import uuid

DEVICE = Path("/dev/vdb")
WORKSPACE = Path("/workspace")
CONFIG_PATH = Path("/etc/gaoji-project.json")
HELPER_PATH = "/usr/local/lib/gaoji/project_disk.py"


class GuestDiskError(ValueError):
    pass


def disk_serial(project_id, generation, filesystem_uuid):
    identity = f"{project_id}:{generation}:{filesystem_uuid}"
    return "gp" + hashlib.sha256(identity.encode()).hexdigest()[:18]


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {
        "project_id",
        "generation",
        "filesystem_uuid",
        "quota_bytes",
        "serial",
        "initialize",
    }:
        raise GuestDiskError("Invalid project disk configuration")
    if any(
        type(config[key]) is not int or not 1 <= config[key] < 2**53
        for key in ("project_id", "generation")
    ):
        raise GuestDiskError("Invalid project identity")
    value = config["filesystem_uuid"]
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise GuestDiskError("Invalid project filesystem identity") from None
    quota = config["quota_bytes"]
    if (
        type(quota) is not int
        or not 64 * 1024**2 <= quota <= 100 * 1024**3
        or quota % 1024**2
    ):
        raise GuestDiskError("Invalid project quota")
    if type(config["initialize"]) is not bool or config["serial"] != disk_serial(
        config["project_id"], config["generation"], value
    ):
        raise GuestDiskError("Invalid project disk serial or initialization permission")
    return dict(config)


class LinuxGuestDisk:
    @staticmethod
    def run(*args, accepted=(0,), timeout=60):
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
            raise GuestDiskError(
                f"Project disk command unavailable: {args[0]}"
            ) from None
        if result.returncode not in accepted:
            raise GuestDiskError(
                f"Project disk command failed: {args[0]} ({result.returncode})"
            )
        return result.stdout

    def device(self):
        info = DEVICE.lstat()
        if not stat.S_ISBLK(info.st_mode):
            raise GuestDiskError("Expected a real project block device")
        return {
            "serial": Path("/sys/class/block/vdb/serial").read_text().strip(),
            "bytes": int(self.run("blockdev", "--getsize64", str(DEVICE)).strip()),
            "number": f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}",
        }

    def mounts(self):
        data = json.loads(
            self.run(
                "findmnt",
                "--json",
                "--list",
                "--output",
                "SOURCE,TARGET,FSTYPE,OPTIONS,FSROOT,MAJ:MIN",
            )
        )
        if not isinstance(data, dict) or not isinstance(data.get("filesystems"), list):
            raise GuestDiskError("Cannot read guest mount inventory")
        return data["filesystems"]

    def filesystem(self):
        output = self.run("blkid", "-p", "-o", "export", str(DEVICE), accepted=(0, 2))
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def filesystem_bytes(self):
        output = self.run("dumpe2fs", "-h", str(DEVICE))
        values = dict(line.split(":", 1) for line in output.splitlines() if ":" in line)
        try:
            blocks, size = int(values["Block count"]), int(values["Block size"])
        except (KeyError, ValueError):
            raise GuestDiskError("Cannot read project filesystem geometry") from None
        if blocks <= 0 or size <= 0:
            raise GuestDiskError("Invalid project filesystem geometry")
        return blocks * size

    def require_blank(self, quota):
        deadline = time.monotonic() + 60
        fd = os.open(DEVICE, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            received = 0
            while received < quota:
                if time.monotonic() > deadline:
                    raise GuestDiskError(
                        "Project disk blank check timed out; no format was attempted"
                    )
                block = os.read(fd, min(1024**2, quota - received))
                if not block or block != bytes(len(block)):
                    raise GuestDiskError(
                        "Project disk is not entirely blank; refusing to format"
                    )
                received += len(block)
        finally:
            os.close(fd)

    def format(self, filesystem_uuid):
        self.run(
            "mkfs.ext4",
            "-q",
            "-F",
            "-m",
            "0",
            "-U",
            filesystem_uuid,
            str(DEVICE),
            timeout=120,
        )

    def prepare_mountpoint(self):
        if not WORKSPACE.exists() and not WORKSPACE.is_symlink():
            WORKSPACE.mkdir(mode=0o700)
        info = WORKSPACE.lstat()
        if not stat.S_ISDIR(info.st_mode) or any(WORKSPACE.iterdir()):
            raise GuestDiskError(
                "Unmounted workspace contains data or is not a directory"
            )
        # A failed mount must never leave a writable system-disk fallback.
        os.chown(WORKSPACE, 0, 0)
        os.chmod(WORKSPACE, 0o700)

    def mount(self):
        self.run("e2fsck", "-p", str(DEVICE), accepted=(0, 1), timeout=120)
        self.run(
            "mount", "-t", "ext4", "-o", "nosuid,nodev", str(DEVICE), str(WORKSPACE)
        )

    def initialize_owner(self):
        os.chown(WORKSPACE, 1000, 1000)
        os.chmod(WORKSPACE, 0o700)

    def usage(self):
        fd = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info, usage = os.fstat(fd), os.fstatvfs(fd)
            if (
                info.st_uid != 1000
                or info.st_gid != 1000
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise GuestDiskError(
                    "Project filesystem ownership differs from expected sandbox user"
                )
            return {
                "number": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
                "available_bytes": usage.f_bavail * usage.f_frsize,
            }
        finally:
            os.close(fd)

    def trim(self):
        self.run("sync", "-f", str(WORKSPACE))
        self.run("fstrim", str(WORKSPACE), timeout=120)

    def unmount(self):
        self.run("sync", "-f", str(WORKSPACE))
        self.run("umount", str(WORKSPACE))


class ProjectGuestWorkspace:
    def __init__(self, config, *, host=None):
        self.config = validate_config(config)
        self.host = host or LinuxGuestDisk()

    def _device(self):
        device = self.host.device()
        if (
            device.get("serial") != self.config["serial"]
            or type(device.get("bytes")) is not int
            or device["bytes"] != self.config["quota_bytes"]
            or not isinstance(device.get("number"), str)
            or ":" not in device["number"]
        ):
            raise GuestDiskError(
                "Project disk serial or capacity differs from the host configuration"
            )
        return device

    def _mount(self, device):
        matches = []
        for mount in self.host.mounts():
            if not isinstance(mount, dict):
                raise GuestDiskError("Invalid guest mount inventory")
            if (
                mount.get("target") == str(WORKSPACE)
                or mount.get("maj:min") == device["number"]
            ):
                matches.append(mount)
        if not matches:
            return None
        if len(matches) != 1:
            raise GuestDiskError("Project disk has multiple guest mount consumers")
        mount = matches[0]
        if (
            mount.get("target") != str(WORKSPACE)
            or mount.get("maj:min") != device["number"]
            or mount.get("fsroot") != "/"
            or mount.get("fstype") != "ext4"
            or not {"rw", "nosuid", "nodev"} <= set(mount.get("options", "").split(","))
        ):
            raise GuestDiskError("Workspace is not the expected isolated project mount")
        return mount

    def _filesystem(self):
        data = self.host.filesystem()
        if (
            data.get("TYPE") != "ext4"
            or data.get("UUID") != self.config["filesystem_uuid"]
        ):
            raise GuestDiskError(
                "Project filesystem UUID or type differs from its journal"
            )
        if self.host.filesystem_bytes() != self.config["quota_bytes"]:
            raise GuestDiskError("Project filesystem capacity differs from its quota")

    def verify(self):
        device = self._device()
        if self._mount(device) is None:
            raise GuestDiskError(
                "Project disk is not mounted; refusing a system-disk workspace"
            )
        self._filesystem()
        usage = self.host.usage()
        available = usage.get("available_bytes")
        if (
            usage.get("number") != device["number"]
            or type(available) is not int
            or not 0 <= available <= self.config["quota_bytes"]
        ):
            raise GuestDiskError("Workspace accounting belongs to another filesystem")
        return {
            "project_id": self.config["project_id"],
            "generation": self.config["generation"],
            "filesystem_uuid": self.config["filesystem_uuid"],
            "quota_bytes": self.config["quota_bytes"],
            "serial": self.config["serial"],
            "mounted": True,
            "verified": True,
            "used_bytes": self.config["quota_bytes"] - available,
        }

    def mount(self):
        device = self._device()
        if self._mount(device) is not None:
            self._filesystem()
            self.host.initialize_owner()
            return self.verify()
        self.host.prepare_mountpoint()
        data = self.host.filesystem()
        if not data:
            if not self.config["initialize"]:
                raise GuestDiskError(
                    "Project filesystem is missing; no initialization was authorized"
                )
            self.host.require_blank(self.config["quota_bytes"])
            self.host.format(self.config["filesystem_uuid"])
        self._filesystem()
        self.host.mount()
        if self._mount(device) is None:
            raise GuestDiskError("Project mount was not observed after mount command")
        self.host.initialize_owner()
        return self.verify()

    def trim(self):
        self.verify()
        self.host.trim()
        return {**self.verify(), "discard_completed": True}

    def unmount(self):
        device = self._device()
        if self._mount(device) is not None:
            self.verify()
            self.host.unmount()
        if self._mount(device) is not None:
            raise GuestDiskError("Project mount is still present after unmount")
        self._filesystem()
        self.host.prepare_mountpoint()
        return {
            "project_id": self.config["project_id"],
            "generation": self.config["generation"],
            "filesystem_uuid": self.config["filesystem_uuid"],
            "mounted": False,
            "verified": True,
        }


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("mount", "verify", "trim", "unmount"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise GuestDiskError(
            "Project workspace management requires the guest administrator"
        )
    fd = os.open(
        "/run/lock/gaoji-project-disk.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "rb") as lock:
        info = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise GuestDiskError("Unsafe guest project lock")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GuestDiskError(
                        "Guest project disk management is busy"
                    ) from None
                time.sleep(0.05)
        fd = os.open(CONFIG_PATH, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > 8192
            ):
                raise GuestDiskError("Unsafe project guest configuration")
            config = json.load(stream)
        result = getattr(ProjectGuestWorkspace(config), args.action)()
        if args.action == "mount" and config["initialize"]:
            seal_initialization(config)
        print(json.dumps(result, sort_keys=True))


def seal_initialization(config):
    """Consume first-boot format permission before acknowledging a mounted disk."""
    fd, temporary = tempfile.mkstemp(prefix=".gaoji-project-", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({**config, "initialize": False}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG_PATH)
        directory = os.open(CONFIG_PATH.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main()
