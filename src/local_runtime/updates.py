"""Database-copy updates, run by a persistent worker outside the controller.

The worker must exclusively own the stopped controller. install_services writes
only this instance's unit definitions and reloads systemd; it must not start them.
The journal is committed before any new-version writer is allowed to start.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable

from .backups import BackupError, backup_id
from .files import file_lock, read_json, write_json
from .maintenance import Maintenance, sync_directory
from .releases import RUNTIME_CHECK_TIMEOUT, ReleaseError, checksum, load_release, runtime_check_arguments
from .setup_clients import SetupError
from src.storage_admission import CapacityError


class RuntimeUpdate(Maintenance):
    def __init__(self, controller, install_services: Callable[[dict], None]):
        super().__init__(controller)
        self.install_services = install_services
        self.record_path = controller.root / "jobs" / (controller.job + ".json")
        self.history = self.directory / "updates"
        if self.history.is_symlink():
            raise BackupError("更新记录目录不能是链接")
        self.history.mkdir(mode=0o700, exist_ok=True)

    def runtime_command(self, arguments: list[str], *, timeout: int, capture: bool = False) -> bytes:
        step = "镜像查询" if arguments[1:3] == ["image", "ls"] else "镜像构建" if arguments[1] == "build" else "运行镜像离线自检"
        try:
            result = subprocess.run(arguments, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture or arguments[1:3] == ["image", "ls"] else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=timeout, env=self.c.process_environment(), check=False)
        except subprocess.TimeoutExpired:
            raise SetupError(f"{step}超过 {timeout} 秒，尚未暂停机器人") from None
        if result.returncode:
            raise SetupError(f"{step}失败，尚未暂停机器人")
        return result.stdout or b""

    def prepare_release(self):
        release = load_release(self.c.root / "releases", self.c.operation["target_checksum"])
        docker = self.c.deployment["docker"]
        present = self.runtime_command([docker, "image", "ls", "--quiet", release.image], timeout=30).strip()
        if not present:
            from .image_install import prepare_image
            prepare_image(docker, release.directory, release.checksum, release.image, self.runtime_command,
                lambda message: self.c.persist(stage="update_image_build", message=message + "，旧实例继续运行"))
        self.c.persist(stage="update_runtime_check", message="正在离线检查源码、依赖、中文字体和浏览器，最长五分钟")
        self.runtime_command(runtime_check_arguments(docker, release.image), timeout=RUNTIME_CHECK_TIMEOUT)
        return release

    def set_deployment(self, deployment: dict) -> None:
        deployment = dict(deployment)
        if getattr(self.c, "controller_checksum", None):
            deployment["controller_checksum"] = checksum(self.c.controller_checksum)
        write_json(self.record_path, deployment)
        self.c.deployment.clear()
        self.c.deployment.update(deployment)
        self.c.release = self.c.root / "releases" / checksum(deployment["checksum"])
        self.install_services(deployment)

    def original(self) -> None:
        if self.state.get("committed"):
            raise BackupError("新版本已允许写入，不能自动丢弃新数据；请明确选择回退")
        self.set_deployment(self.state["original_deployment"])
        super().original()

    def rollback_source(self, target: str) -> dict | None:
        if self.c.operation["kind"] != "rollback":
            return None
        if self.c.operation.get("confirm_data_replacement") is not True:
            raise BackupError("回退将恢复升级前数据；请明确确认，新数据会另行保留")
        previous = read_json(self.history / (backup_id(self.c.operation.get("rollback_id")) + ".json"))
        if (not previous.get("committed") or previous.get("target_checksum") != self.c.deployment["checksum"]
                or previous.get("original_deployment", {}).get("checksum") != target):
            raise BackupError("此回退点不匹配当前运行版本，请重新选择")
        return self.backups.check(previous["backup_id"], target)

    def switch(self, candidate: Path, release) -> None:
        self.save(stage="committing")
        self.c.persist(stage="update_switch", message="副本验证通过，正在切换版本；原数据仍保留")
        self.replace_candidate(candidate)
        deployment = {**self.state["original_deployment"], "checksum": release.checksum, "runtime_image": release.image}
        self.set_deployment(deployment)
        self.refresh_client()
        self.c.render(False)
        # A reboot at this point must not allow NapCat or Bot to write before
        # verification commits. Explicit maintenance commands still work.
        document = read_json(self.directory / "compose.json")
        for service in document["services"].values():
            service.update(profiles=["maintenance"], restart="no")
        write_json(self.directory / "compose.json", document)
        write_json(self.c.desired_path, {"bot_enabled": False, "revision": self.c.store.read(self.c.identifier)["revision"]})
        self.c.compose(["up", "--detach", "--wait", "--wait-timeout", "90", "database"], timeout=120)
        self.command(["run", "--rm", "--no-deps", "-T", "bot", "check-database"], timeout=60)
        self.save(stage="committed", committed=True, activation_after=int(time.time()))
        self.finish()

    def verify_online(self, enabled: bool) -> None:
        self.command(["run", "--rm", "--no-deps", "-T", "bot", "check-database"], timeout=60)
        if not enabled:
            return
        qq = self.c.store.read(self.c.identifier)["settings"]["bot_qq"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            path = self.directory / "state/desktop-health.json"
            try:
                health = read_json(path) if path.exists() else {}
                if (health.get("updated_at", 0) >= self.state["activation_after"] and health.get("qq") == qq
                        and health.get("connected") is True):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise SetupError("新版本已切换，但尚未确认 QQ 连接；新旧数据均保留，不自动回退")

    def finish(self) -> None:
        if not self.state.get("committed"):
            raise BackupError("版本切换尚未提交，不能恢复消息写入")
        # Write the rollback receipt before opening traffic. Every repeat after a
        # crash finishes this committed version; it never replays the migration.
        self.save(stage="resuming")
        write_json(self.history / (self.state["id"] + ".json"), self.state)
        enabled = self.state["desired"].get("bot_enabled") is True
        self.c.render(enabled)
        revision = self.c.store.read(self.c.identifier)["revision"]
        write_json(self.c.desired_path, {"bot_enabled": enabled, "revision": revision})
        if self.state["sandbox_enabled"]:
            self.c.sandbox.start()
        running = list(self.state["running"])
        if enabled and "bot" not in running:
            running.append("bot")
        if not enabled:
            running = [name for name in running if name != "bot"]
        if running:
            self.c.compose(["up", "--detach", "--wait", "--wait-timeout", "90", "--no-deps", *running], timeout=120)
        self.c.persist(stage="update_verify", message="版本已切换，正在核对数据库和 QQ 连接")
        self.verify_online(enabled)
        self.save(stage="verified")
        write_json(self.history / (self.state["id"] + ".json"), self.state)
        candidate = self.workspace / "candidate"
        if candidate.exists():
            shutil.rmtree(candidate)
        self.c.persist(status="completed", stage="updated" if self.state["kind"] == "update" else "rolled_back",
            message="版本和数据库已切换并通过检查；已保留切换前备份及原数据", backup_id=self.state["backup_id"])
        self.marker.unlink()
        sync_directory(self.marker.parent)

    def run_update(self) -> None:
        with file_lock(self.directory / ".maintenance.lock", timeout=0.1):
            if self.state and (self.state.get("id") != self.c.operation.get("id")
                    or self.state.get("kind") not in {"update", "rollback"}
                    or self.state.get("target_checksum") != self.c.operation.get("target_checksum")):
                raise BackupError("已有另一项未结束维护，未开始更新")
            if not self.state and self.c.operation.get("status") == "completed":
                receipt = read_json(self.history / (backup_id(self.c.operation["id"]) + ".json"))
                if receipt.get("target_checksum") != self.c.operation.get("target_checksum"):
                    raise BackupError("操作编号已用于其他版本，未重复执行")
                return
            if not self.state and self.c.operation.get("status") == "failed":
                # A rollback may already have reopened the old instance. Its
                # snapshot is now stale; retrying needs a fresh operation ID.
                return
            try:
                if self.state:
                    if self.state.get("committed"):
                        self.finish()
                    else:
                        self.original()
                        self.c.persist(status="failed", stage="update_reverted", message="上次更新在提交前中断，已恢复原版本和数据；可重新发起更新")
                    return
                if self.c.operation["kind"] not in {"update", "rollback"}:
                    raise BackupError("无效版本操作")
                expected = checksum(self.c.operation["expected_checksum"])
                target = checksum(self.c.operation["target_checksum"])
                if expected != self.c.deployment["checksum"] or target == expected:
                    raise BackupError("当前运行版本已变化或已是目标版本，未重复更新")
                source = self.rollback_source(target)
                self.c.persist(status="running", stage="update_prepare", message="正在核对新版本，旧实例继续运行")
                release = self.prepare_release()
                self.begin(self.c.operation["kind"], metadata={"original_deployment": dict(self.c.deployment),
                    "target_checksum": target, "committed": False})
                snapshot = self.snapshot()
                self.save(backup_id=snapshot["id"])
                candidate = self.stage_restore(source or snapshot, runtime_image=release.image, images=release.images,
                    archive_checksum=target if source else expected, migrate=source is None)
                self.switch(candidate, release)
            except Exception as error:
                if self.state and self.state.get("committed"):
                    self.c.persist(status="failed", stage="update_unverified",
                        message="版本已切换，但启动或验收尚未完成；保留新旧数据，请继续原更新任务，不要重装")
                    return
                recovered = not self.marker.exists()
                if not recovered:
                    try:
                        self.state = read_json(self.marker)
                        self.original()
                        recovered = True
                    except Exception:
                        pass
                message = str(error) if isinstance(error, (BackupError, ReleaseError, SetupError, CapacityError)) else "版本更新未完成"
                self.c.persist(status="failed", stage="update_reverted" if recovered else "recovery_required",
                    message=message + ("；原版本和数据已保留" if recovered else "；尚未恢复运行，原数据仍保留"))
