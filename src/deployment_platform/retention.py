"""Remove only verified, unreferenced content owned by one website instance."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat

from src.local_runtime.files import read_json
from .content import relative_path, sha256


def regular(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022


def prune_uploads(host, root: Path, instance: str, *, purge: bool = False) -> int:
    released = 0
    bootstrap = root / "bootstrap"
    if not bootstrap.exists():
        return 0
    if bootstrap.is_symlink():
        raise ValueError("部署缓存目录不能是链接")
    for jobroot in bootstrap.iterdir():
        if not re.fullmatch(r"[a-f0-9]{32}", jobroot.name) or jobroot.is_symlink() or not jobroot.is_dir():
            continue
        record_path = root / "jobs" / (jobroot.name + ".json")
        if not record_path.exists() or not regular(record_path):
            continue
        record = read_json(record_path)
        if record.get("job") != jobroot.name or record.get("instance_id") != instance or record.get("app_id") != "static-site":
            continue
        if record.get("status") != "completed" and not (purge and record.get("status") == "failed"):
            continue
        checksum = record.get("checksum", "")
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            continue
        work = jobroot / checksum
        request_path = work / "application.json"
        if work.is_symlink() or not request_path.exists() or not regular(request_path):
            continue
        request = read_json(request_path)
        if request.get("app_id") != "static-site" or request.get("instance_id") != instance:
            continue
        if jobroot.name != host.job:
            pid = host.run(["systemctl", "show", f"gaoji-app-install-{jobroot.name}.service", "--property=MainPID", "--value"], capture=True, timeout=30).strip()
            if pid != "0":
                if purge:
                    raise ValueError("该网站仍有后台任务在读取上传包，请结束原任务后继续删除")
                continue
        files = [("website.tar.gz", request.get("content_sha256"))]
        # Keep code/markers for a completed unit's harmless reboot replay. Its
        # large archive is unnecessary once the durable job itself is complete.
        if record.get("status") == "completed":
            files.append(("gaoji-runtime.tar.gz", checksum))
        for name, expected in files:
            target = work / name
            if not target.exists():
                continue
            if not regular(target) or not isinstance(expected, str) or sha256(target) != expected:
                raise ValueError("上传副本已被改动，未自动删除未知文件")
            size = target.stat().st_size
            target.unlink()
            released += size
    return released


def prune_versions(directory: Path, keep: set[str], mounted_sources: list[str]) -> int:
    releases = directory / "releases"
    if not releases.exists():
        return 0
    if releases.is_symlink():
        raise ValueError("网站版本目录不能是链接")
    released = 0
    for release in releases.iterdir():
        if release.name in keep or not re.fullmatch(r"[a-f0-9]{64}", release.name) or release.is_symlink() or not release.is_dir():
            continue
        prefix = str(release) + "/"
        if any(source == str(release) or source.startswith(prefix) for source in mounted_sources):
            continue
        manifest_path = release / "content.json"
        if not manifest_path.exists() or not regular(manifest_path):
            continue
        manifest = read_json(manifest_path)
        if manifest.get("checksum") != release.name:
            raise ValueError("网页版本来源无法确认，未删除")
        if not isinstance(manifest.get("files"), list):
            raise ValueError("网页版本缺少文件清单，未删除")
        expected = {"content.json"}
        for item in manifest["files"]:
            name = "html/" + relative_path(item["path"]).as_posix()
            target = release / name
            if not regular(target) or sha256(target) != item["sha256"]:
                raise ValueError("网页文件已被改动，未自动删除")
            expected.add(name)
        for item in release.rglob("*"):
            name = item.relative_to(release).as_posix()
            if item.is_symlink() or not (item.is_file() or item.is_dir()):
                raise ValueError("网页版本包含未知文件，未自动删除")
            if item.is_file() and (name not in expected and name != "nginx.conf" or not regular(item)):
                raise ValueError("网页版本包含未知文件，未自动删除")
        released += sum(item.stat().st_size for item in release.rglob("*") if item.is_file() and not item.is_symlink())
        shutil.rmtree(release)
    return released
