"""Private, checksummed instance snapshots; never archive a live PG data directory."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import time
from typing import BinaryIO, Iterator

from .files import read_json, write_json
from .instances import InstanceError, validate_settings
from .components import component_keys
from .runtime import runtime_environment


ID = re.compile(r"^[a-f0-9]{32}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
DATA_TREES = ("state", "files", "napcat")
MAX_ENTRIES = 200_000
MAX_MANIFEST = 64 * 1024 * 1024
BLOCK = 1024 * 1024
EPHEMERAL = {"SingletonLock", "SingletonSocket", "SingletonCookie"}


class BackupError(InstanceError):
    pass


def backup_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise BackupError("备份编号无效")
    return value


def digest_file(filename: Path) -> tuple[int, str]:
    size, digest = 0, hashlib.sha256()
    with regular_file(filename) as source:
        for chunk in iter(lambda: source.read(BLOCK), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


@contextmanager
def regular_file(filename: Path) -> Iterator[BinaryIO]:
    descriptor = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise BackupError("备份中发现非普通文件，未继续读取")
        yield source


def _member_name(name: str) -> str:
    path = PurePosixPath(name)
    if not isinstance(name, str) or not name or len(name) > 4096 or "\\" in name or "\0" in name \
            or path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise BackupError("备份内的文件路径无效")
    if name not in {"snapshot.json", "database.dump"} and not (len(path.parts) > 1 and path.parts[0] in DATA_TREES):
        raise BackupError("备份包含本实例数据之外的文件")
    return name


def walk_data(directory: Path) -> Iterator[tuple[str, Path]]:
    for name in DATA_TREES:
        root = directory / name
        if root.is_symlink() or not root.is_dir():
            raise BackupError("实例数据目录缺失或被替换成了链接")
        for parent, children, files in os.walk(root, followlinks=False):
            children.sort()
            for child in children:
                if (Path(parent) / child).is_symlink():
                    raise BackupError("实例中有链接目录，备份已停止以免读取外部文件")
            for child in sorted(files):
                filename = Path(parent) / child
                if child in EPHEMERAL and (filename.is_symlink() or not filename.is_file()):
                    continue
                if filename == directory / "state/desktop-health.json":
                    continue
                if not stat.S_ISREG(filename.lstat().st_mode):
                    raise BackupError("实例中有非普通文件，备份已停止")
                yield _member_name(filename.relative_to(directory).as_posix()), filename


def _snapshot(config: dict, private: dict, admin_secret: bytes) -> dict:
    if len(admin_secret) != 32:
        raise BackupError("控制台密钥不完整，未创建备份")
    runtime_environment(config, private, configured=False)
    revision = config["revision"]
    credentials = {key: private[key] for key in ("onebot_token", "onebot_secret", "napcat_token", "database_password")}
    if any(not isinstance(value, str) or len(value) < 32 for value in credentials.values()):
        raise BackupError("实例凭据不完整，未创建备份")
    credentials["model_key_sets"] = {str(revision): private["model_keys"]}
    credentials["component_key_sets"] = {str(revision): component_keys(private.get("component_keys", {}))}
    return {"schema": 1, "instance_id": config["id"], "revision": revision, "settings": validate_settings(config["settings"]),
        "private": credentials, "admin_secret": base64.b64encode(admin_secret).decode("ascii")}


def validate_snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema", "instance_id", "revision", "settings", "private", "admin_secret"} \
            or value["schema"] != 1 or type(value["revision"]) is not int or value["revision"] < 1:
        raise BackupError("备份配置格式不支持")
    backup_id(value["instance_id"])
    settings = validate_settings(value["settings"])
    private = value["private"]
    if not isinstance(private, dict) or set(private) - {"component_key_sets"} != {"onebot_token", "onebot_secret", "napcat_token", "database_password", "model_key_sets"}:
        raise BackupError("备份凭据格式不支持")
    keys = private.get("model_key_sets", {}).get(str(value["revision"]))
    if not isinstance(keys, dict) or set(keys) - {model["id"] for model in settings["models"]} \
            or any(not isinstance(key, str) or len(key) > 8192 or any(char in key for char in "\0\r\n") for key in keys.values()):
        raise BackupError("备份模型凭据不匹配")
    try:
        admin = base64.b64decode(value["admin_secret"], validate=True)
    except (ValueError, TypeError):
        raise BackupError("备份控制台密钥无效") from None
    services = component_keys(private["component_key_sets"].get(str(value["revision"]))) if "component_key_sets" in private else {}
    normalized = _snapshot({"id": value["instance_id"], "revision": value["revision"], "settings": settings},
        {**private, "model_keys": keys, "component_keys": services}, admin)
    return normalized


class _HashReader:
    def __init__(self, stream: BinaryIO):
        self.stream, self.digest = stream, hashlib.sha256()

    def read(self, size: int) -> bytes:
        block = self.stream.read(size)
        self.digest.update(block)
        return block


def write_archive(filename: Path, *, directory: Path, snapshot: dict, dump: Path, checksum: str) -> dict:
    if not DIGEST.fullmatch(checksum):
        raise BackupError("运行版本摘要无效")
    snapshot = validate_snapshot(snapshot)
    entries = []
    with filename.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
            payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            member = tarfile.TarInfo("snapshot.json")
            member.size, member.mode = len(payload), 0o600
            archive.addfile(member, io.BytesIO(payload))
            entries.append({"name": member.name, "bytes": member.size, "sha256": hashlib.sha256(payload).hexdigest()})
            def add(name: str, source: Path) -> None:
                if len(entries) >= MAX_ENTRIES:
                    raise BackupError("实例文件数量超出单份备份范围")
                with regular_file(source) as stream:
                    before = os.fstat(stream.fileno())
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = before.st_size, stat.S_IMODE(before.st_mode) & 0o777
                    reader = _HashReader(stream)
                    archive.addfile(member, reader)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise BackupError("文件在备份期间发生变化，请稍后重试")
                    entries.append({"name": name, "bytes": member.size, "sha256": reader.digest.hexdigest()})
            with regular_file(dump) as source:
                if source.read(5) != b"PGDMP":
                    raise BackupError("数据库导出文件无效，未创建备份")
            add("database.dump", dump)
            for name, source in walk_data(directory):
                add(name, source)
            manifest = {"schema": 1, "instance_id": snapshot["instance_id"], "runtime_checksum": checksum,
                "created_at": int(time.time()), "files": entries,
                "excluded": ["cache", "logs", "assets", "temporary_browser_locks", "desktop_health"]}
            body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
            if len(body) > MAX_MANIFEST:
                raise BackupError("备份清单过大")
            member = tarfile.TarInfo("manifest.json")
            member.size, member.mode = len(body), 0o600
            archive.addfile(member, io.BytesIO(body))
        output.flush()
        os.fsync(output.fileno())
    return manifest


def verify_archive(filename: Path, *, expected_checksum: str, max_bytes: int, destination: Path | None = None) -> dict:
    """Stream-check every entry. Optional extraction only writes validated regular paths."""
    if destination is not None:
        destination.mkdir(mode=0o700, exist_ok=False)
    entries, manifest, snapshot = [], None, None
    total, seen = 0, set()
    try:
        with regular_file(filename) as raw, tarfile.open(fileobj=raw, mode="r|") as archive:
            for member in archive:
                if manifest is not None or not member.isfile() or member.name in seen or len(seen) > MAX_ENTRIES:
                    raise BackupError("备份包含重复、特殊文件或清单后的额外内容")
                seen.add(member.name)
                if member.name != "manifest.json":
                    _member_name(member.name)
                if member.size < 0 or total + member.size > max_bytes:
                    raise BackupError("恢复所需空间超过可用额度")
                total += member.size
                source = archive.extractfile(member)
                if member.name in {"manifest.json", "snapshot.json"}:
                    if member.size > (MAX_MANIFEST if member.name == "manifest.json" else 2 * BLOCK):
                        raise BackupError("备份配置或清单过大")
                    data = source.read()
                    if member.name == "manifest.json":
                        manifest = json.loads(data)
                        continue
                    snapshot = validate_snapshot(json.loads(data))
                    source = io.BytesIO(data)
                target = None
                try:
                    if destination is not None:
                        target_path = destination / member.name
                        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        target = target_path.open("xb")
                        os.fchmod(target.fileno(), member.mode & 0o777)
                    digest, size = hashlib.sha256(), 0
                    for block in iter(lambda: source.read(BLOCK), b""):
                        size += len(block)
                        digest.update(block)
                        if target:
                            target.write(block)
                    if size != member.size:
                        raise BackupError("备份文件内容被截断")
                    if target:
                        target.flush()
                        os.fsync(target.fileno())
                    entries.append({"name": member.name, "bytes": size, "sha256": digest.hexdigest()})
                finally:
                    if target:
                        target.close()
        if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("files") != entries \
                or not snapshot or manifest.get("instance_id") != snapshot["instance_id"] or "database.dump" not in seen:
            raise BackupError("备份内容与清单不一致，未执行恢复")
        if manifest.get("runtime_checksum") != expected_checksum:
            raise BackupError("备份来自不同运行版本，请先使用对应版本的 App 恢复")
        return {"manifest": manifest, "snapshot": snapshot, "content_bytes": total}
    except BaseException:
        if destination is not None:
            shutil.rmtree(destination)
        raise


class BackupStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.root = directory / "backups"
        if self.root.is_symlink():
            raise BackupError("备份目录不能是链接")
        self.root.mkdir(mode=0o700, exist_ok=True)

    def path(self, identifier: str) -> Path:
        result = self.root / backup_id(identifier)
        if result.is_symlink():
            raise BackupError("备份目录不能是链接")
        return result

    def list(self) -> list[dict]:
        result = []
        for directory in self.root.iterdir():
            if ID.fullmatch(directory.name) and not directory.is_symlink() and (directory / "metadata.json").is_file():
                record = read_json(directory / "metadata.json")
                if record.get("id") == directory.name:
                    result.append({**{key: record[key] for key in ("id", "created_at", "bytes", "sha256", "files", "revision")},
                        "imported": record.get("imported") is True,
                        "source_instance_id": record.get("source_instance_id", record.get("instance_id"))})
        return sorted(result, key=lambda value: (value["created_at"], value["id"]), reverse=True)

    def publish(self, identifier: str, config: dict, private: dict, dump: Path, checksum: str, *, database_bytes: int) -> dict:
        if type(database_bytes) is not int or database_bytes < 0:
            raise BackupError("数据库容量记录无效")
        directory = self.path(identifier)
        if (directory / "metadata.json").exists():
            return self.check(identifier, checksum)
        directory.mkdir(mode=0o700, exist_ok=True)
        temporary = directory / "snapshot.partial"
        temporary.unlink(missing_ok=True)
        try:
            with regular_file(self.directory / "config/admin.secret") as stream:
                secret = stream.read(33)
            manifest = write_archive(temporary, directory=self.directory,
                snapshot=_snapshot(config, private, secret), dump=dump, checksum=checksum)
            size, digest = digest_file(temporary)
            verified = verify_archive(temporary, expected_checksum=checksum, max_bytes=size)
            os.replace(temporary, directory / "snapshot.tar")
            record = {"schema": 1, "id": identifier, "instance_id": config["id"], "runtime_checksum": checksum,
                "created_at": manifest["created_at"], "bytes": size, "sha256": digest, "files": len(manifest["files"]),
                "revision": config["revision"], "content_bytes": verified["content_bytes"], "database_bytes": database_bytes}
            write_json(directory / "metadata.json", record)
            return record
        finally:
            temporary.unlink(missing_ok=True)

    def check(self, identifier: str, checksum: str) -> dict:
        directory = self.path(identifier)
        record = read_json(directory / "metadata.json")
        if record.get("schema") != 1 or record.get("id") != identifier or record.get("instance_id") != self.directory.name \
                or record.get("runtime_checksum") != checksum or type(record.get("database_bytes")) is not int or record["database_bytes"] < 0:
            raise BackupError("备份不属于当前实例或运行版本")
        if digest_file(directory / "snapshot.tar") != (record.get("bytes"), record.get("sha256")):
            raise BackupError("备份摘要不匹配，未执行恢复")
        return record
