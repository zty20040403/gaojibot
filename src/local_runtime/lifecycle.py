"""Durable service removal/reinstatement; never deletes instance or engine data.

The privileged dispatcher must verify the desktop's deployment ownership before
calling this module. The HTTP controller cannot submit arbitrary units or paths.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from .backups import backup_id
from .files import file_lock, read_json, status_directory, write_json
from .manager import Controller, PATH, ROOT
from .maintenance import sync_directory, write_bytes
from .releases import read_regular
from .services import APP_OWNER
from .setup_clients import SetupError


UNIT_ROOT = Path("/usr/local/lib/systemd/system")


class Lifecycle:
    controller_class = Controller
    verification_timeout = 60

    @property
    def deployment(self):
        return self.c.deployment

    def __init__(self, job, root=ROOT, *, unit_root=UNIT_ROOT):
        self.job, self.root, self.unit_root = backup_id(job), root, unit_root
        self.c = self.controller_class(job, root)
        self.directory = self.c.directory
        self.identifier = self.c.identifier
        self.marker = self.directory / "config/lifecycle.json"
        self.records = root / "lifecycles"
        self.records.mkdir(mode=0o700, exist_ok=True)
        info = self.records.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SetupError("实例维护记录目录归属不符")
        self.names = [f"gaoji-app-install-{self.job}.service", f"gaoji-app-{self.identifier}.service",
            f"gaoji-app-sandbox-{self.identifier}.service", f"gaoji-app-network-{self.identifier}.service",
            f"gaoji-app-control-{self.identifier}.service"]

    def systemctl(self, *arguments):
        try:
            result = subprocess.run(["systemctl", *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180,
                env={"PATH": PATH, "LANG": "C.UTF-8"}, check=False)
            if result.returncode:
                missing = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
                if arguments[0] != "show" or missing != {"LoadState": "not-found", "ActiveState": "inactive", "FragmentPath": "", "DropInPaths": ""}:
                    raise SetupError("本实例服务操作未确认，未修改其他服务")
            return result.stdout.decode().strip()
        except (OSError, subprocess.SubprocessError):
            raise SetupError("本实例服务操作尚未确认，数据保留，请继续原维护任务") from None

    def unit_state(self, name):
        raw = self.systemctl("show", name, "--property=LoadState,ActiveState,FragmentPath,DropInPaths")
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if set(values) != {"LoadState", "ActiveState", "FragmentPath", "DropInPaths"}:
            raise SetupError("无法可靠读取服务状态，未继续维护")
        if values["DropInPaths"] or values["FragmentPath"] not in {"", str(self.unit_root / name)}:
            raise SetupError("此服务含外部覆盖配置，未修改或停止它")
        if values["ActiveState"] not in {"active", "inactive", "failed"}:
            raise SetupError("服务正在切换状态，请稍后重试")
        return values

    def unit_snapshot(self, name):
        if name not in self.names:
            raise SetupError("服务不属于本实例")
        for directory in (self.unit_root, self.unit_root / "multi-user.target.wants"):
            if directory.exists() or directory.is_symlink():
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise SetupError("服务目录归属不符")
        filename = self.unit_root / name
        link = self.unit_root / "multi-user.target.wants" / name
        if link.is_symlink():
            if os.readlink(link) != "../" + name:
                raise SetupError("服务启动项指向其他位置，未修改")
        elif link.exists():
            raise SetupError("服务启动项不是本应用链接")
        if not filename.exists() and not filename.is_symlink():
            if link.is_symlink():
                raise SetupError("服务启动项没有对应定义，请先核对")
            body = None
        else:
            body = read_regular(filename, 65536).decode()
            prefix = body.splitlines()[0] if body else ""
            if name == self.names[0]:
                owned = re.fullmatch(r"# Gaoji installer " + self.job + r" [a-f0-9]{64}", prefix)
            else:
                owned = prefix == "# " + APP_OWNER
            if not owned:
                raise SetupError("同名服务不属于本应用，未修改")
        state = self.unit_state(name)
        if body is None and (state["FragmentPath"] or state["ActiveState"] == "active"):
            raise SetupError("同名服务仍由其他定义加载，未停止")
        if body is not None and state["FragmentPath"] != str(filename):
            raise SetupError("服务定义尚未正确加载，请先核对")
        return {"name": name, "body": body, "enabled": link.is_symlink(), "active": state["ActiveState"] == "active"}

    def pending_updates(self):
        folder = self.root / "updates"
        if folder.is_symlink():
            raise SetupError("更新记录目录无效")
        if folder.exists():
            for filename in folder.glob("*.json"):
                value = json.loads(read_regular(filename, 32768))
                if value.get("request", {}).get("job") == self.job and not value.get("finished") and value.get("status") != "completed":
                    raise SetupError("本实例有尚未结束的版本任务，未开始卸载")

    def pending_lifecycle(self, *, except_id=None):
        for filename in self.records.glob("*.json"):
            value = json.loads(read_regular(filename, 2 * 1024**2))
            if value.get("job") == self.job and value.get("id") != except_id and (
                value.get("status") in {"queued", "running"} or value.get("status") == "failed" and value.get("resumable") is True
            ):
                raise SetupError("本实例已有未结束的服务维护，请继续原任务")

    def load(self, identifier):
        record = json.loads(read_regular(self.records / (backup_id(identifier) + ".json"), 512 * 1024))
        if record.get("id") != identifier or record.get("job") != self.job or record.get("instance_id") != self.identifier:
            raise SetupError("维护任务不属于本实例")
        if record.get("kind") not in {"uninstall", "reinstall"}:
            raise SetupError("维护类型无效")
        return record

    def save(self, record, **changes):
        record.update(changes, updated_at=int(time.time()))
        write_json(self.records / (record["id"] + ".json"), record)
        public_root = status_directory(self.root, "lifecycles")
        path = public_root / (record["id"] + ".json")
        write_json(path, self.public(record))
        os.chmod(path, 0o644)

    def public(self, record):
        marker = read_json(self.marker) if self.marker.exists() else None
        state = marker.get("state", "unknown") if marker else "installed"
        return {key: record[key] for key in ("id", "job", "instance_id", "kind", "status", "stage", "message", "created_at", "updated_at", "resumable") if key in record} | {
            "schema": 1, "service_state": state,
            "units": [unit["name"] for unit in record["units"] if unit["body"] is not None],
            "data_retained": True, "data_directory": str(self.directory),
            "shared_software_not_removed": True}

    def compose_digest(self):
        content = read_regular(self.directory / "compose.json", 1024 * 1024)
        value = json.loads(content)
        if set(value) != {"name", "services"} or value["name"] != "gaoji-" + self.identifier or set(value["services"]) != {"database", "napcat", "bot"}:
            raise SetupError("运行配置不再是本实例的固定组件，未停止它们")
        if any("container_name" in service or "extends" in service for service in value["services"].values()):
            raise SetupError("运行配置引用其他容器，未继续维护")
        return hashlib.sha256(content).hexdigest()

    def prepare(self, kind):
        if kind not in {"uninstall", "reinstall"}:
            raise SetupError("请选择卸载服务或重新启用；本入口不删除数据")
        with file_lock(self.root / "jobs/install.lock", timeout=0.1):
            self.pending_updates()
            self.pending_lifecycle()
            config = self.c.store.read(self.identifier)
            original = None
            if kind == "uninstall":
                self.c.mutable()
                units = [self.unit_snapshot(name) for name in self.names]
            else:
                marker = read_json(self.marker)
                if marker.get("state") != "retained":
                    raise SetupError("请先完成原卸载任务")
                original = self.load(marker["id"])
                if original["kind"] != "uninstall" or original["status"] != "completed":
                    raise SetupError("没有可恢复的已完成卸载记录")
                units = original["units"]
                for name in self.names:
                    if self.unit_snapshot(name)["body"] is not None:
                        raise SetupError("服务名称已被重新使用，未覆盖")
            record = {"schema": 1, "id": uuid.uuid4().hex, "job": self.job, "instance_id": self.identifier,
                "kind": kind, "revision": config["revision"], "checksum": self.c.deployment["checksum"],
                "compose_sha256": self.compose_digest(),
                "units": units, "original_id": original["id"] if original else None,
                "components": original["components"] if original else self.c.services()["services"],
                "status": "preview", "stage": "prepare", "message": "维护清单已生成，尚未执行", "created_at": int(time.time()), "resumable": False}
            self.save(record)
            return self.public(record)

    def submit(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"id", "confirm"} or payload["confirm"] is not True:
            raise SetupError("请先确认实例服务维护清单")
        with file_lock(self.root / "jobs/install.lock", timeout=0.1):
            record = self.load(payload["id"])
            self.pending_lifecycle(except_id=record["id"])
            if record["status"] == "completed":
                return self.public(record)
            if record["status"] == "failed" and not record.get("resumable"):
                raise SetupError("原维护预览已失效，请重新预览")
            if record["status"] == "preview":
                try:
                    if time.time() - record["created_at"] > 900:
                        raise SetupError("维护预览已过期，请重新预览")
                    self.validate_version(record)
                except SetupError as error:
                    self.save(record, status="failed", stage="expired", message=str(error), resumable=False)
                    raise
            self.save(record, status="queued", message="服务维护已排队", resumable=True)
            return self.public(record)

    def validate_version(self, record):
        if self.c.store.read(self.identifier)["revision"] != record["revision"] or self.c.deployment["checksum"] != record["checksum"]:
            raise SetupError("实例配置或版本已变化，请重新预览")
        if self.compose_digest() != record["compose_sha256"]:
            raise SetupError("实例运行配置已变化，请重新预览")

    def disable_link(self, unit):
        current = self.unit_snapshot(unit["name"])
        if current["body"] != unit["body"]:
            raise SetupError("服务定义在确认后发生变化，未覆盖")
        link = self.unit_root / "multi-user.target.wants" / unit["name"]
        if link.is_symlink():
            link.unlink()
            sync_directory(link.parent)

    def remove_definition(self, unit):
        current = self.unit_snapshot(unit["name"])
        if current["body"] is None:
            return
        if current["body"] != unit["body"] or current["active"] or current["enabled"]:
            raise SetupError("服务定义已变化或尚未停用，未删除")
        (self.unit_root / unit["name"]).unlink()
        sync_directory(self.unit_root)
        self.systemctl("daemon-reload")

    def restore_definition(self, unit, *, restore_boot=True):
        if unit["body"] is None:
            return
        filename = self.unit_root / unit["name"]
        # A resumed restore may encounter its own already-written definition.
        current = self.unit_snapshot(unit["name"])
        if current["body"] not in {None, unit["body"]}:
            raise SetupError("服务名称已被其他配置占用，未覆盖")
        if current["body"] is None:
            write_bytes(filename, unit["body"].encode())
            os.chmod(filename, 0o644)
            sync_directory(filename.parent)
            self.systemctl("daemon-reload")
        if not restore_boot and current["enabled"]:
            self.disable_link(unit)
        elif restore_boot and unit["enabled"] and not current["enabled"]:
            link = self.unit_root / "multi-user.target.wants" / unit["name"]
            link.symlink_to("../" + unit["name"])
            sync_directory(link.parent)

    def run(self, identifier):
        with file_lock(self.root / "jobs/install.lock", timeout=0.1):
            record = self.load(identifier)
            if record["status"] == "completed":
                return self.public(record)
            if record["status"] not in {"queued", "running", "failed"}:
                raise SetupError("维护尚未确认")
            if record["status"] == "failed" and not record.get("resumable"):
                raise SetupError("请重新预览，不可继续失效任务")
            try:
                with ExitStack() as locks:
                    for name in (".backup-transfer.lock", ".maintenance.lock"):
                        locks.enter_context(file_lock(self.directory / name, timeout=0.1))
                    self.pending_updates()
                    self.pending_lifecycle(except_id=record["id"])
                    if record["stage"] == "compensating":
                        self.abandon_freeze(record)
                        self.save(record, status="failed", resumable=False, message="已放弃冲突的卸载并恢复管理服务，请重新预览")
                        return self.public(record)
                    self.validate_version(record)
                    if record.get("started"):
                        self.systemctl("daemon-reload")
                    if record["kind"] == "uninstall":
                        self.uninstall(record)
                    else:
                        self.reinstall(record)
                    self.save(record, status="completed", stage="verified", resumable=False,
                        message="本实例服务已卸载，数据库、文件、QQ 会话和容器均保留" if record["kind"] == "uninstall" else "原实例服务已恢复，数据未替换")
            except Exception as error:
                self.save(record, status="failed", resumable=bool(record.get("started")),
                    message=str(error) if isinstance(error, SetupError) else "维护尚未完成，数据与原任务保留，请继续原任务核对")
            return self.public(record)

    def uninstall(self, record):
        try:
            self.c.mutable(lifecycle_job_id=record["id"])
        except SetupError:
            if record.get("started") and record["stage"] == "disabling":
                self.abandon_freeze(record)
            raise
        if not record.get("started"):
            if [self.unit_snapshot(name) for name in self.names] != record["units"]:
                raise SetupError("服务状态已变化，请重新预览")
            write_json(self.marker, {"id": record["id"], "state": "removing"})
            self.save(record, status="running", stage="disabling", started=True, message="正在取消本实例自动启动")
        else:
            if read_json(self.marker).get("id") != record["id"]:
                raise SetupError("实例维护标记已变化")
        # Reboot cannot revive a unit already removed from its owned target.
        for unit in record["units"]:
            if unit["body"] is not None:
                current = self.unit_snapshot(unit["name"])
                if current["body"] is not None:
                    self.disable_link(unit)
        self.systemctl("daemon-reload")
        control = self.names[-1]
        if self.unit_snapshot(control)["body"] is not None:
            self.systemctl("stop", control)
        self.c = self.controller_class(self.job, self.root)
        try:
            self.c.mutable(lifecycle_job_id=record["id"])
        except SetupError:
            if record["stage"] == "disabling":
                self.abandon_freeze(record)
            raise
        self.save(record, stage="stopping", message="正在停止并复查本实例组件")
        record.setdefault("sandbox_was_enabled", self.c.sandbox.enabled_path.exists())
        self.save(record)
        self.c.sandbox.pause(service_stopped=self.unit_snapshot(self.names[2])["body"] is None)
        for name in (self.names[2], self.names[3], self.names[1], self.names[0]):
            if self.unit_snapshot(name)["body"] is not None:
                self.systemctl("stop", name)
        self.c.compose(["stop"], timeout=180)
        if any(row["state"] not in {"exited", "dead", "created"} for row in self.c.services()["services"]):
            raise SetupError("仍有本实例组件未确认停止，未删除服务定义")
        self.save(record, stage="removing", message="组件已停止，正在移除本实例服务定义")
        for unit in record["units"]:
            if unit["body"] is not None:
                self.remove_definition(unit)
        if any(self.unit_snapshot(name)["body"] is not None for name in self.names):
            raise SetupError("仍有本实例服务定义，卸载尚未完成")
        write_json(self.marker, {"id": record["id"], "state": "retained"})

    def abandon_freeze(self, record):
        marker = read_json(self.marker) if self.marker.exists() else None
        if marker and marker.get("id") != record["id"] or marker is None and record["stage"] != "compensating":
            raise SetupError("维护标记变化，未自行恢复服务")
        self.save(record, stage="compensating", message="已有另一任务开始，正在恢复原管理入口")
        for unit in record["units"]:
            self.restore_definition(unit)
        self.marker.unlink(missing_ok=True)
        sync_directory(self.marker.parent)
        self.systemctl("daemon-reload")
        if record["units"][-1]["active"]:
            self.systemctl("start", self.names[-1])
            if not self.unit_snapshot(self.names[-1])["active"]:
                raise SetupError("原管理服务尚未恢复，请继续原维护任务核对")
        record.update(started=False, stage="conflict")

    def reinstall(self, record):
        marker = read_json(self.marker) if self.marker.exists() else None
        if marker and marker.get("id") not in {record["original_id"], record["id"]}:
            raise SetupError("实例由另一维护任务接管")
        if not record.get("started"):
            if not marker or marker.get("state") != "retained" or marker["id"] != record["original_id"]:
                raise SetupError("原卸载状态已变化")
            if any(self.unit_snapshot(name)["body"] is not None for name in self.names):
                raise SetupError("服务名称被重新使用，未覆盖")
            write_json(self.marker, {"id": record["id"], "state": "restoring"})
            self.save(record, status="running", stage="restoring", started=True, message="正在恢复已保留的服务定义")
        for unit in record["units"]:
            self.restore_definition(unit, restore_boot=unit["name"] != self.names[0])
        self.systemctl("daemon-reload")
        original = self.load(record["original_id"])
        self.save(record, stage="resuming", message="正在重新启动原实例并核对服务")
        if self.marker.exists():
            self.marker.unlink()
            sync_directory(self.marker.parent)
        if original.get("sandbox_was_enabled"):
            self.c.sandbox.start()
        for unit in record["units"]:
            # Keep the old bootstrap for audit, never restart or boot-enable it.
            if unit["name"] == self.names[0]:
                continue
            if unit["active"] and unit["body"] is not None:
                self.systemctl("start", unit["name"])
                if not self.unit_snapshot(unit["name"])["active"]:
                    raise SetupError("服务尚未确认恢复，请继续原任务")
        expected = {row["name"] for row in record["components"] if row["state"] == "running"}
        actual = {row["name"] for row in self.c.services()["services"] if row["state"] == "running"}
        if actual - expected:
            self.c.compose(["stop", *sorted(actual - expected)])
        if expected:
            self.c.compose(["up", "--detach", "--no-deps", *sorted(expected)], timeout=180)
        deadline = time.monotonic() + self.verification_timeout
        while True:
            states = {row["name"]: row for row in self.c.services()["services"]}
            running = {name for name, row in states.items() if row["state"] == "running"}
            if running == expected and ("database" not in expected or states["database"].get("health") == "healthy"):
                break
            if time.monotonic() >= deadline:
                raise SetupError("服务已请求启动，但运行组件或数据库健康尚未通过，不能报告恢复完成")
            time.sleep(0.5)
