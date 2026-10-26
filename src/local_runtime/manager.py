"""Per-instance loopback controller, authenticated exclusively over desktop SSH.

The API exposes fixed application operations, never host paths, arbitrary shell,
Docker options or service names. Root credentials remain outside the Bot process.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

from .files import file_lock, read_json, write_json
from .instances import ConfigurationConflict, InstanceError, InstanceStore
from .runtime import compose_document, runtime_environment
from .setup_clients import NapCatSetup, SetupError
from .components import ComponentError, requirements
from .backups import BackupError, BackupStore, backup_id
from .backup_transfer import BackupTransfer, MAX_BYTES
from .storage import Storage
from .capacity import Capacity
from src.storage_admission import CapacityError
from .lifecycle_state import check_lifecycle, lifecycle_present
from .maintenance import Maintenance
from .video_assets import VideoAssetError, VideoAssets
from .sandbox_service import SandboxService
from .sandbox_image import SandboxImage, SandboxImageError
from .qq_handover import approve_handover, clear_handover, read_handover, start_allowed
from .readiness import connection_status


ROOT = Path("/var/lib/gaoji-app")
PATH = "/var/lib/gaoji-app/tools/bin:/run/current-system/sw/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class Controller:
    def __init__(self, job: str, root: Path = ROOT):
        if not re.fullmatch(r"[a-f0-9]{32}", job):
            raise ValueError("Invalid deployment ID")
        self.root = root
        self.job = job
        self.deployment = read_json(root / "jobs" / (job + ".json"))
        self.identifier = self.deployment["instance_id"]
        self.store = InstanceStore(root / "data")
        self.directory = self.store.directory(self.identifier)
        self.sandbox_image_path = self.directory / "config/sandbox-image.json"
        if self.sandbox_image_path.exists():
            self.adopt_sandbox_image(read_json(self.sandbox_image_path))
        self.sandbox = SandboxService(self.directory, self.deployment)
        self.release = root / "releases" / self.deployment["checksum"]
        self.control = read_json(self.directory / "config/control.json")
        # During a crash in the restore commit, config and credential revisions
        # can temporarily differ. The maintenance journal repairs them before use.
        config, private = self.store.read(self.identifier), self.store.secrets(self.identifier)
        self.napcat = NapCatSetup(config["ports"]["napcat"], private["napcat_token"])
        self.lock = threading.RLock()
        self.operation_path = self.directory / "config/operation.json"
        self.desired_path = self.directory / "config/desired.json"
        self.operation = read_json(self.operation_path) if self.operation_path.exists() else None
        self.worker: threading.Thread | None = None
        self.capacity = Capacity(self)
        self.capacity.prepare()
        self.storage = Storage(self)

    def adopt_sandbox_image(self, record: dict) -> None:
        if (record.get("schema") != 1 or not re.fullmatch(r"gaoji-app-sandbox:[a-f0-9]{64}", record.get("image", ""))
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", record.get("image_id", ""))):
            raise SetupError("沙盒镜像记录无效，未加载运行配置")
        if record.get("integrity") != "nix-store-v1":
            # Keep the controller reachable so applying configuration can verify
            # an older image. Never activate it based only on a Docker tag/ID.
            self.deployment.pop("sandbox_image", None)
            self.deployment.pop("sandbox_image_id", None)
            return
        self.deployment.update(sandbox_image=record["image"], sandbox_image_id=record["image_id"])

    def sandbox_build(self) -> SandboxImage | None:
        source = self.release / "sandbox"
        if not source.is_dir():
            return None
        return SandboxImage(source, self.root / "sandbox-images", docker=self.deployment["docker"],
            docker_host=self.deployment["docker_host"])

    def sandbox_status(self) -> dict:
        try:
            builder = self.sandbox_build()
            record = read_json(self.sandbox_image_path) if self.sandbox_image_path.exists() else {}
            installed = (builder is not None and record.get("recipe") == builder.recipe_id
                         and record.get("integrity") == "nix-store-v1")
            return {"supported": builder is not None, "state": "recorded" if installed else "not_installed",
                "bytes": record.get("bytes", 0) if installed else 0}
        except (OSError, ValueError, InstanceError):
            return {"supported": False, "state": "invalid", "bytes": 0}

    def process_environment(self) -> dict:
        return {"PATH": PATH, "HOME": "/root", "LANG": "C.UTF-8", "DOCKER_HOST": self.deployment["docker_host"]}

    def compose(self, arguments: list[str], *, stdin: dict | None = None, timeout: int = 180) -> str:
        command = [self.deployment["docker"], "compose", "--file", str(self.directory / "compose.json"), *arguments]
        try:
            # Output can include DSNs and QR URLs. It never goes into journal or
            # the desktop response; public diagnostics contain only fixed text.
            result = subprocess.run(command, input=json.dumps(stdin).encode() if stdin is not None else b"",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                env=self.process_environment(), check=False)
        except subprocess.TimeoutExpired:
            raise SetupError("组件操作超时，状态未确认；请先查看任务结果，不要重复安装") from None
        if result.returncode:
            raise SetupError("运行组件未完成此操作，已保留数据；请查看实例的诊断状态")
        return result.stdout.decode("utf-8", errors="replace")

    def public_operation(self) -> dict | None:
        if not self.operation:
            return None
        return {key: self.operation[key] for key in ("id", "kind", "status", "stage", "message", "created_at", "updated_at", "backup_id") if key in self.operation}

    def status(self) -> dict:
        with self.lock:
            pending = (self.directory / "config/maintenance.json").exists()
            try:
                config = self.store.public_status(self.identifier)
            except InstanceError:
                if not pending:
                    raise
                config = self.store.read(self.identifier)
                for model in config["settings"]["models"]:
                    model["has_key"] = False
            desired = read_json(self.desired_path) if self.desired_path.exists() else {"bot_enabled": False}
            sandbox = self.sandbox_status()
            return {"instance": config, "desired": desired, "operation": self.public_operation(),
                "qq_handover": read_handover(self.directory),
                "video_model": VideoAssets(self.directory).status(),
                "sandbox_runtime": sandbox,
                "cluster_configuration": {"schema": 1},
                "matrix_configuration": {"schema": 1},
                "imessage_configuration": {"schema": 1},
                "capacity_configuration": {"schema": 1},
                "model_key_references": {"schema": 1},
                "maintenance_pending": pending,
                "component_requirements": requirements(config["settings"], {name: True for name in config.get("component_keys_present", [])},
                    sandbox_available=sandbox["supported"])}

    def backups(self) -> dict:
        return {"backups": BackupStore(self.directory).list(), "transfer": {"schema": 1, "max_bytes": MAX_BYTES}}

    def qq_status(self) -> dict:
        if read_handover(self.directory) is not None:
            return {"online": False, "qq": "", "nickname": "", "phase": "handover_required",
                "qrcode": "", "offline": True, "has_error": False}
        return self.napcat.status()

    def qq_refresh(self) -> dict:
        if read_handover(self.directory) is not None:
            raise SetupError("请先确认原服务器 QQ 已停止，再接管此实例")
        return self.napcat.refresh()

    def versions(self) -> dict:
        def version(identifier):
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{64}", identifier):
                return "未知"
            try:
                return str(read_json(self.root / "releases" / identifier / "manifest.json").get("version", "未知"))[:80]
            except (OSError, ValueError):
                return "未知"
        history = self.directory / "updates"
        points = []
        if history.is_dir() and not history.is_symlink():
            for path in sorted(history.glob("*.json"), key=lambda item: item.lstat().st_mtime, reverse=True)[:100]:
                if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                    continue
                try:
                    record = read_json(path)
                    target = record["original_deployment"]["checksum"]
                    if (record.get("committed") is not True or record["target_checksum"] != self.deployment["checksum"]
                            or not re.fullmatch(r"[a-f0-9]{64}", target)):
                        continue
                    points.append({"id": path.stem, "created_at": record.get("activation_after", 0),
                        "target_checksum": target, "version": version(target)})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return {"instance_id": self.identifier, "checksum": self.deployment["checksum"], "version": version(self.deployment["checksum"]),
            "controller_checksum": self.deployment.get("controller_checksum", self.deployment["checksum"]),
            "revision": self.store.read(self.identifier)["revision"], "rollbacks": points,
            "maintenance_pending": (self.directory / "config/maintenance.json").exists(), "operation": self.public_operation()}

    def services(self) -> dict:
        raw = self.compose(["ps", "--all", "--format", "json"], timeout=15).strip()
        values = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
        services = [{"name": row.get("Service"), "state": row.get("State"), "health": row.get("Health", "")}
            for row in values if row.get("Service") in {"bot", "database", "napcat"}]
        return {"services": services, "connection": connection_status(self.directory, self.store.read(self.identifier), services)}

    def mutable(self, *, storage_job_id=None, lifecycle_job_id=None) -> None:
        check_lifecycle(self.directory, operation=lifecycle_job_id)
        if self.storage.busy(except_id=storage_job_id):
            raise SetupError("副本清理正在进行，请完成后再修改实例")
        if (self.directory / "config/update-worker.json").exists():
            raise SetupError("独立更新任务正在接管实例，请等待更新完成")
        if self.operation and self.operation["status"] in {"queued", "running"}:
            raise SetupError("正在应用配置，请等当前操作完成后再修改")
        if (self.directory / "config/maintenance.json").exists():
            raise SetupError("上次维护还未完成，请先恢复中断前状态")

    def save(self, payload: dict) -> dict:
        if set(payload) - {"component_keys", "model_key_sources"} != {"revision", "settings", "model_keys"}:
            raise InstanceError("Invalid configuration request")
        with self.lock:
            self.mutable()
            try:
                self.store.update(self.identifier, payload["settings"], expected_revision=payload["revision"], model_keys=payload["model_keys"], service_keys=payload.get("component_keys", {}), model_key_sources=payload.get("model_key_sources", {}))
            except InstanceError as error:
                if any(label + " 地址已更改" in str(error) for label in ("Embedding", "集群", "Matrix")):
                    raise SetupError(str(error)) from None
                raise
            return self.status()

    def runtime_probe(self, command: str, payload: dict) -> dict:
        if command not in {"probe-model", "probe-embedding", "probe-cluster", "probe-matrix", "probe-imessage"}:
            raise InstanceError("Unsupported runtime probe")
        # Probe in the same Compose network as the Bot, not the host's network.
        # Credentials travel on stdin, never in Docker arguments or environment.
        result = json.loads(self.compose(["run", "--rm", "--no-deps", "-T", "bot", command], stdin=payload, timeout=60))
        if not isinstance(result, dict):
            raise SetupError("接口检测没有返回有效结果")
        if isinstance(result.get("error"), str):
            raise SetupError(result["error"][:500])
        if not isinstance(result.get("data"), dict):
            raise SetupError("接口检测没有返回有效结果")
        return result["data"]

    def embedding(self, payload: dict) -> dict:
        return self.component_probe("embedding", payload)

    def cluster(self, payload: dict) -> dict:
        return self.component_probe("cluster", payload)

    def matrix(self, payload: dict) -> dict:
        return self.component_probe("matrix", payload)

    def imessage(self, payload: dict) -> dict:
        return self.component_probe("imessage", payload)

    def component_probe(self, name: str, payload: dict) -> dict:
        if set(payload) != {"options", "key"}:
            raise InstanceError("Invalid component test request")
        options, key = payload["options"], payload["key"]
        if key is None:
            config, private = self.store.snapshot(self.identifier)
            original = config["settings"]["components"][name]
            fields = ("base_url", "user_id") if name == "matrix" else ("base_url", "bot_handle") if name == "imessage" else ("base_url",)
            if not isinstance(options, dict) or any(options.get(field) != original[field] for field in fields):
                raise SetupError("接口地址已更改，请重新填写密钥，避免发送原密钥到其他服务")
            key = private["component_keys"].get(name, "")
        return self.runtime_probe("probe-" + name, {"options": options, "key": key})

    def model(self, payload: dict, *, listing: bool) -> dict:
        if set(payload) != {"model", "key"}:
            raise InstanceError("Invalid model test request")
        profile, key = payload["model"], payload["key"]
        if key is None:
            config, private = self.store.snapshot(self.identifier)
            original = next((item for item in config["settings"]["models"] if isinstance(profile, dict) and item["id"] == profile.get("id")), None)
            if not original or any(original[field] != profile.get(field) for field in ("base_url", "protocol")):
                raise SetupError("接口地址已更换，请重新填写密钥，避免把原密钥发给其他服务")
            key = private["model_keys"].get(profile.get("id"), "") if isinstance(profile, dict) else ""
        return self.runtime_probe("probe-model", {"model": profile, "key": key, "listing": listing})

    def submit(self, payload: dict) -> dict:
        if set(payload) - {"id", "kind", "revision", "administrator", "backup_id", "confirm"} or not {"id", "kind", "revision"} <= set(payload):
            raise InstanceError("Invalid setup operation")
        identifier, kind = payload["id"], payload["kind"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier) or kind not in {"activate", "stop", "backup", "restore", "recover", "resume_qq"}:
            raise InstanceError("Invalid setup operation")
        if kind == "restore":
            backup_id(payload.get("backup_id"))
            if payload.get("confirm") is not True:
                raise SetupError("请先确认恢复会替换本实例当前数据，恢复后机器人保持停止")
        elif kind == "resume_qq":
            if "backup_id" in payload or payload.get("confirm") is not True:
                raise SetupError("请确认原服务器 QQ 已停止，再启动这里的 QQ")
        elif "backup_id" in payload or "confirm" in payload:
            raise InstanceError("Unexpected restore fields")
        if kind != "activate" and "administrator" in payload:
            raise InstanceError("Unexpected administrator fields")
        with self.lock:
            pending = self.directory / "config/maintenance.json"
            if kind == "recover" and pending.exists() and read_json(pending).get("kind") in {"update", "rollback"}:
                raise SetupError("这是版本更新任务，请继续原更新任务，不要使用普通备份恢复")
            if self.operation and self.operation["id"] == identifier:
                if self.operation["kind"] != kind or self.operation.get("backup_id") != payload.get("backup_id"):
                    raise SetupError("操作编号已用于其他动作")
                return self.public_operation()
            if kind != "recover":
                self.mutable()
            elif self.worker and self.worker.is_alive():
                raise SetupError("正在恢复，请等当前操作完成")
            config = self.store.read(self.identifier)
            if kind == "resume_qq" and read_handover(self.directory) is None:
                raise SetupError("没有等待接管的 QQ，请刷新当前状态")
            if type(payload["revision"]) is not int or kind != "recover" and config["revision"] != payload["revision"]:
                raise ConfigurationConflict("配置已变更，请刷新后重试")
            admin = payload.get("administrator")
            if kind == "activate":
                if read_handover(self.directory) is not None:
                    raise SetupError("请先在 QQ 登录页完成接管，再启用机器人")
                _, private = self.store.snapshot(self.identifier)
                runtime_environment(config, private,
                    sandbox=self.sandbox.prepare() if config["settings"]["features"]["sandbox"] else None,
                    sandbox_available=self.sandbox_status()["supported"])
                if admin is not None:
                    if not isinstance(admin, dict) or set(admin) != {"username", "password"}:
                        raise InstanceError("Invalid administrator fields")
                    if not isinstance(admin["username"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}", admin["username"]):
                        raise SetupError("管理员账户名需为 3～32 位字母或数字，可含点、下划线和短横线")
                    if not isinstance(admin["password"], str) or not 12 <= len(admin["password"]) <= 128:
                        raise SetupError("控制台密码请设置为 12～128 个字符")
                if not re.fullmatch(r"[1-9][0-9]{4,14}", config["settings"]["admin_qq"]):
                    raise SetupError("请填写有效的管理员 QQ 号")
            now = int(time.time())
            self.operation = {"id": identifier, "kind": kind, "revision": config["revision"], "administrator": admin,
                "status": "queued", "stage": "prepare", "message": "配置已提交", "created_at": now, "updated_at": now}
            if kind == "restore":
                self.operation["backup_id"] = payload["backup_id"]
            self.persist()
            self.resume()
            return self.public_operation()

    def persist(self, **changes) -> None:
        self.operation.update(changes, updated_at=int(time.time()))
        write_json(self.operation_path, self.operation)

    def resume(self) -> None:
        with self.lock:
            if (self.directory / "config/update-worker.json").exists():
                return
            if self.worker and self.worker.is_alive() or not self.operation:
                return
            if self.operation["kind"] in {"update", "rollback"}:
                # A separate persistent worker owns runtime replacement. Never
                # reinterpret its receipt as an activation or ordinary restore.
                return
            if self.operation["status"] not in {"queued", "running"} and not (self.directory / "config/maintenance.json").exists():
                return
            self.worker = threading.Thread(target=self.execute, name="instance-operation", daemon=True)
            self.worker.start()

    def render(self, enabled: bool) -> None:
        self.capacity.prepare()
        VideoAssets(self.directory).prepare()
        config, private = self.store.snapshot(self.identifier)
        sandbox = self.sandbox.prepare() if config["settings"]["features"]["sandbox"] else None
        runtime = self.directory / "config/runtime.json"
        write_json(runtime, {"schema": 1, "environment": runtime_environment(config, private, configured=enabled, sandbox=sandbox)})
        if os.geteuid() == 0:
            os.chown(runtime, 10001, 10001)
        write_json(self.directory / "compose.json", self.compose_spec(config, enabled=enabled, sandbox=sandbox))

    def compose_spec(self, config: dict, *, enabled: bool, sandbox: dict | None) -> dict:
        return compose_document(config, self.directory,
            runtime_image=self.deployment["runtime_image"], images=read_json(self.release / "images.lock.json"),
            bot_enabled=enabled, sandbox_enabled=sandbox is not None, qq_enabled=start_allowed(self.directory))

    def maintenance(self) -> Maintenance:
        return Maintenance(self)

    def execute(self) -> None:
        try:
            if self.operation["kind"] in {"backup", "restore", "recover"}:
                self.maintenance().run(recovery_only=self.operation["kind"] == "recover" or self.operation["status"] == "failed")
                return
            config = self.store.read(self.identifier)
            if config["revision"] != self.operation["revision"]:
                raise SetupError("配置版本已变化，尚未启动机器人，请重新确认")
            if (self.operation["kind"] == "resume_qq" and self.operation.get("stage") == "qq_ready"
                    and read_handover(self.directory) is None):
                self.persist(status="completed", message="此实例 QQ 登录入口已就绪；请核对账号后另行启用机器人")
                return
            self.persist(status="running", stage="prepare", message="正在准备运行配置")
            if self.operation["kind"] == "resume_qq":
                self.persist(stage="qq_handover", message="已确认接管，正在启动此实例的 QQ；机器人仍保持停止")
                approve_handover(self.directory)
                self.render(False)
                write_json(self.desired_path, {"bot_enabled": False, "revision": config["revision"]})
                self.compose(["up", "--detach", "--wait", "--wait-timeout", "120", "database", "napcat"], timeout=150)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        qq = self.napcat.status()
                    except SetupError:
                        qq = {}
                    if qq.get("online") or qq.get("qrcode"):
                        break
                    if time.monotonic() >= deadline:
                        raise SetupError("QQ 组件已请求启动，但登录入口尚未确认，请重试接管")
                    time.sleep(2)
                # Record verification before removing the gate so a crash between
                # the two writes can finish the same operation without relaunching.
                self.persist(stage="qq_ready", message="已确认 QQ 登录入口，正在完成接管")
                clear_handover(self.directory)
                self.persist(status="completed", stage="qq_ready", message="此实例 QQ 登录入口已就绪；请核对账号后另行启用机器人")
                return
            if self.operation["kind"] == "stop":
                self.render(False)
                write_json(self.desired_path, {"bot_enabled": False, "revision": config["revision"]})
                self.compose(["stop", "bot"], timeout=90)
                self.sandbox.pause()
                self.persist(status="completed", stage="stopped", message="机器人已停止，数据库和 QQ 登录仍保留", administrator=None)
                return
            if read_handover(self.directory) is not None:
                raise SetupError("请先在 QQ 登录页完成接管，再启用机器人")
            self.compose(["up", "--detach", "--wait", "--wait-timeout", "120", "database", "napcat"], timeout=150)
            qq = self.napcat.status()
            if not qq["online"] or qq["qq"] != config["settings"]["bot_qq"]:
                raise SetupError("请先扫码登录当前配置的 QQ，再启用机器人")
            self.persist(stage="model", message="正在从服务器验证默认模型")
            _, private = self.store.snapshot(self.identifier)
            profile = next(model for model in config["settings"]["models"] if model["id"] == config["settings"]["default_model"])
            self.runtime_probe("probe-model", {"model": profile, "key": private["model_keys"].get(profile["id"], ""), "listing": False})
            if config["settings"]["features"]["semantic"]:
                self.persist(stage="embedding", message="正在验证 Embedding 服务和向量维数")
                self.runtime_probe("probe-embedding", {"options": config["settings"]["components"]["embedding"], "key": private["component_keys"].get("embedding", "")})
            if config["settings"]["features"]["cluster"]:
                self.persist(stage="cluster", message="正在验证集群接口与访问凭据")
                self.runtime_probe("probe-cluster", {"options": config["settings"]["components"]["cluster"], "key": private["component_keys"].get("cluster", "")})
            if config["settings"]["components"]["matrix"]["enabled"]:
                self.persist(stage="matrix", message="正在验证 Matrix 账户、房间和发言权限")
                self.runtime_probe("probe-matrix", {"options": config["settings"]["components"]["matrix"], "key": private["component_keys"].get("matrix", "")})
            if config["settings"]["components"]["imessage"]["enabled"]:
                self.persist(stage="imessage", message="正在验证 BlueBubbles 与指定 iMessage 会话")
                self.runtime_probe("probe-imessage", {"options": config["settings"]["components"]["imessage"], "key": private["component_keys"].get("imessage", "")})
            self.render(False)
            if config["settings"]["features"]["video"]:
                self.persist(stage="video", message="正在准备视频转写模型")
                VideoAssets(self.directory).ensure(lambda received, total: self.persist(
                    message=f"正在准备视频转写模型：{received * 100 // total}%"), admit=self.capacity.require)
                self.persist(stage="video", message="正在验证视频工具和转写模型")
                self.compose(["run", "--rm", "--no-deps", "-T", "bot", "check-video"], timeout=180)
            self.persist(stage="administrator", message="正在核对控制台管理员")
            administrator = self.operation.get("administrator")
            request = {**administrator, "qq_id": config["settings"]["admin_qq"]} if administrator else None
            if request:
                self.compose(["run", "--rm", "--no-deps", "-T", "bot", "ensure-admin"], stdin=request)
            else:
                self.compose(["run", "--rm", "--no-deps", "-T", "bot", "check-admin"], stdin={"qq_id": config["settings"]["admin_qq"]})
            self.persist(stage="sandbox", message="正在核对沙盒运行状态")
            if config["settings"]["features"]["sandbox"]:
                builder = self.sandbox_build()
                if builder is not None:
                    self.persist(stage="sandbox_image", message="正在准备隔离沙盒镜像")
                    record = builder.ensure(lambda message: self.persist(message=message), admit=self.capacity.require)
                    write_json(self.sandbox_image_path, record)
                    self.adopt_sandbox_image(record)
                    self.render(False)
                self.persist(stage="sandbox", message="正在接通隔离沙盒")
                self.sandbox.start()
                try:
                    result = json.loads(self.compose(["run", "--rm", "--no-deps", "-T", "bot", "check-sandbox"], timeout=30))
                    if result.get("channel_ready") is not True or result.get("instance_id") != self.identifier:
                        raise SetupError("聊天容器尚未接通本实例沙盒，未切换机器人")
                except Exception:
                    self.sandbox.pause()
                    raise
            else:
                self.sandbox.pause()
            self.persist(stage="connection", message="正在接通 QQ 与机器人")
            self.napcat.configure(config["settings"]["bot_qq"], private["onebot_token"])
            self.render(True)
            write_json(self.desired_path, {"bot_enabled": True, "revision": config["revision"]})
            self.compose(["up", "--detach", "--no-deps", "--force-recreate", "bot"])
            self.persist(stage="verify", message="正在核对机器人启动与 QQ 连接")
            # The runtime writes only safe booleans to its own state directory.
            ready_path = self.directory / "state/desktop-health.json"
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if ready_path.exists():
                    health = read_json(ready_path)
                    if health.get("updated_at", 0) >= self.operation["updated_at"] and health.get("qq") == config["settings"]["bot_qq"] and health.get("connected"):
                        self.persist(status="completed", stage="online", message="机器人已接通，可在已选择的群里 @它", administrator=None)
                        return
                time.sleep(2)
            raise SetupError("服务已启动，但尚未确认 QQ 消息连接；请查看 QQ 状态后重试，不必重装")
        except Exception as error:
            message = str(error) if isinstance(error, (SetupError, ComponentError, VideoAssetError, SandboxImageError, ConfigurationConflict, CapacityError)) else "设置未完成，数据已保留，请核对配置后重试"
            self.persist(status="failed", message=message, administrator=None)


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, controller: Controller, *, bind_host: str = "127.0.0.1"):
        self.controller = controller
        self.slots = threading.BoundedSemaphore(8)
        super().__init__((bind_host, controller.control["port"]), ManagerHandler)

    def process_request(self, request, address) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address) -> None:
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class ManagerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args) -> None:
        pass

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)
        self.streaming = False

    def answer(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        controller = self.server.controller
        expected = "Bearer " + controller.control["token"]
        credential = self.headers.get("Authorization", "")
        if self.headers.get("Origin") or not secrets.compare_digest(credential.encode(), expected.encode()):
            self.answer(403, {"error": "需要通过已登录的桌面客户端操作"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path == "/backup/import":
                if (not 1024 <= length <= MAX_BYTES or self.headers.get("Transfer-Encoding")
                        or self.headers.get_content_type() != "application/octet-stream"):
                    raise BackupError("备份上传长度或类型无效")
                encoded = self.headers.get("X-Gaoji-Backup", "")
                if len(encoded) > 8192:
                    raise BackupError("备份传输头过大")
                info = json.loads(base64.b64decode(encoded, validate=True))
                if not isinstance(info, dict) or info.get("bytes") != length:
                    raise BackupError("备份上传长度不一致")
                revision = int(self.headers.get("X-Gaoji-Revision", "0"))
                self.connection.settimeout(60)
                result = BackupTransfer(controller).import_archive(self.rfile, info, revision)
                self.answer(200, {"data": result})
                return
            if not 0 < length <= 256 * 1024 or self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise InstanceError("Invalid request body")
            data = self.rfile.read(length)
            if len(data) != length:
                raise InstanceError("Incomplete request")
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise InstanceError("Invalid request body")
            if self.path == "/backup/import/status":
                if set(payload) != {"metadata"}:
                    raise BackupError("备份回执查询无效")
                self.answer(200, {"data": BackupTransfer(controller).receipt(payload["metadata"])})
                return
            if self.path == "/backup/export":
                if set(payload) != {"id"}:
                    raise BackupError("备份下载请求无效")
                with BackupTransfer(controller).export(payload["id"]) as (info, stream):
                    self.connection.settimeout(60)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(info["bytes"]))
                    self.send_header("X-Gaoji-Backup", base64.b64encode(json.dumps(info).encode()).decode())
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.streaming = True
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        self.wfile.write(block)
                return
            endpoints = {"/status": controller.status, "/services": controller.services, "/backups": controller.backups, "/versions": controller.versions,
                "/storage/status": controller.storage.status, "/storage/scan": controller.storage.scan, "/storage/plan": controller.storage.plan,
                "/qq/status": controller.qq_status, "/qq/refresh": controller.qq_refresh,
                "/console": lambda: {"port": controller.store.read(controller.identifier)["ports"]["bot"]}}
            if self.path in endpoints:
                if payload:
                    raise InstanceError("Unexpected request fields")
                result = endpoints[self.path]()
            elif self.path == "/settings":
                result = controller.save(payload)
            elif self.path in {"/model/test", "/model/list"}:
                result = controller.model(payload, listing=self.path == "/model/list")
            elif self.path == "/embedding/test":
                result = controller.embedding(payload)
            elif self.path == "/cluster/test":
                result = controller.cluster(payload)
            elif self.path == "/matrix/test":
                result = controller.matrix(payload)
            elif self.path == "/imessage/test":
                result = controller.imessage(payload)
            elif self.path == "/operation":
                result = controller.submit(payload)
            elif self.path == "/storage/policy":
                result = controller.storage.save_policy(payload)
            elif self.path == "/storage/cleanup":
                result = controller.storage.submit(payload)
            else:
                self.answer(404, {"error": "不支持的操作"})
                return
            self.answer(200, {"data": result})
        except ConfigurationConflict:
            self.answer(409, {"error": "配置已被更新，请刷新后重试"})
        except (SetupError, ComponentError, BackupError, CapacityError) as error:
            self.answer(400, {"error": str(error)})
        except (ValueError, InstanceError):
            self.answer(400, {"error": "填写内容不完整或格式不正确，请检查后重试"})
        except (OSError, subprocess.SubprocessError):
            if self.streaming:
                self.close_connection = True
                return
            self.answer(503, {"error": "实例组件暂时不可用，请稍后重试"})
        except Exception:
            if self.streaming:
                self.close_connection = True
                return
            self.answer(500, {"error": "操作未完成，现有数据已保留"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    controller = Controller(args.job)
    if lifecycle_present(controller.directory):
        return
    with file_lock(controller.directory / ".manager.lock", timeout=0.1):
        server = ManagerServer(controller)
        controller.resume()
        controller.storage.start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            controller.storage.shutdown.set()


if __name__ == "__main__":
    main()
