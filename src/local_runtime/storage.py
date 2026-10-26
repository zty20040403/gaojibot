"""Instance-only retention with preview, durable deletion and no global prune."""
from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid

from .backups import backup_id
from .files import file_lock, read_json, write_json
from .maintenance import sync_directory
from .setup_clients import SetupError
from .validation import ConfigurationConflict


ID = re.compile(r"^[a-f0-9]{32}$")
DEFAULT_POLICY = {"enabled": False, "keep_backups": 5, "keep_restore_points": 2, "min_age_days": 7}
LIVE = ("database", "state", "files", "napcat", "cache", "logs", "assets")


class StorageError(SetupError):
    pass


def policy(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY) or type(value["enabled"]) is not bool:
        raise StorageError("空间保留策略格式无效")
    for key, maximum in (("keep_backups", 100), ("keep_restore_points", 100), ("min_age_days", 3650)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise StorageError("至少保留一份备份、一个恢复点和一天时间")
    return dict(value)


def document(path):
    if path.is_symlink() or path.stat().st_size > 8 * 1024**2:
        raise StorageError("维护记录无效，未继续清理")
    return read_json(path)


def cleanup_pending(directory, except_id=None):
    path = directory / "config/storage-job.json"
    record = document(path) if path.exists() else None
    return bool(record and record.get("status") in {"queued", "running"} and record.get("id") != except_id)


def tree_info(path, *, deadline=None):
    """Fingerprint metadata without reading database/file content or following links."""
    deadline = deadline or time.monotonic() + 10
    first = path.lstat()
    digest, allocated, logical, count = hashlib.sha256(), 0, 0, 0
    stack = [path]
    while stack:
        current = stack.pop()
        info = current.lstat()
        if time.monotonic() > deadline or count >= 200000:
            raise StorageError("目录过大，本次扫描未完成；未将它列入清理")
        if info.st_dev != first.st_dev or current.is_mount() or stat.S_ISLNK(info.st_mode):
            raise StorageError("目录含挂载点或链接，不能按普通副本清理")
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise StorageError("目录包含特殊文件，未将它列入清理")
        count += 1
        allocated += getattr(info, "st_blocks", (info.st_size + 511) // 512) * 512
        logical += info.st_size if stat.S_ISREG(info.st_mode) else 0
        digest.update(json.dumps([str(current.relative_to(path)), info.st_dev, info.st_ino, info.st_mode,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink], separators=(",", ":")).encode())
        if stat.S_ISDIR(info.st_mode):
            stack.extend(sorted(current.iterdir(), reverse=True))
    return {"bytes": allocated, "logical_bytes": logical, "files": count, "fingerprint": digest.hexdigest(),
        "identity": [first.st_dev, first.st_ino], "modified_at": int(first.st_mtime)}


class Storage:
    def __init__(self, controller):
        self.c = controller
        self.directory = controller.directory
        self.policy_path = self.directory / "config/storage-policy.json"
        self.scan_path = self.directory / "config/storage-scan.json"
        self.plan_path = self.directory / "config/storage-plan.json"
        self.job_path = self.directory / "config/storage-job.json"
        self.auto_path = self.directory / "config/storage-auto.json"
        self.worker = None
        self.scanning = threading.Lock()
        self.shutdown = threading.Event()
        self.scheduler = None

    def settings(self):
        saved = document(self.policy_path) if self.policy_path.exists() else {"revision": 1, "policy": DEFAULT_POLICY}
        if type(saved.get("revision")) is not int or saved["revision"] < 1:
            raise StorageError("保留策略版本无效")
        return {"revision": saved["revision"], "policy": policy(saved["policy"])}

    def busy(self, except_id=None):
        return cleanup_pending(self.directory, except_id)

    def status(self):
        disk = shutil.disk_usage(self.directory)
        job = document(self.job_path) if self.job_path.exists() else None
        return {**self.settings(), "disk": {"total_bytes": disk.total, "free_bytes": disk.free},
            "capacity": self.c.capacity.status(),
            "scan": document(self.scan_path) if self.scan_path.exists() else None,
            "job": {**{key: job[key] for key in ("id", "status", "message", "updated_at", "items_done", "estimated_bytes", "free_before", "free_after") if key in job},
                "resumable": job.get("status") == "failed" and job.get("started") is True} if job else None}

    def save_policy(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"revision", "policy"}:
            raise StorageError("保留策略请求无效")
        selected = policy(payload["policy"])
        with self.c.lock:
            self.c.mutable()
            current = self.settings()
            if type(payload["revision"]) is not int or current["revision"] != payload["revision"]:
                raise ConfigurationConflict("保留策略已变化")
            previous = document(self.policy_path) if self.policy_path.exists() else {}
            value = {"revision": current["revision"] + 1, "policy": selected,
                "changes": [*previous.get("changes", [])[-49:], {"revision": current["revision"] + 1, "changed_at": int(time.time()), "policy": selected}]}
            write_json(self.policy_path, value)
            self.plan_path.unlink(missing_ok=True)
            self.scan_path.unlink(missing_ok=True)
            return self.settings()

    def _catalog(self):
        settings = self.settings()
        rows, warnings, referenced, grouped_backups = [], [], set(), set()
        broken = False
        histories = self.directory / "updates"
        workspaces = self.directory / "recovery"
        backups = self.directory / "backups"
        for root in (histories, workspaces, backups):
            if root.is_symlink():
                raise StorageError("副本目录不能是链接")
        update_rows = {}
        if histories.exists():
            for filename in sorted(histories.iterdir()):
                if not ID.fullmatch(filename.stem) or filename.suffix != ".json":
                    continue
                try:
                    record = document(filename)
                    identifier = backup_id(record["backup_id"])
                    if identifier in referenced:
                        broken = True
                        warnings.append("多个恢复点共享备份，保留它们以避免破坏恢复关系")
                    referenced.add(identifier)
                    if record.get("id") != filename.stem or record.get("original_deployment", {}).get("instance_id") != self.c.identifier:
                        raise StorageError("恢复点归属无效")
                    if record.get("stage") != "verified" or record.get("committed") is not True:
                        warnings.append("存在尚未验证的更新恢复点，保留其数据")
                        continue
                    update_rows[filename.stem] = record
                except (OSError, ValueError, KeyError, SetupError):
                    broken = True
                    warnings.append("更新记录损坏，无法确认备份引用；本次不删除副本")
        deadline = time.monotonic() + 15
        def add(identifier, kind, paths, created_at, protected=""):
            try:
                details = [{"path": str(path.relative_to(self.directory)), **tree_info(path, deadline=deadline)} for path in paths]
                rows.append({"id": kind + ":" + identifier, "kind": kind, "created_at": created_at,
                    "bytes": sum(item["bytes"] for item in details), "paths": details,
                    "protected": protected, "eligible": False})
            except (OSError, ValueError, SetupError):
                warnings.append(f"{kind}:{identifier} 无法完整核对，未列入清理")
        if workspaces.exists():
            for folder in sorted(workspaces.iterdir()):
                if not ID.fullmatch(folder.name) or folder.is_symlink():
                    continue
                try:
                    if folder.name in update_rows:
                        record = update_rows[folder.name]
                        identifier = record["backup_id"]
                        archive = document(backups / identifier / "metadata.json")
                        if archive.get("id") != identifier or archive.get("instance_id") != self.c.identifier:
                            raise StorageError("更新备份归属无效")
                        # Retire the advertised rollback point before its data.
                        add(folder.name, "restore_point", [histories / (folder.name + ".json"), folder, backups / identifier],
                            int(record["activation_after"]))
                        grouped_backups.add(identifier)
                    elif (folder / "rollback.json").exists():
                        record = document(folder / "rollback.json")
                        original = json.loads(base64.b64decode(record["original_config"]["instance.json"], validate=True))
                        if record.get("id") != folder.name or record.get("kind") != "restore" or record.get("stage") != "committed" or original["id"] != self.c.identifier:
                            raise StorageError("恢复副本归属无效")
                        add(folder.name, "restore_point", [folder], int(record.get("created_at", (folder / "rollback.json").stat().st_mtime)))
                    else:
                        warnings.append("保留一个没有完整完成记录的恢复目录")
                except (OSError, ValueError, KeyError, SetupError):
                    warnings.append("恢复副本记录不完整，已保留")
        if backups.exists():
            for folder in sorted(backups.iterdir()):
                if not ID.fullmatch(folder.name) or folder.name in grouped_backups or folder.is_symlink():
                    continue
                try:
                    record = document(folder / "metadata.json")
                    if record.get("instance_id") != self.c.identifier or record.get("id") != folder.name:
                        raise StorageError("备份归属无效")
                    add(folder.name, "backup", [folder], max(int(record["created_at"]), int(record.get("imported_at", 0))),
                        "仍被恢复点引用" if folder.name in referenced else "")
                except (OSError, ValueError, KeyError, SetupError):
                    warnings.append("保留一个无法核对来源的备份目录")
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        cutoff = time.time() - settings["policy"]["min_age_days"] * 86400
        for kind, keep in (("backup", "keep_backups"), ("restore_point", "keep_restore_points")):
            for index, row in enumerate(item for item in rows if item["kind"] == kind):
                reason = "恢复记录不完整" if broken else "最近保留份数" if index < settings["policy"][keep] else "未到保留期限" if row["created_at"] >= cutoff else ""
                row["protected"] = row["protected"] or reason
                row["eligible"] = not row["protected"]
        if len(rows) > 1000:
            raise StorageError("副本超过 1000 份，需要分批核对，未开始清理")
        return settings, rows, list(dict.fromkeys(warnings))

    def scan(self):
        if not self.scanning.acquire(blocking=False):
            raise StorageError("空间扫描已在进行")
        try:
            self.c.capacity.scan()
            _, rows, warnings = self._catalog()
            live = []
            for name in LIVE:
                try:
                    info = tree_info(self.directory / name, deadline=time.monotonic() + 1)
                    live.append({"name": name, "bytes": info["bytes"], "complete": True})
                except (OSError, SetupError):
                    live.append({"name": name, "bytes": None, "complete": False})
            value = {"created_at": int(time.time()), "live": live, "items": [self.public(row) for row in rows],
                "warnings": warnings, "candidate_bytes": sum(row["bytes"] for row in rows if row["eligible"])}
            write_json(self.scan_path, value)
            return value
        finally:
            self.scanning.release()

    @staticmethod
    def public(row):
        return {**{key: row[key] for key in ("id", "kind", "created_at", "bytes", "protected", "eligible")},
            "paths": [item["path"] for item in row["paths"]]}

    def plan(self):
        with self.c.lock:
            self.c.mutable()
            settings, rows, warnings = self._catalog()
            selected = [row for row in rows if row["eligible"]]
            value = {"id": uuid.uuid4().hex, "created_at": int(time.time()), "policy_revision": settings["revision"],
                "instance_revision": self.c.store.read(self.c.identifier)["revision"], "items": selected}
            write_json(self.plan_path, value)
            return {"id": value["id"], "created_at": value["created_at"], "items": [self.public(row) for row in selected],
                "estimated_bytes": sum(row["bytes"] for row in selected), "warnings": warnings}

    def submit(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"id", "confirm"} or payload["confirm"] is not True:
            raise StorageError("请先确认清理预览")
        identifier = backup_id(payload["id"])
        with self.c.lock:
            existing = document(self.job_path) if self.job_path.exists() else None
            if existing and existing["id"] == identifier:
                if existing.get("status") == "failed" and existing.get("started"):
                    self.c.mutable()
                    existing.update(status="queued", message="继续上次清理", updated_at=int(time.time()))
                    write_json(self.job_path, existing)
                self.resume()
                return self.status()["job"]
            if existing and existing.get("started") and existing.get("status") == "failed":
                raise StorageError("请先继续上次未完成的清理，不能覆盖原任务记录")
            self.c.mutable()
            plan = document(self.plan_path)
            if plan["id"] != identifier or time.time() - plan["created_at"] > 900:
                raise StorageError("清理预览已过期，请重新预览")
            if plan["policy_revision"] != self.settings()["revision"] or plan["instance_revision"] != self.c.store.read(self.c.identifier)["revision"]:
                raise ConfigurationConflict("实例或保留策略已变化")
            value = {**plan, "status": "queued", "message": "清理已排队", "updated_at": int(time.time()),
                "items_done": 0, "estimated_bytes": sum(row["bytes"] for row in plan["items"]), "free_before": shutil.disk_usage(self.directory).free}
            write_json(self.job_path, value)
            self.resume()
            return self.status()["job"]

    def resume(self):
        with self.c.lock:
            if self.worker and self.worker.is_alive() or not self.job_path.exists():
                return
            job = document(self.job_path)
            if job.get("status") not in {"queued", "running"}:
                return
            self.worker = threading.Thread(target=self.execute, name="instance-storage", daemon=True)
            self.worker.start()

    def mounts(self):
        def run(args):
            result = subprocess.run([self.c.deployment["docker"], *args], capture_output=True, text=True, timeout=20,
                env=self.c.process_environment(), check=True)
            if len(result.stdout) > 8 * 1024**2:
                raise StorageError("容器挂载信息过大，未清理")
            return result.stdout
        identifiers = run(["ps", "--all", "--quiet", "--no-trunc"]).split()
        if len(identifiers) > 5000 or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in identifiers):
            raise StorageError("容器列表无法可靠核对，未清理")
        sources = []
        for offset in range(0, len(identifiers), 100):
            for line in run(["inspect", "--format", "{{json .Mounts}}", *identifiers[offset:offset + 100]]).splitlines():
                mounts = json.loads(line)
                if not isinstance(mounts, list):
                    raise StorageError("容器挂载记录无效")
                sources.extend(Path(value["Source"]).resolve() for value in mounts if value.get("Source"))
        return sources

    def _source(self, relative):
        parts = Path(relative).parts
        if len(parts) != 2 or parts[0] not in {"backups", "recovery", "updates"}:
            raise StorageError("清理计划含非副本路径")
        if parts[0] == "updates" and not parts[1].endswith(".json"):
            raise StorageError("更新恢复点路径无效")
        backup_id(parts[1][:-5] if parts[0] == "updates" and parts[1].endswith(".json") else parts[1])
        root = self.directory / parts[0]
        if root.is_symlink():
            raise StorageError("清理目录被替换为链接")
        return root / parts[1]

    @staticmethod
    def kernel_mounts():
        filename = Path("/proc/self/mountinfo")
        if not filename.exists():
            return []
        paths = []
        for line in filename.read_text().splitlines():
            fields = line.split()
            if len(fields) < 6:
                raise StorageError("系统挂载信息无效，未清理")
            paths.append(Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])))
        return paths

    def execute(self):
        job = document(self.job_path)
        try:
            with ExitStack() as locks:
                for name in (".backup-transfer.lock", ".maintenance.lock", ".storage.lock"):
                    locks.enter_context(file_lock(self.directory / name, timeout=0.1))
                with self.c.lock:
                    self.c.mutable(storage_job_id=job["id"])
                    if not job.get("started"):
                        if job["policy_revision"] != self.settings()["revision"] or job["instance_revision"] != self.c.store.read(self.c.identifier)["revision"]:
                            raise StorageError("实例或保留策略已变化，请重新预览")
                        _, current, _ = self._catalog()
                        available = {row["id"]: row for row in current if row["eligible"]}
                        if any(available.get(row["id"]) != row for row in job["items"]):
                            raise StorageError("副本或恢复关系在预览后变化，请重新预览")
                root = self.directory / ".storage-trash"
                if root.is_symlink():
                    raise StorageError("清理暂存目录不能是链接")
                root.mkdir(mode=0o700, exist_ok=True)
                trash = root / backup_id(job["id"])
                if trash.is_symlink():
                    raise StorageError("清理暂存目录不能是链接")
                trash.mkdir(mode=0o700, exist_ok=True)
                sources = [path.resolve() for path in self.mounts()]
                mounted = [path.resolve() for path in self.kernel_mounts()]
                # Check the whole approved group before retiring any rollback
                # index; a live mount must not leave a half-retired recovery point.
                for row in job["items"][job["items_done"]:]:
                    for item in row["paths"]:
                        source = self._source(item["path"])
                        destination = trash / item["path"].replace("/", "-")
                        for location in (source.resolve(), destination.resolve()):
                            if any(mount == location or location in mount.parents or mount in location.parents for mount in sources) or any(mount == location or location in mount.parents for mount in mounted):
                                raise StorageError("副本仍被容器或系统挂载，未清理")
                job.update(status="running", started=True, message="正在核对并清理过期副本", updated_at=int(time.time()))
                write_json(self.job_path, job)
                for index, row in enumerate(job["items"]):
                    if index < job["items_done"]:
                        continue
                    for item in row["paths"]:
                        source = self._source(item["path"])
                        destination = trash / item["path"].replace("/", "-")
                        if destination.exists() and source.exists():
                            raise StorageError("副本路径已被重新使用，停止清理")
                        if source.exists() or source.is_symlink():
                            if tree_info(source)["fingerprint"] != item["fingerprint"]:
                                raise StorageError("副本在预览后发生变化，请重新预览")
                            resolved = source.resolve()
                            if any(mount == resolved or resolved in mount.parents or mount in resolved.parents for mount in sources):
                                raise StorageError("副本仍被容器挂载，未清理")
                            os.rename(source, destination)
                            sync_directory(source.parent); sync_directory(trash)
                        if destination.exists() or destination.is_symlink():
                            info = destination.lstat()
                            if destination.is_symlink() or [info.st_dev, info.st_ino] != item["identity"]:
                                raise StorageError("清理暂存对象不匹配")
                            if destination.is_dir():
                                tree_info(destination)
                                if not shutil.rmtree.avoids_symlink_attacks:
                                    raise StorageError("当前系统缺少安全目录删除能力")
                                shutil.rmtree(destination)
                            else:
                                destination.unlink()
                            sync_directory(trash)
                    job.update(items_done=index + 1, updated_at=int(time.time()))
                    write_json(self.job_path, job)
                trash.rmdir()
                sync_directory(root)
                job.update(status="completed", message="过期副本已清理，当前数据未删除", free_after=shutil.disk_usage(self.directory).free)
        except Exception as error:
            job.update(status="failed", message=str(error) if isinstance(error, SetupError) else "清理未完成，未处理的副本保留")
        finally:
            job["updated_at"] = int(time.time())
            write_json(self.job_path, job)
            self.scan_path.unlink(missing_ok=True)
            if not job.get("started"):
                empty = self.directory / ".storage-trash" / backup_id(job["id"])
                if not empty.parent.is_symlink() and not empty.is_symlink():
                    try:
                        empty.rmdir()
                    except OSError:
                        pass

    def tick(self):
        if not self.settings()["policy"]["enabled"] or self.busy():
            return
        last = document(self.job_path) if self.job_path.exists() else {}
        previous = document(self.auto_path) if self.auto_path.exists() else {}
        if last.get("started") and last.get("status") == "failed":
            return
        if time.time() - max(last.get("updated_at", 0), previous.get("checked_at", 0)) < 3600:
            return
        write_json(self.auto_path, {"checked_at": int(time.time())})
        preview = self.plan()
        if preview["items"]:
            self.submit({"id": preview["id"], "confirm": True})

    def start(self):
        self.resume()
        def schedule():
            while not self.shutdown.is_set():
                for action in (self.c.capacity.scan, self.tick):
                    try:
                        action()
                    except (OSError, ValueError, SetupError, ConfigurationConflict):
                        pass
                if self.shutdown.wait(60):
                    break
        self.scheduler = threading.Thread(target=schedule, name="instance-retention", daemon=True)
        self.scheduler.start()
