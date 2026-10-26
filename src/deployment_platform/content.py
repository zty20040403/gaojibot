"""Extract a static website without trusting archive paths or permission bits."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile

from src.local_runtime.files import read_json, write_json

MAX_BYTES = 128 * 1024**2
MAX_FILE = 32 * 1024**2
MAX_FILES = 5000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_path(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if (not name or value.is_absolute() or len(value.parts) > 21
            or re.search(r"[\x00-\x1f\x7f\\:]", name)
            or any(part.startswith(".") or len(part) > 240 or part == "node_modules" for part in value.parts)
            or re.search(r"\.(pem|key|p12|pfx)$", name, flags=re.I)):
        raise ValueError("网站含有不安全路径、隐藏文件或密钥")
    return value


def extract_website(archive: Path, destination: Path, checksum: str) -> dict:
    if (not re.fullmatch(r"[a-f0-9]{64}", checksum) or archive.is_symlink()
            or not archive.is_file() or archive.stat().st_size > MAX_BYTES + 8 * 1024**2
            or sha256(archive) != checksum):
        raise ValueError("网站上传包校验失败")
    if destination.is_symlink():
        raise ValueError("网站版本目录不能是链接")
    if destination.exists():
        manifest = read_json(destination / "content.json")
        if manifest.get("checksum") != checksum:
            raise ValueError("网站版本已存在但内容不一致")
        for item in manifest["files"]:
            target = destination / "html" / relative_path(item["path"])
            if target.is_symlink() or not target.is_file() or sha256(target) != item["sha256"]:
                raise ValueError("已保存的网站版本校验失败")
        return manifest
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".site-", dir=destination.parent))
    try:
        html = temporary / "html"
        html.mkdir(mode=0o755)
        html.chmod(0o755)
        total = 0
        members = set()
        files = []
        with tarfile.open(archive, "r|gz") as source:
            for index, member in enumerate(source):
                if index >= MAX_FILES * 2:
                    raise ValueError("网站文件数量超过上限")
                relative = relative_path(member.name)
                key = relative.as_posix().casefold()
                if key in members or not member.isfile() and not member.isdir():
                    raise ValueError("网站不允许重复路径、链接或特殊文件")
                members.add(key)
                target = html / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                if member.isdir():
                    target.mkdir(exist_ok=True, mode=0o755)
                    continue
                total += member.size
                if member.size > MAX_FILE or total > MAX_BYTES or len(files) >= MAX_FILES:
                    raise ValueError("网站超过大小限制")
                stream = source.extractfile(member)
                if stream is None:
                    raise ValueError("网站文件不可读取")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                if target.stat().st_size != member.size:
                    raise ValueError("网站文件未完整上传")
                target.chmod(0o644)
                files.append({"path": relative.as_posix(), "bytes": member.size, "sha256": sha256(target)})
        if not any(item["path"] == "index.html" and item["bytes"] > 0 for item in files):
            raise ValueError("网站根目录缺少非空 index.html")
        for directory in html.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        manifest = {"schema": 1, "checksum": checksum, "bytes": total, "files": files}
        write_json(temporary / "content.json", manifest)
        temporary.rename(destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
