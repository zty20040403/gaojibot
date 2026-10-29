"""Recoverable maintenance of exactly one desktop-managed instance."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .official_configuration import backup_bundle, restore_bundle, platforms
from .backups import BackupError, BackupStore, backup_id, regular_file, verify_archive, walk_data
from .files import file_lock, read_json, write_json
from .runtime import compose_document, runtime_environment
from .setup_clients import NapCatSetup, SetupError
from .qq_handover import clear_handover, read_handover, require_handover, start_allowed


DATA = ("state", "files", "napcat", "database")
CONFIG = ("instance.json", "config/secrets.json", "config/runtime.json", "config/admin.secret", "config/postgres.password")
RESERVE = 512 * 1024 * 1024


def write_bytes(filename: Path, value: bytes, *, bot_owned: bool = False) -> None:
    temporary = filename.with_name(filename.name + ".restore-partial")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            if bot_owned and os.geteuid() == 0:
                os.fchown(stream.fileno(), 10001, 10001)
        os.replace(temporary, filename)
        sync_directory(filename.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def own_tree(root: Path) -> None:
    if os.geteuid() == 0:
        for parent, children, files in os.walk(root, followlinks=False):
            os.chown(parent, 10001, 10001, follow_symlinks=False)
            for name in files:
                os.chown(Path(parent) / name, 10001, 10001, follow_symlinks=False)


class Maintenance:
    def __init__(self, controller):
        self.c = controller
        self.directory = controller.directory
        self.marker = self.directory / "config/maintenance.json"
        self.backups = BackupStore(self.directory)
        self.state = read_json(self.marker) if self.marker.exists() else None

    def save(self, **changes) -> None:
        self.state.update(changes)
        write_json(self.marker, self.state)

    @property
    def workspace(self) -> Path:
        root = self.directory / "recovery"
        if root.is_symlink():
            raise BackupError("恢复目录不能是链接")
        root.mkdir(mode=0o700, exist_ok=True)
        workspace = root / backup_id(self.state["id"])
        if workspace.is_symlink():
            raise BackupError("恢复目录不能是链接")
        workspace.mkdir(mode=0o700, exist_ok=True)
        return workspace

    def command(self, arguments: list[str], *, compose: Path | None = None, source: Path | None = None,
                output: Path | None = None, timeout: int = 600) -> bytes:
        command = [self.c.deployment["docker"], "compose", "--file", str(compose or self.directory / "compose.json"), *arguments]
        incoming = source.open("rb") if source else subprocess.DEVNULL
        outgoing = output.open("xb") if output else subprocess.PIPE
        try:
            if output:
                os.fchmod(outgoing.fileno(), 0o600)
            result = subprocess.run(command, stdin=incoming, stdout=outgoing, stderr=subprocess.PIPE, timeout=timeout,
                env=self.c.process_environment(), check=False)
            if result.returncode:
                raise SetupError("数据库维护操作失败，原始数据仍保留")
            if output:
                outgoing.flush()
                os.fsync(outgoing.fileno())
            return result.stdout or b""
        except subprocess.TimeoutExpired:
            raise SetupError("数据库维护操作超时，原始数据仍保留") from None
        finally:
            if source:
                incoming.close()
            if output:
                outgoing.close()

    def begin(self, kind: str, *, metadata: dict | None = None) -> None:
        original = read_json(self.directory / "compose.json")
        running = [row["name"] for row in self.c.services()["services"] if row["state"] == "running"]
        saved = {}
        for name in CONFIG:
            with regular_file(self.directory / name) as stream:
                saved[name] = base64.b64encode(stream.read()).decode("ascii")
        self.state = {"schema": 1, "id": self.c.operation["id"], "kind": kind, "stage": "preparing", "created_at": int(time.time()),
            "compose": original, "running": running, "original_config": saved,
            "sandbox_enabled": self.c.sandbox.enabled_path.exists(),
            "qq_handover": read_handover(self.directory),
            "desired": read_json(self.c.desired_path) if self.c.desired_path.exists() else {"bot_enabled": False}}
        if metadata:
            if self.state.keys() & metadata.keys():
                raise BackupError("维护元数据不能覆盖恢复记录")
            self.state.update(metadata)
        write_json(self.marker, self.state)
        # Keep a reboot or the instance unit from starting writers mid-snapshot.
        paused = json.loads(json.dumps(original))
        for service in paused["services"].values():
            service["profiles"] = ["maintenance"]
            service["restart"] = "no"
        write_json(self.directory / "compose.json", paused)
        self.c.compose(["stop", *[name for name in ("bot", "napcat") if name in original["services"]]], timeout=90)
        self.c.sandbox.pause()

    def stop_candidate(self) -> None:
        compose = self.workspace / "candidate-compose.json"
        if compose.exists():
            self.command(["down", "--timeout", "30"], compose=compose, timeout=60)

    def original(self) -> None:
        if self.state["stage"] == "resuming":
            self.resume_backup()
            self.refresh_client()
            return
        self.save(stage="rolling_back")
        self.c.sandbox.pause()
        self.stop_candidate()
        self.c.compose(["stop", *[name for name in ("bot", "napcat", "database") if name in read_json(self.directory / "compose.json")["services"]]], timeout=90)
        rollback = self.workspace / "rollback"
        discarded = self.workspace / "discarded"
        discarded.mkdir(mode=0o700, exist_ok=True)
        for name in DATA:
            old, live = rollback / name, self.directory / name
            if old.exists():
                if live.exists():
                    os.rename(live, discarded / name)
                os.rename(old, live)
                sync_directory(self.directory)
                sync_directory(rollback)
        for name, contents in self.state["original_config"].items():
            if name not in CONFIG:
                raise BackupError("恢复日志包含不支持的配置路径")
            write_bytes(self.directory / name, base64.b64decode(contents, validate=True), bot_owned=name in {"config/admin.secret", "config/runtime.json"})
        self.refresh_client()
        write_json(self.c.desired_path, self.state["desired"])
        if "qq_handover" in self.state:
            if self.state["qq_handover"] is None:
                current = read_handover(self.directory)
                if current is not None and current["operation_id"] != self.state["id"]:
                    raise BackupError("QQ 接管记录已变化，未覆盖")
                clear_handover(self.directory)
            else:
                write_json(self.directory / "config/qq-handover.json", self.state["qq_handover"])
        write_json(self.directory / "compose.json", self.state["compose"])
        if rollback.exists() and any(rollback.iterdir()):
            raise BackupError("仍有原数据尚未归位，已停止清理恢复目录")
        self.save(stage="resuming")
        if self.state.get("sandbox_enabled"):
            self.c.sandbox.start()
        if self.state["running"]:
            self.c.compose(["up", "--detach", "--wait", "--wait-timeout", "90", "--no-deps", *self.state["running"]], timeout=120)
        shutil.rmtree(self.workspace)
        self.marker.unlink()
        sync_directory(self.marker.parent)
        self.refresh_client()

    def refresh_client(self) -> None:
        config, private = self.c.store.snapshot(self.c.identifier)
        self.c.napcat = NapCatSetup(config["ports"]["napcat"], private["napcat_token"])

    def resume_backup(self) -> None:
        newly_started = [name for name in ("bot", "napcat", "database") if name not in self.state["running"] and name in self.state["compose"]["services"]]
        if newly_started:
            self.c.compose(["stop", *newly_started], timeout=90)
        write_json(self.directory / "compose.json", self.state["compose"])
        self.save(stage="resuming")
        if self.state.get("sandbox_enabled"):
            self.c.sandbox.start()
        if self.state["running"]:
            self.c.compose(["up", "--detach", "--wait", "--wait-timeout", "90", "--no-deps", *self.state["running"]], timeout=120)
        shutil.rmtree(self.workspace)
        self.marker.unlink()
        sync_directory(self.marker.parent)

    def backup(self) -> None:
        self.c.persist(status="running", stage="backup_prepare", message="正在暂停本实例写入并检查备份空间")
        self.begin("backup")
        record = self.snapshot()
        self.resume_backup()
        self.c.persist(status="completed", stage="backed_up", message="备份已校验并保存，原运行状态已恢复", backup_id=record["id"])

    def snapshot(self) -> dict:
        """Export while writers remain paused, including during runtime updates."""
        self.c.compose(["up", "--detach", "--wait", "--wait-timeout", "90", "database"], timeout=120)
        raw = self.command(["exec", "-T", "database", "psql", "-U", "gaoji", "-d", "gaoji", "-Atc", "SELECT pg_database_size(current_database())"], timeout=30)
        pg_bytes = int(raw.strip())
        data_bytes = sum(path.stat().st_size for _, path in walk_data(self.directory))
        self.c.capacity.require(2 * pg_bytes + data_bytes)
        if shutil.disk_usage(self.directory).free < 2 * pg_bytes + data_bytes + RESERVE:
            raise BackupError("可用空间不足以同时保留原数据和完整备份，未清理任何数据")
        self.c.persist(stage="backup_database", message="正在导出数据库")
        dump = self.workspace / "database.dump"
        self.command(["exec", "-T", "database", "pg_dump", "-U", "gaoji", "-d", "gaoji", "--format=custom", "--no-owner", "--no-acl"], output=dump)
        config, private = self.c.store.snapshot(self.c.identifier)
        self.c.persist(stage="backup_verify", message="正在打包并逐项校验备份")
        return self.backups.publish(self.c.operation["id"], config, private, dump, self.c.deployment["checksum"], database_bytes=pg_bytes)

    def stage_restore(self, record: dict, *, runtime_image: str | None = None, images: dict | None = None,
                      archive_checksum: str | None = None, migrate: bool = False) -> Path:
        self.c.capacity.require(record["content_bytes"] * 2 + record["database_bytes"] * 3)
        available = shutil.disk_usage(self.directory).free - RESERVE
        # Account for decompressed PG pages, indexes and restore WAL, not just
        # the compressed dump. Never remove original data to make the copy fit.
        if available < record["content_bytes"] * 2 + record["database_bytes"] * 3:
            raise BackupError("恢复暂存空间不足，原数据未被覆盖")
        candidate = self.workspace / "candidate"
        result = verify_archive(self.backups.path(record["id"]) / "snapshot.tar", expected_checksum=archive_checksum or self.c.deployment["checksum"],
            max_bytes=available // 2, destination=candidate)
        snapshot = result["snapshot"]
        source_id = record.get("source_instance_id", self.c.identifier)
        if (snapshot["instance_id"] != source_id or source_id != self.c.identifier and record.get("imported") is not True):
            raise BackupError("此备份来源未经当前实例的导入流程验证")
        current = self.c.store.read(self.c.identifier)
        current["settings"] = snapshot["settings"]
        # Never retain the destination account or manufacture a new HMAC origin.
        current.pop("official_credential_ref", None)
        current.pop("official_recovery_required", None)
        if current["settings"].get("platforms", {}).get("qq_official"):
            current["settings"]["official"]["exclusive_deployment_confirmed"] = False
        current["revision"] += 1
        private = snapshot["private"]
        keys = private["model_key_sets"][str(snapshot["revision"])]
        services = private["component_key_sets"][str(snapshot["revision"])]
        private["model_key_sets"] = {str(current["revision"]): keys}
        private["component_key_sets"] = {str(current["revision"]): services}
        for name in (*DATA, "config", "cache", "napcat/QQ", "napcat/config"):
            (candidate / name).mkdir(mode=0o700, exist_ok=True)
        (candidate / "capacity").mkdir(mode=0o755, exist_ok=True)
        (candidate / "assets").mkdir(mode=0o755, exist_ok=True)
        if snapshot.get("official_bundle") is not None:
            current["official_credential_ref"] = restore_bundle(candidate, snapshot["official_bundle"])
        elif current["settings"].get("platforms", {}).get("qq_official"):
            current["official_recovery_required"] = True
        write_json(candidate / "instance.json", current)
        write_json(candidate / "config/secrets.json", private)
        write_bytes(candidate / "config/admin.secret", base64.b64decode(snapshot["admin_secret"]), bot_owned=True)
        write_bytes(candidate / "config/postgres.password", private["database_password"].encode())
        write_json(candidate / "config/runtime.json", {"schema": 1, "environment": runtime_environment(current, {**private, "model_keys": keys, "component_keys": services}, configured=False)})
        for name in ("state", "files", "cache"):
            own_tree(candidate / name)
        if os.geteuid() == 0:
            os.chown(candidate / "config/runtime.json", 10001, 10001)
        document = self.candidate_document(current, candidate, runtime_image=runtime_image or self.c.deployment["runtime_image"],
            images=images or read_json(self.c.release / "images.lock.json"))
        document["name"] = "gaoji-restore-" + self.c.operation["id"]
        document["services"].pop("napcat", None)
        document["services"]["bot"].pop("ports")
        for service in document["services"].values():
            service["restart"] = "no"
        compose = self.workspace / "candidate-compose.json"
        write_json(compose, document)
        self.command(["up", "--detach", "--wait", "--wait-timeout", "90", "database"], compose=compose, timeout=120)
        self.c.persist(stage="restore_database", message="正在独立数据库中验证备份，原数据未覆盖")
        self.command(["exec", "-T", "database", "pg_restore", "-U", "gaoji", "-d", "gaoji", "--no-owner", "--no-acl", "--single-transaction", "--exit-on-error"],
            compose=compose, source=candidate / "database.dump")
        if migrate:
            self.c.persist(stage="update_migrate", message="正在独立数据库副本中验证新版本迁移，原库尚未修改")
            self.command(["run", "--rm", "--no-deps", "-T", "bot", "migrate"], compose=compose)
        self.command(["run", "--rm", "--no-deps", "-T", "bot", "check-database"], compose=compose, timeout=60)
        self.stop_candidate()
        return candidate

    def candidate_document(self, config: dict, directory: Path, *, runtime_image: str, images: dict) -> dict:
        return compose_document(config, directory, runtime_image=runtime_image, images=images)

    def commit_restore(self, candidate: Path) -> None:
        self.save(stage="committing")
        self.c.persist(stage="restore_switch", message="验证通过，正在切换数据；切换后机器人保持停止")
        source = self.state.get("source_instance_id", self.c.identifier)
        if source != self.c.identifier:
            require_handover(self.directory, source, self.c.operation["id"])
        self.replace_candidate(candidate)
        self.refresh_client()
        self.c.render(False)
        config = self.c.store.read(self.c.identifier)
        write_json(self.c.desired_path, {"bot_enabled": False, "revision": config["revision"]})
        self.c.compose(["up", "--detach", "--wait", "--wait-timeout", "90", "database"], timeout=120)
        self.command(["run", "--rm", "--no-deps", "-T", "bot", "check-database"], timeout=60)
        self.save(stage="committed")
        self.finish_restore()

    def replace_candidate(self, candidate: Path) -> None:
        """Move only journaled instance data; the caller controls the commit point."""
        self.c.compose(["stop", *[name for name in ("bot", "napcat", "database") if name in read_json(self.directory / "compose.json")["services"]]], timeout=90)
        rollback = self.workspace / "rollback"
        rollback.mkdir(mode=0o700)
        for name in DATA:
            os.rename(self.directory / name, rollback / name)
            sync_directory(self.directory)
            sync_directory(rollback)
            os.rename(candidate / name, self.directory / name)
            sync_directory(self.directory)
            sync_directory(candidate)
        candidate_config = read_json(candidate / "instance.json")
        reference = candidate_config.get("official_credential_ref")
        if reference:
            restore_bundle(self.directory, backup_bundle(candidate, reference), reference=reference)
        for name in CONFIG:
            with regular_file(candidate / name) as source:
                write_bytes(self.directory / name, source.read(), bot_owned=name in {"config/admin.secret", "config/runtime.json"})

    def finish_restore(self) -> None:
        write_json(self.workspace / "rollback.json", self.state)
        foreign = self.state.get("source_instance_id", self.c.identifier) != self.c.identifier
        if not foreign and platforms(self.c.store.read(self.c.identifier)["settings"].get("platforms"))["onebot"] and start_allowed(self.directory):
            self.c.compose(["up", "--detach", "--no-deps", "napcat"])
        self.c.persist(status="completed", stage="restored", message="备份已恢复并核对数据库；原数据仍保留。机器人保持停止；请核对账号、原始凭据完整性和旧部署状态后再启用")
        self.marker.unlink()
        sync_directory(self.marker.parent)
        candidate = self.workspace / "candidate"
        if candidate.exists():
            shutil.rmtree(candidate)

    def run(self, *, recovery_only: bool = False) -> None:
        with file_lock(self.directory / ".maintenance.lock", timeout=0.1):
            if self.state and self.state.get("kind") in {"update", "rollback"}:
                raise BackupError("版本切换需要由独立更新任务恢复，不能按普通备份恢复处理")
            try:
                if self.state:
                    if self.state["stage"] == "committed":
                        self.finish_restore()
                        return
                    self.c.persist(stage="recovering", message="正在恢复上次中断前的数据和运行状态")
                    self.original()
                    self.state = None
                if recovery_only:
                    self.c.persist(status="completed", stage="recovered", message="已恢复中断前的数据和运行状态，请重新提交需要的操作")
                    return
                if self.c.operation["kind"] == "backup":
                    self.backup()
                else:
                    self.c.persist(status="running", stage="restore_verify", message="正在校验备份完整性")
                    record = self.backups.check(self.c.operation["backup_id"], self.c.deployment["checksum"])
                    self.begin("restore", metadata={"source_instance_id": record.get("source_instance_id", self.c.identifier)})
                    candidate = self.stage_restore(record)
                    self.commit_restore(candidate)
            except Exception as error:
                if self.state and self.state.get("stage") == "committed":
                    self.c.persist(status="failed", stage="recovery_required", message="数据库恢复已完成，但 QQ 组件或收尾未完成，请点击恢复中断前状态继续收尾")
                    return
                recovered = True
                if self.marker.exists():
                    try:
                        self.state = read_json(self.marker)
                        self.original()
                    except Exception:
                        recovered = False
                message = str(error) if isinstance(error, (BackupError, SetupError)) else "维护未完成，原数据仍保留"
                self.c.persist(status="failed", stage="failed" if recovered else "recovery_required",
                    message=message + ("；已恢复原运行状态" if recovered else "；运行状态尚未恢复，请点击恢复中断前状态"))
