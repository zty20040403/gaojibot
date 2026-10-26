"""Account for one instance's data, owned volumes and container writable layers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from src.storage_admission import CapacityError, DEFAULT_POLICY, MIB, check_snapshot, policy, read_snapshot


def prepare_directory(directory: Path) -> Path:
    public = directory / "capacity"
    public.mkdir(mode=0o755, exist_ok=True)
    info = public.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise CapacityError("容量状态目录归属不正确")
    # Reopening a controller must not change the tree approved for removal.
    if stat.S_IMODE(info.st_mode) != 0o755:
        public.chmod(0o755)
    return public


def allocated_bytes(root: Path, *, deadline: float, seen: set | None = None) -> int:
    """Count allocated blocks, never file contents or symlink targets."""
    seen = seen if seen is not None else set()
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    used = 0
    device = os.fstat(descriptor).st_dev
    def visit(fd):
        nonlocal used
        if time.monotonic() > deadline:
            raise CapacityError("数据文件较多，本次容量统计未完成")
        info = os.fstat(fd)
        key = (info.st_dev, info.st_ino)
        if key in seen:
            return
        if info.st_dev != device:
            raise CapacityError("实例目录含额外挂载，容量未完整统计")
        seen.add(key)
        used += getattr(info, "st_blocks", (info.st_size + 511) // 512) * 512
        with os.scandir(fd) as entries:
            for entry in entries:
                if time.monotonic() > deadline or len(seen) >= 1000000:
                    raise CapacityError("数据文件较多，本次容量统计未完成")
                try:
                    child = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(child.st_mode):
                    nested = os.open(entry.name, flags, dir_fd=fd)
                    try:
                        if (os.fstat(nested).st_dev, os.fstat(nested).st_ino) != (child.st_dev, child.st_ino):
                            raise CapacityError("扫描期间目录发生变化")
                        visit(nested)
                    finally:
                        os.close(nested)
                elif (child.st_dev, child.st_ino) not in seen:
                    seen.add((child.st_dev, child.st_ino))
                    used += getattr(child, "st_blocks", (child.st_size + 511) // 512) * 512
    try:
        visit(descriptor)
        return used
    finally:
        os.close(descriptor)


class Capacity:
    def __init__(self, controller):
        self.c = controller
        self.directory = controller.directory
        self.public = self.directory / "capacity"
        self.filename = self.public / "status.json"
        self.scanning = threading.Lock()
        self.deadline = None

    def prepare(self):
        prepare_directory(self.directory)

    def publish(self, value):
        self.prepare()
        descriptor, temporary = tempfile.mkstemp(prefix=".sample-", dir=self.public)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(temporary, self.filename)
            parent = os.open(self.public, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def command(self, *arguments):
        # Deployment fixes the local Unix socket and executable; no paths or
        # commands from a chat or the desktop request enter this interface.
        if not self.c.deployment["docker_host"].startswith("unix:///"):
            raise CapacityError("容量统计需要本机 Docker 引擎")
        remaining = 10 if self.deadline is None else min(10, self.deadline - time.monotonic())
        if remaining <= 0:
            raise CapacityError("本次容量统计超时，请稍后重新扫描")
        try:
            result = subprocess.run([self.c.deployment["docker"], *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=remaining, check=False, env=self.c.process_environment())
        except (OSError, subprocess.TimeoutExpired):
            raise CapacityError("Docker 容量统计暂不可用") from None
        if result.returncode or len(result.stdout) > 2 * 1024**2:
            raise CapacityError("Docker 容量统计暂不可用")
        return result.stdout.decode("utf-8")

    def engine_inventory(self):
        identifier = self.c.identifier
        engine_root = Path(json.loads(self.command("info", "--format", "{{json .DockerRootDir}}")))
        if not engine_root.is_absolute() or engine_root.resolve() != engine_root:
            raise CapacityError("Docker 数据目录无法确认")
        names = self.command("volume", "ls", "--filter", "label=gaoji.app.instance=" + identifier, "--format", "{{.Name}}").splitlines()
        if len(names) > 128 or len(set(names)) != len(names) or any(not re.fullmatch(r"gaoji-app-" + identifier + r"-(nix|work-s[0-9a-f]{6,32})", name) for name in names):
            raise CapacityError("实例工作卷目录无法确认")
        volumes = []
        if names:
            rows = json.loads(self.command("volume", "inspect", *names))
            if (not isinstance(rows, list) or len(rows) != len(names)
                or any(not isinstance(row, dict) or not isinstance(row.get("Name"), str) for row in rows)
                or {row["Name"] for row in rows} != set(names)):
                raise CapacityError("工作卷统计不完整")
            for row in rows:
                labels = row.get("Labels") or {}
                name = row.get("Name")
                expected_kind = "nix-cache" if name == f"gaoji-app-{identifier}-nix" else "workspace"
                expected_path = engine_root / "volumes" / str(name) / "_data"
                if (not isinstance(labels, dict) or row.get("Driver") != "local" or row.get("Options")
                    or labels.get("gaoji.app.instance") != identifier or labels.get("gaoji.app.resource") != expected_kind
                    or row.get("Mountpoint") != str(expected_path) or expected_path.resolve() != expected_path):
                    raise CapacityError("工作卷归属不正确，未读取其他数据")
                volumes.append(expected_path)
        containers = set()
        for label in ("gaoji.app.instance=" + identifier, "com.docker.compose.project=gaoji-" + identifier):
            containers.update(self.command("ps", "-aq", "--no-trunc", "--filter", "label=" + label).splitlines())
        if len(containers) > 128 or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in containers):
            raise CapacityError("容器写入层无法完整统计")
        writable = 0
        if containers:
            rows = json.loads(self.command("container", "inspect", "--size", *sorted(containers)))
            if (not isinstance(rows, list) or len(rows) != len(containers)
                or any(not isinstance(row, dict) or not isinstance(row.get("Id"), str) for row in rows)
                or {row["Id"] for row in rows} != containers):
                raise CapacityError("容器写入层无法完整统计")
            for row in rows:
                labels = (row.get("Config") or {}).get("Labels") if isinstance(row.get("Config"), dict) else None
                if (not isinstance(labels, dict) or not (labels.get("gaoji.app.instance") == identifier
                    or labels.get("com.docker.compose.project") == "gaoji-" + identifier)
                    or type(row.get("SizeRw")) is not int or row["SizeRw"] < 0):
                    raise CapacityError("容器归属或写入层大小无法确认")
                writable += row["SizeRw"]
        return engine_root, volumes, writable

    def data_usage(self, *, deadline, seen):
        return allocated_bytes(self.directory, deadline=deadline, seen=seen)

    def volume_usage(self, volumes, *, deadline, seen):
        return sum(allocated_bytes(folder, deadline=deadline, seen=seen) for folder in volumes)

    def scan(self, *, wait=False):
        if not self.scanning.acquire(timeout=30 if wait else 0):
            raise CapacityError("容量统计已在运行")
        try:
            self.deadline = time.monotonic() + 30
            config = self.c.store.read(self.c.identifier)
            selected = policy(config["settings"]["components"].get("capacity", DEFAULT_POLICY))
            value = {"schema": 1, "instance_id": self.c.identifier, "revision": config["revision"],
                "policy": selected, "sampled_at": int(time.time()), "complete": False, "used_bytes": 0,
                "data_bytes": None, "sandbox_bytes": None, "writable_bytes": None, "disks": [], "reason": ""}
            try:
                disk = shutil.disk_usage(self.directory)
                value["disks"].append({"name": "instance", "free_bytes": disk.free, "total_bytes": disk.total})
                engine_root, volumes, writable = self.engine_inventory()
                engine_disk = shutil.disk_usage(engine_root)
                value["disks"].append({"name": "docker", "free_bytes": engine_disk.free, "total_bytes": engine_disk.total})
                seen = set()
                deadline = min(self.deadline, time.monotonic() + 20)
                value["data_bytes"] = self.data_usage(deadline=deadline, seen=seen)
                value["sandbox_bytes"] = self.volume_usage(volumes, deadline=deadline, seen=seen)
                value["writable_bytes"] = writable
                value["used_bytes"] = value["data_bytes"] + value["sandbox_bytes"] + writable
                value["complete"] = True
                check_snapshot(value)
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
                value["reason"] = str(error) if isinstance(error, CapacityError) else "本次容量统计不完整，未读取其他实例或删除文件"
            value["sampled_at"] = int(time.time())
            self.publish(value)
            return value
        finally:
            self.deadline = None
            self.scanning.release()

    def status(self):
        try:
            value = read_snapshot(self.filename, self.c.identifier)
        except CapacityError as error:
            return {"state": "unknown", "reason": str(error)}
        reason = value.get("reason", "")
        try:
            check_snapshot(value)
        except CapacityError as error:
            reason = reason or str(error)
        return {**value, "state": "unknown" if not value["complete"] else "blocked" if reason else "ready", "reason": reason}

    def require(self, expected_bytes=64 * MIB):
        # Maintenance writes need a fresh sample, not the dashboard's cache.
        check_snapshot(self.scan(wait=True), expected_bytes)
