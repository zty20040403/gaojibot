"""Authenticated archive transport, separate from applying a restore."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import time

from .backups import BackupError, BackupStore, backup_id, regular_file, verify_archive
from .files import file_lock, read_json, write_json
from .maintenance import sync_directory
from src.storage_admission import CapacityError


MAX_BYTES = 32 * 1024**3
FIELDS = {"schema", "id", "source_instance_id", "runtime_checksum", "bytes", "sha256", "database_bytes", "created_at", "revision", "files"}


def metadata(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS or type(value.get("schema")) is not int or value["schema"] != 1:
        raise BackupError("备份传输格式不支持")
    backup_id(value["id"])
    backup_id(value["source_instance_id"])
    from .releases import checksum
    checksum(value["runtime_checksum"])
    checksum(value["sha256"])
    if (any(type(value[key]) is not int or value[key] < 0 for key in ("bytes", "database_bytes", "created_at", "revision", "files"))
            or not 1024 <= value["bytes"] <= MAX_BYTES or value["database_bytes"] > MAX_BYTES * 16
            or value["revision"] < 1 or not 2 <= value["files"] <= 200000):
        raise BackupError("备份传输大小或元数据无效")
    return dict(value)


def import_id(instance: str, digest: str) -> str:
    return hashlib.sha256((instance + ":" + digest).encode()).hexdigest()[:32]


class BackupTransfer:
    def __init__(self, controller):
        self.c = controller
        self.store = BackupStore(controller.directory)

    def receipt(self, info: dict) -> dict:
        info = metadata(info)
        identifier = import_id(self.c.identifier, info["sha256"])
        if not self.store.path(identifier).exists():
            return {"id": identifier, "imported": False}
        record = self.store.check(identifier, info["runtime_checksum"])
        if record.get("imported") is not True or record["sha256"] != info["sha256"]:
            raise BackupError("备份编号冲突，未覆盖已有备份")
        return {"id": identifier, "imported": True, "already_imported": True}

    @contextmanager
    def export(self, identifier: str):
        with file_lock(self.c.directory / ".backup-transfer.lock", timeout=0.1):
            folder = self.store.path(identifier)
            record = read_json(folder / "metadata.json")
            record = self.store.check(identifier, record["runtime_checksum"])
            info = metadata({**{key: record[key] for key in FIELDS - {"source_instance_id"}},
                "source_instance_id": record.get("source_instance_id", record["instance_id"])})
            with regular_file(folder / "snapshot.tar") as stream:
                yield info, stream

    def import_archive(self, stream, info: dict, revision: int) -> dict:
        info = metadata(info)
        identifier = import_id(self.c.identifier, info["sha256"])
        with file_lock(self.c.directory / ".backup-transfer.lock", timeout=0.1), file_lock(self.c.directory / ".maintenance.lock", timeout=0.1):
            self._ready(revision, info)
            destination = self.store.path(identifier)
            if destination.exists():
                record = self.store.check(identifier, info["runtime_checksum"])
                if record.get("imported") is not True or record["sha256"] != info["sha256"]:
                    raise BackupError("备份编号冲突，未覆盖已有备份")
                return {"id": identifier, "imported": True, "already_imported": True}
            try:
                self.c.capacity.require(info["bytes"])
            except CapacityError as error:
                raise BackupError(str(error)) from None
            if shutil.disk_usage(self.c.directory).free < info["bytes"] + 512 * 1024**2:
                raise BackupError("服务器暂存空间不足，未删除现有数据")
            temporary = self.store.root / (".import-" + identifier)
            if temporary.is_symlink():
                raise BackupError("备份暂存目录不能是链接")
            if temporary.exists():
                # Same content-addressed import after an interrupted upload.
                shutil.rmtree(temporary)
            temporary.mkdir(mode=0o700)
            try:
                filename = temporary / "snapshot.tar"
                digest, received = hashlib.sha256(), 0
                with filename.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    while received < info["bytes"]:
                        data = stream.read(min(1024 * 1024, info["bytes"] - received))
                        if not data:
                            raise BackupError("备份上传中断，未导入")
                        received += len(data)
                        if received > info["bytes"]:
                            raise BackupError("备份传输超过声明大小")
                        digest.update(data)
                        output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                if digest.hexdigest() != info["sha256"]:
                    raise BackupError("备份摘要不匹配，未导入")
                verified = verify_archive(filename, expected_checksum=info["runtime_checksum"], max_bytes=info["bytes"])
                snapshot, manifest = verified["snapshot"], verified["manifest"]
                if (snapshot["instance_id"] != info["source_instance_id"] or snapshot["revision"] != info["revision"]
                        or len(manifest["files"]) != info["files"] or manifest["created_at"] != info["created_at"]):
                    raise BackupError("备份来源与清单不一致，未导入")
                record = {**info, "id": identifier, "instance_id": self.c.identifier, "imported": True,
                    "imported_at": int(time.time()), "content_bytes": verified["content_bytes"]}
                write_json(temporary / "metadata.json", record)
                with self.c.lock:
                    self._ready(revision, info)
                    os.rename(temporary, destination)
                    sync_directory(self.store.root)
                return {"id": identifier, "imported": True, "already_imported": False}
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    def _ready(self, revision: int, info: dict):
        with self.c.lock:
            self.c.mutable()
            if type(revision) is not int or self.c.store.read(self.c.identifier)["revision"] != revision:
                raise BackupError("实例配置已变化，请刷新后重新导入")
            if info["runtime_checksum"] != self.c.deployment["checksum"]:
                raise BackupError("备份的运行版本与目标不一致，请先安装对应运行版本；未导入或覆盖数据")
