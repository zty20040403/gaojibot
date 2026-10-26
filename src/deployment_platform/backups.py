"""Instance-bound static snapshots; never archive paths supplied by a client."""
from __future__ import annotations

import io
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import time

from src.local_runtime.files import read_json, write_json
from .content import MAX_BYTES, MAX_FILE, MAX_FILES, relative_path, sha256

MAX_BACKUPS = 20


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("无效网站备份编号")
    return value


def owned(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("网站备份文件所有者或类型无效")


def verify(directory: Path, instance: str, backup: str) -> tuple[Path, dict]:
    folder = directory / "backups"
    owned(folder, directory=True)
    archive = folder / (identifier(backup) + ".tar.gz")
    record = folder / (backup + ".json")
    owned(record)
    owned(archive)
    if record.stat().st_size > 8192 or archive.stat().st_size > MAX_BYTES + 8 * 1024**2:
        raise ValueError("网站备份大小无效")
    info = read_json(record)
    if (info.get("schema") != 1 or info.get("app_id") != "static-site"
            or info.get("id") != backup or info.get("source_instance_id") != instance
            or info.get("bytes") != archive.stat().st_size or info.get("sha256") != sha256(archive)):
        raise ValueError("网站备份校验失败，未恢复")
    return archive, info


def create(directory: Path, instance: str, backup: str, checksum: str) -> dict:
    identifier(instance)
    identifier(backup)
    folder = directory / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    owned(folder, directory=True)
    record = folder / (backup + ".json")
    if record.exists() or record.is_symlink():
        return verify(directory, instance, backup)[1]
    if len(list(folder.glob("*.json"))) >= MAX_BACKUPS:
        raise ValueError("此网站已有 20 份备份，请先导出或删除不再需要的备份")
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ValueError("没有可备份的网站版本")
    release = directory / "releases" / checksum
    owned(release.parent, directory=True)
    owned(release, directory=True)
    owned(release / "html", directory=True)
    owned(release / "content.json")
    manifest = read_json(release / "content.json")
    files = manifest.get("files", [])
    if manifest.get("checksum") != checksum or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("网站版本清单无效")
    expected = {relative_path(item["path"]).as_posix() for item in files}
    actual = set()
    for entry in (release / "html").rglob("*"):
        if entry.is_dir() and not entry.is_symlink():
            owned(entry, directory=True)
        else:
            owned(entry)
            actual.add(entry.relative_to(release / "html").as_posix())
    if actual != expected:
        raise ValueError("网站包含未登记的文件，未创建不完整备份")
    if shutil.disk_usage(folder).free < MAX_BYTES * 2:
        raise ValueError("网站备份至少需要 256 MiB 可用空间")
    partial = folder / (backup + ".partial")
    archive = folder / (backup + ".tar.gz")
    # A crash before the manifest commit may leave only this job's own archive.
    for candidate in (partial, archive):
        if candidate.exists() or candidate.is_symlink():
            owned(candidate)
            candidate.unlink()
    total = 0
    seen = set()
    try:
        with partial.open("xb") as stream:
            partial.chmod(0o600)
            with tarfile.open(fileobj=stream, mode="w:gz") as output:
                for item in files:
                    relative = relative_path(item["path"])
                    if relative.as_posix().casefold() in seen:
                        raise ValueError("网站备份含重复路径")
                    seen.add(relative.as_posix().casefold())
                    target = release / "html" / relative
                    for parent in target.parents:
                        if parent == release:
                            break
                        owned(parent, directory=True)
                    owned(target)
                    if target.stat().st_size > MAX_FILE:
                        raise ValueError("网站文件过大")
                    data = target.read_bytes()
                    total += len(data)
                    if total > MAX_BYTES or len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                        raise ValueError("网站内容发生变化，未创建备份")
                    entry = tarfile.TarInfo(relative.as_posix())
                    entry.mode = 0o644
                    entry.size = len(data)
                    output.addfile(entry, io.BytesIO(data))
            stream.flush()
            os.fsync(stream.fileno())
        if "index.html" not in seen:
            raise ValueError("网站备份没有入口页面")
        partial.replace(archive)
        info = {"schema": 1, "app_id": "static-site", "id": backup, "source_instance_id": instance,
                "bytes": archive.stat().st_size, "sha256": sha256(archive),
                "created_at": int(time.time()), "files": len(files)}
        write_json(record, info)
        return verify(directory, instance, backup)[1]
    finally:
        if partial.exists():
            partial.unlink()


def delete(directory: Path, backup: str) -> None:
    folder = directory / "backups"
    identifier(backup)
    if not folder.exists():
        return
    owned(folder, directory=True)
    for suffix in (".tar.gz", ".json", ".partial"):
        target = folder / (backup + suffix)
        if target.exists() or target.is_symlink():
            owned(target)
            target.unlink()


def purge_candidates(directory: Path) -> list[Path]:
    folder = directory / "backups"
    if not folder.exists() and not folder.is_symlink():
        return []
    owned(folder, directory=True)
    items = list(folder.iterdir())
    for item in items:
        if not re.fullmatch(r"[a-f0-9]{32}\.(?:json|tar\.gz|partial)", item.name):
            raise ValueError("备份目录存在未知文件，未删除")
        owned(item)
    return items


def purge(directory: Path) -> None:
    items = purge_candidates(directory)
    for item in items:
        item.unlink()
    folder = directory / "backups"
    if folder.exists():
        folder.rmdir()
