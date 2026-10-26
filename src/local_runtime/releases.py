"""Verify desktop-uploaded releases before an existing instance is paused."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .validation import InstanceError


MAX_RELEASE_BYTES = 512 * 1024**2
MAX_FILE_BYTES = 25 * 1024**2
# Includes container startup, cold Python imports, fonts and browser rendering.
RUNTIME_CHECK_TIMEOUT = 300
REQUIRED_FILES = {"Dockerfile", "images.lock.json", "pyproject.toml", "uv.lock",
                  "src/local_runtime/entrypoint.py", "src/local_runtime/manager.py",
                  "src/local_runtime/runtime.py", "alembic.ini"}


class ReleaseError(InstanceError):
    pass


def checksum(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ReleaseError("运行包摘要无效")
    return value


def read_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o022
                or before.st_size > maximum):
            raise ReleaseError("运行包文件归属、权限或大小异常")
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ReleaseError("运行包在校验期间发生变化")
        if len(data) != before.st_size:
            raise ReleaseError("运行包文件没有完整读取")
        return data


@dataclass(frozen=True)
class Release:
    directory: Path
    checksum: str
    version: str
    image: str
    manifest: dict
    images: dict


def load_release(root: Path, expected: str) -> Release:
    """The root is installer-owned; an API request supplies only a checksum."""
    expected = checksum(expected)
    directory = root / expected
    try:
        for folder in (root, directory):
            info = folder.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o022):
                raise ReleaseError("运行包目录不属于本应用安装器")
        marker = json.loads(read_regular(directory / ".bundle.json", 4096))
        if not isinstance(marker, dict) or marker.get("sha256") != expected:
            raise ReleaseError("运行包没有对应的安装校验记录")
        manifest = json.loads(read_regular(directory / "manifest.json", 8 * 1024**2))
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "version", "files"}
                or manifest["schema"] != 1 or not isinstance(manifest["version"], str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", manifest["version"])
                or not isinstance(manifest["files"], list) or not 1 <= len(manifest["files"]) <= 10000):
            raise ReleaseError("此运行包的格式或版本不支持更新")
        names, total, image_content = set(), 0, None
        for item in manifest["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                raise ReleaseError("运行包清单条目无效")
            name = item["path"]
            if (not isinstance(name, str) or not name or "\\" in name or "\0" in name
                    or PurePosixPath(name).is_absolute() or any(part in {"", ".", ".."} for part in name.split("/"))
                    or name in names or name in {"manifest.json", ".bundle.json"}
                    or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_FILE_BYTES):
                raise ReleaseError("运行包清单路径或大小无效")
            names.add(name)
            total += item["bytes"]
            if total > MAX_RELEASE_BYTES:
                raise ReleaseError("运行包展开大小超过上限")
            current = directory
            for part in PurePosixPath(name).parts[:-1]:
                current /= part
                info = current.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise ReleaseError("运行包包含链接或可被其他用户修改的目录")
            content = read_regular(directory / name, MAX_FILE_BYTES)
            if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != checksum(item["sha256"]):
                raise ReleaseError("运行包内容与清单摘要不一致，尚未暂停机器人")
            if name == "images.lock.json":
                image_content = content
        if not REQUIRED_FILES <= names:
            raise ReleaseError("运行包缺少更新或数据库迁移所需文件")
        images = json.loads(image_content)
        if not isinstance(images, dict) or images.get("schema") != 1:
            raise ReleaseError("运行依赖清单格式不支持")
        for name in ("python", "postgres", "napcat"):
            if not isinstance(images.get(name), str) or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", images[name]):
                raise ReleaseError("运行依赖必须使用固定镜像摘要")
        identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return Release(directory, expected, manifest["version"], "gaoji-app-runtime:" + identity, manifest, images)
    except ReleaseError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ReleaseError("运行包校验未完成，尚未暂停机器人") from error


def runtime_check_arguments(docker: str, image: str) -> list[str]:
    if not Path(docker).is_absolute() or not re.fullmatch(r"gaoji-app-runtime:[a-f0-9]{64}", image):
        raise ReleaseError("运行镜像检查目标无效")
    return [docker, "run", "--rm", "--platform", "linux/amd64", "--pull=never", "--network", "none", "--read-only",
        "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--memory", "1g", "--cpus", "1", "--pids-limit", "256", "--shm-size", "128m",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--env", "HOME=/tmp", image, "check-runtime"]
