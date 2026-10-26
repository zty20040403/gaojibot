"""Confirmed deletion of one retained instance, with a journal outside its data."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
import shutil
import stat
import time
import uuid

from .backups import backup_id
from .files import file_lock, read_json, write_json
from .lifecycle import Lifecycle, UNIT_ROOT
from .maintenance import sync_directory
from .manager import ROOT
from .purge_inventory import PurgeInventory, fingerprint, tree_snapshot
from .releases import checksum, read_regular
from .setup_clients import SetupError
from .storage import Storage


def private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SetupError("删除任务目录归属异常")


class Purge:
    lifecycle_class = Lifecycle
    inventory_class = PurgeInventory

    def __init__(self, job, root=ROOT, *, unit_root=UNIT_ROOT):
        self.job, self.root, self.unit_root = backup_id(job), root, unit_root
        for name in ("jobs", "data", "data/instances"):
            private_directory(root / name)
        self.job_path = root / "jobs" / (job + ".json")
        self.deployment = json.loads(read_regular(self.job_path, 65536))
        self.identifier = backup_id(self.deployment.get("instance_id"))
        checksum(self.deployment.get("checksum"))
        checksum(self.deployment.get("controller_checksum", self.deployment["checksum"]))
        self.directory = root / "data/instances" / self.identifier
        self.records = root / "lifecycles"
        private_directory(self.records)
        self.inventory = self.inventory_class(self.identifier, self.directory, self.deployment)

    def load(self, identifier):
        value = json.loads(read_regular(self.records / (backup_id(identifier) + ".json"), 2 * 1024**2))
        if value.get("id") != identifier or value.get("job") != self.job or value.get("instance_id") != self.identifier or value.get("kind") != "purge":
            raise SetupError("删除任务不属于本实例")
        return value

    def save(self, record, **changes):
        record.update(changes, updated_at=int(time.time()))
        if len(json.dumps(record).encode()) > 2 * 1024**2:
            raise SetupError("删除清单过大，未继续执行")
        write_json(self.records / (record["id"] + ".json"), record)
        public = self.root / "status/lifecycles" / (record["id"] + ".json")
        if public.parent.is_symlink() or not public.parent.is_dir():
            raise SetupError("删除回执目录不可用")
        write_json(public, self.public(record))
        os.chmod(public, 0o644)

    def public(self, record):
        state = "removed" if record["status"] == "completed" else "purging" if record.get("started") else "retained"
        return {key: record[key] for key in ("id", "job", "instance_id", "kind", "status", "stage", "message", "created_at", "updated_at", "resumable")} | {
            "schema": 1, "service_state": state, "units": [unit["name"] for unit in record["task_units"] if unit["body"]],
            "data_retained": False, "data_directory": str(self.directory), "shared_software_not_removed": True,
            "delete_resources": {kind: [{"id": row["id"], "name": row["name"]} for row in rows] for kind, rows in record["resources"].items()},
            "data_bytes": record["tree"]["bytes"], "audit_retained": True,
            "free_before": record["free_before"], **({"free_after": record["free_after"]} if "free_after" in record else {})}

    def context(self, operation=None):
        current = json.loads(read_regular(self.job_path, 65536))
        if current.get("purged_at"):
            raise SetupError("原实例已经删除，不能从旧安装任务重新创建")
        if fingerprint(current) != fingerprint(self.deployment):
            raise SetupError("部署记录已变化，未开始删除")
        life = self.lifecycle_class(self.job, self.root, unit_root=self.unit_root)
        if life.identifier != self.identifier:
            raise SetupError("实例归属已变化，未开始删除")
        life.pending_updates()
        life.pending_lifecycle(except_id=operation)
        marker = read_json(life.marker)
        original = life.load(marker.get("id"))
        if marker.get("state") != "retained" or original["kind"] != "uninstall" or original["status"] != "completed":
            raise SetupError("请先完成卸载服务，再单独确认删除数据")
        life.c.mutable(lifecycle_job_id=marker["id"])
        if any(life.unit_snapshot(name)["body"] is not None for name in life.names):
            raise SetupError("本实例服务定义仍存在，未开始删除数据")
        return life, marker["id"]

    def task_units(self, life, *, operation=None):
        targets = []
        for folder, kind in ((self.records, "lifecycle"), (self.root / "updates", "update")):
            if folder.is_symlink():
                raise SetupError("维护记录目录不能是链接")
            for path in folder.glob("*.json"):
                record = json.loads(read_regular(path, 2 * 1024**2))
                target = record.get("request", {}) if kind == "update" else record
                if target.get("job") != self.job or target.get("id") == operation:
                    continue
                identifier = backup_id(target.get("id"))
                name = "gaoji-app-" + kind + "-" + identifier + ".service"
                life.names.append(name)
                unit = life.unit_snapshot(name)
                if unit["body"]:
                    module = "src.local_runtime." + kind + "_worker"
                    if module not in unit["body"] or "--operation " + identifier not in unit["body"] or unit["active"]:
                        raise SetupError("历史维护执行器归属或停止状态未核实")
                    targets.append(unit)
        return targets

    def locks(self, stack):
        for name in (".backup-transfer.lock", ".maintenance.lock", ".storage.lock"):
            stack.enter_context(file_lock(self.directory / name, timeout=0.1))

    def prepare(self, kind="purge"):
        if kind != "purge":
            raise SetupError("此入口只生成彻底删除清单")
        with file_lock(self.root / "jobs/install.lock", timeout=0.1), ExitStack() as stack:
            life, original = self.context()
            self.locks(stack)
            units = self.task_units(life)
            resources = self.inventory.snapshot()
            tree = tree_snapshot(self.directory)
            record = {"schema": 1, "id": uuid.uuid4().hex, "job": self.job, "instance_id": self.identifier,
                "kind": "purge", "status": "preview", "stage": "prepare", "resumable": False,
                "message": "删除清单已生成，尚未删除任何数据", "created_at": int(time.time()),
                "original_id": original, "deployment_fingerprint": fingerprint(self.deployment), "task_units": units,
                "resources": resources, "tree": tree, "free_before": shutil.disk_usage(self.directory).free}
            self.save(record)
            return self.public(record)

    def submit(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"id", "confirm", "erase_instance_id"}
                or payload["confirm"] is not True or payload["erase_instance_id"] != self.identifier):
            raise SetupError("必须单独确认永久删除这个实例的数据库、会话和文件")
        with file_lock(self.root / "jobs/install.lock", timeout=0.1):
            record = self.load(payload["id"])
            if record["status"] == "completed":
                return self.public(record)
            if record["status"] == "failed" and not record["resumable"]:
                raise SetupError("删除清单已失效，请重新预览")
            if record["status"] == "preview":
                try:
                    if time.time() - record["created_at"] > 900:
                        raise SetupError("删除清单已过期，请重新预览")
                    self.verify_initial(record)
                except SetupError as error:
                    self.save(record, status="failed", stage="expired", message=str(error), resumable=False)
                    raise
            self.save(record, status="queued", message="删除任务已保存", resumable=True)
            return self.public(record)

    def verify_initial(self, record):
        life, original = self.context(record["id"])
        if (original != record["original_id"] or fingerprint(self.deployment) != record["deployment_fingerprint"]
                or self.task_units(life, operation=record["id"]) != record["task_units"]
                or self.inventory.snapshot() != record["resources"]
                or tree_snapshot(self.directory) != record["tree"]):
            raise SetupError("数据或资源在预览后变化，请重新预览")
        return life

    def quarantine(self, record):
        parent = self.root / "data/.purge"
        parent.mkdir(mode=0o700, exist_ok=True)
        private_directory(parent)
        return parent / record["id"]

    def verify_tree_mounts(self, target, record):
        locations = (target.resolve(), self.directory.resolve())
        for mounted in Storage.kernel_mounts():
            if any(mounted == location or location in mounted.parents for location in locations):
                raise SetupError("删除区域出现系统挂载，未继续")
        remaining = self.inventory.remaining(record["resources"], retired_path=target)
        if any(remaining.values()):
            raise SetupError("仍有实例容器资源，未删除文件数据")

    def remove_data(self, record):
        target = self.quarantine(record)
        self.verify_tree_mounts(target, record)
        if (target.exists() or target.is_symlink()) and (self.directory.exists() or self.directory.is_symlink()):
            raise SetupError("原数据路径被重新使用，未继续删除")
        if self.directory.exists() or self.directory.is_symlink():
            if tree_snapshot(self.directory) != record["tree"]:
                raise SetupError("数据在确认后发生变化，未继续删除")
            os.rename(self.directory, target)
            sync_directory(self.directory.parent); sync_directory(target.parent)
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != record["tree"]["identity"]:
                raise SetupError("删除暂存目录与原数据不符")
            tree_snapshot(target)
            if not shutil.rmtree.avoids_symlink_attacks:
                raise SetupError("当前系统不支持安全目录删除，未继续")
            self.save(record, stage="deleting", message="正在删除已确认的实例数据")
            self.verify_tree_mounts(target, record)
            shutil.rmtree(target)
            sync_directory(target.parent)

    def retire_access(self):
        parent = self.root / "control-access"
        if parent.is_symlink():
            raise SetupError("旧管理凭据目录异常，未删除")
        path = parent / (self.job + ".json")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            # Only unlink the exact installation-owned directory entry, never
            # follow it or enumerate another user's credential files.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SetupError("旧管理凭据条目异常，未删除")
            path.unlink()
            sync_directory(parent)

    def run(self, identifier):
        with file_lock(self.root / "jobs/install.lock", timeout=0.1), ExitStack() as stack:
            record = self.load(identifier)
            if record["status"] == "completed":
                return self.public(record)
            if record["status"] not in {"queued", "running", "failed"} or record["status"] == "failed" and not record["resumable"]:
                raise SetupError("删除尚未确认或清单已失效")
            try:
                current = json.loads(read_regular(self.job_path, 65536))
                if not (current.get("purged_at") and current.get("purge_task") == record["id"]) and fingerprint(current) != record["deployment_fingerprint"]:
                    raise SetupError("部署记录已变化，未继续删除")
                if record["stage"] not in {"retiring", "deleting", "metadata"}:
                    info = self.directory.lstat()
                    if [info.st_dev, info.st_ino] != record["tree"]["identity"] or not stat.S_ISDIR(info.st_mode):
                        raise SetupError("原实例目录已被替换，未写入或删除")
                    self.locks(stack)
                if not record.get("started"):
                    life = self.verify_initial(record)
                    self.save(record, status="running", stage="resources", started=True, message="正在删除已确认的停止容器与工作卷")
                elif record["stage"] not in {"retiring", "deleting", "metadata"}:
                    life, _ = self.context(record["id"])
                    for unit in record["task_units"]:
                        life.names.append(unit["name"])
                if record["stage"] not in {"retiring", "deleting", "metadata"}:
                    for unit in record["task_units"]:
                        current = life.unit_snapshot(unit["name"])
                        if current["body"] is not None:
                            life.disable_link(unit)
                            life.remove_definition(unit)
                    for kind in ("containers", "volumes", "networks"):
                        for row in record["resources"][kind]:
                            self.inventory.remove(record["resources"], kind, row["id"])
                    self.save(record, stage="retiring", message="容器资源已移除，正在封存待删除数据目录")
                self.remove_data(record)
                self.save(record, stage="metadata", message="正在撤销旧管理凭据并复查删除结果")
                self.retire_access()
                current = json.loads(read_regular(self.job_path, 65536))
                if current.get("purge_task") not in {None, record["id"]}:
                    raise SetupError("部署记录由另一删除任务接管")
                if not current.get("purged_at") and fingerprint(current) != record["deployment_fingerprint"]:
                    raise SetupError("部署记录已变化，未覆盖")
                current.update(purged_at=int(time.time()), purge_task=record["id"])
                write_json(self.job_path, current)
                if self.directory.exists() or self.directory.is_symlink() or self.quarantine(record).exists():
                    raise SetupError("仍有实例数据，未报告删除完成")
                self.verify_tree_mounts(self.quarantine(record), record)
                self.save(record, status="completed", stage="verified", resumable=False,
                    message="本实例数据库、QQ 会话、文件及容器资源已删除；仅保留维护审计，共享软件和镜像未删除",
                    free_after=shutil.disk_usage(self.root / "data").free)
            except Exception as error:
                self.save(record, status="failed", resumable=bool(record.get("started")),
                    message=str(error) if isinstance(error, SetupError) else "删除尚未完成，未处理内容和原任务保留，请继续原任务")
            return self.public(record)
