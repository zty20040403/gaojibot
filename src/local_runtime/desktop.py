"""Docker Desktop backend; no SSH, systemd, host paths or production config."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

from .capacity import Capacity, allocated_bytes
from .files import file_lock, read_json, write_json
from .instances import InstanceStore
from .manager import Controller, ManagerServer, ROOT
from .maintenance import Maintenance, write_bytes
from .backups import regular_file
from .qq_handover import start_allowed
from .runtime import compose_document
from .setup_clients import SetupError

SOURCE = Path("/opt/gaoji")
SEED = ROOT / "desktop.json"
INSTALL = ROOT / "desktop-install.json"


def desktop_compose(config, directory, deployment, images, *, enabled=False, sandbox_enabled=False, qq_enabled=True, root=ROOT):
    document = compose_document(config, directory, runtime_image=deployment["runtime_image"],
                                images=images, bot_enabled=enabled, sandbox_enabled=sandbox_enabled, qq_enabled=qq_enabled)
    volume = deployment["volume"]
    if not re.fullmatch(r"gaoji-desktop-[a-f0-9]{32}-data", volume):
        raise ValueError("Invalid desktop volume")
    prefix = directory.relative_to(root).as_posix()
    def mount(child, target, readonly=False):
        return {"type": "volume", "source": "data", "target": target, "read_only": readonly,
                "volume": {"nocopy": True, "subpath": prefix + "/" + child}}
    # Only the Bot's runtime secrets are mounted, never controller/model catalog credentials.
    for name, service in document["services"].items():
        mounts = []
        for item in service["volumes"]:
            relative = Path(item["source"].replace("$$", "$")).relative_to(directory).as_posix()
            if relative.startswith("config/"):
                continue
            mounts.append(mount(relative, item["target"], item["read_only"]))
        if name == "bot":
            mounts.append(mount("runtime-config", "/run/gaoji", True))
        elif name == "database":
            mounts.append(mount("database-config", "/run/secrets", True))
        service["volumes"] = mounts
        service["labels"] = {"gaoji.desktop.job": deployment["job"]}
        service["networks"] = ["instance"]
        service["extra_hosts"] = ["host.docker.internal:host-gateway"]
    # NapCat is available to the controller by private DNS, not a host-loopback detour.
    document["services"]["napcat"].pop("ports")
    document["volumes"] = {"data": {"external": True, "name": volume}}
    document["networks"] = {"instance": {"external": True, "name": deployment["network"]}}
    return document


def sync_config_mounts(directory):
    for child in ("runtime-config", "database-config"):
        folder = directory / child
        if folder.is_symlink():
            raise SetupError("本机配置挂载路径异常，未启动组件")
        folder.mkdir(mode=0o700, exist_ok=True)
        if child == "runtime-config" and os.geteuid() == 0:
            os.chown(folder, 10001, 10001)
    for source, target, bot_owned in (
        ("config/runtime.json", "runtime-config/runtime.json", True),
        ("config/admin.secret", "runtime-config/admin.secret", True),
        ("config/postgres.password", "database-config/postgres-password", False),
    ):
        with regular_file(directory / source) as stream:
            write_bytes(directory / target, stream.read(), bot_owned=bot_owned)


class DesktopMaintenance(Maintenance):
    def refresh_client(self):
        super().refresh_client()
        self.c.napcat.base = "http://napcat:6099"
        sync_config_mounts(self.directory)

    def candidate_document(self, config, directory, *, runtime_image, images):
        sync_config_mounts(directory)
        document = desktop_compose(config, directory, {**self.c.deployment, "runtime_image": runtime_image},
            images, qq_enabled=False, root=self.c.root)
        # Restore validation must not share the live database's DNS/network.
        document["networks"] = {"instance": {"internal": True}}
        return document


class DesktopCapacity(Capacity):
    def data_usage(self, *, deadline, seen):
        # Includes this local instance's optional image build cache and recovery
        # receipts, which live beside data/ inside its one owned Desktop volume.
        return allocated_bytes(self.c.root, deadline=deadline, seen=seen)

    def volume_usage(self, volumes, *, deadline, seen):
        return sum(volumes)

    def engine_inventory(self):
        from .desktop_sandbox import sandbox_volume_sizes
        identifiers = set()
        for label in ("com.docker.compose.project=gaoji-" + self.c.identifier,
                      "gaoji.app.instance=" + self.c.identifier, "gaoji.desktop.job=" + self.c.job):
            identifiers.update(self.command("ps", "-aq", "--no-trunc", "--filter", "label=" + label).splitlines())
        if len(identifiers) > 128 or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in identifiers):
            raise ValueError("Unexpected Desktop container inventory")
        writable = 0
        if identifiers:
            rows = json.loads(self.command("container", "inspect", "--size", *sorted(identifiers)))
            if {row["Id"] for row in rows} != identifiers:
                raise ValueError("Incomplete Desktop container inventory")
            for row in rows:
                labels = row["Config"]["Labels"]
                if (not (labels.get("com.docker.compose.project") == "gaoji-" + self.c.identifier
                    or labels.get("gaoji.app.instance") == self.c.identifier or labels.get("gaoji.desktop.job") == self.c.job)
                        or type(row.get("SizeRw")) is not int or row["SizeRw"] < 0):
                    raise ValueError("Unexpected container")
                writable += row["SizeRw"]
        names = self.command("volume", "ls", "--filter", "label=gaoji.app.instance=" + self.c.identifier, "--format", "{{.Name}}").splitlines()
        return self.directory, sandbox_volume_sizes(self.c.identifier, names) if names else [], writable


class DesktopController(Controller):
    def __init__(self, job, root=ROOT, source=SOURCE):
        super().__init__(job, root)
        self.source = source
        if not self.release.is_dir():
            self.release = source
        self.install_path = root / "desktop-install.json"
        self.napcat.base = "http://napcat:6099"
        self.capacity = DesktopCapacity(self)
        from .desktop_sandbox import DesktopSandboxService
        self.sandbox = DesktopSandboxService(self.directory, self.deployment)

    def sandbox_build(self):
        from .desktop_sandbox import DesktopSandboxImage
        source = self.source / "sandbox"
        if not source.is_dir():
            return None
        return DesktopSandboxImage(source, self.root / "sandbox-images", deployment=self.deployment, root=self.root)

    def process_environment(self):
        return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": "/root", "LANG": "C.UTF-8", "DOCKER_HOST": "unix:///var/run/docker.sock"}

    def status(self):
        return {**super().status(), "desktop_installation": read_json(self.install_path)}

    def versions(self):
        value = super().versions()
        if value["version"] == "未知" and self.release == self.source:
            value["version"] = read_json(self.source / "manifest.json")["version"]
        return value

    def maintenance(self):
        return DesktopMaintenance(self)

    def compose_spec(self, config, *, enabled, sandbox):
        sync_config_mounts(self.directory)
        return desktop_compose(config, self.directory,
            self.deployment, read_json(self.release / "images.lock.json"), enabled=enabled,
            sandbox_enabled=sandbox is not None, qq_enabled=start_allowed(self.directory), root=self.root)

    def submit(self, payload):
        if read_json(self.install_path)["status"] != "awaiting_setup":
            raise SetupError("本机安装尚未完成，请先继续安装")
        return super().submit(payload)

    def install_compose(self, arguments, *, timeout):
        from .image_install import run_observed
        detail = {}

        class DiscardLog:
            def write(self, _block):
                pass

        def report(value):
            detail.update(value)
            state = read_json(self.install_path)
            state.update(progress={k: value[k] for k in ("detail", "elapsed_seconds", "quiet_seconds")},
                         updated_at=int(time.time()))
            write_json(self.install_path, state)

        command = [self.deployment["docker"], "compose", "--file", str(self.directory / "compose.json"), *arguments]
        try:
            result = run_observed(command, timeout=timeout, environment=self.process_environment(),
                                  log=DiscardLog(), report=report)
        except subprocess.TimeoutExpired:
            raise SetupError("组件操作超时；继续时会先检查原组件，不创建另一份数据库") from None
        if result:
            raise SetupError((detail.get("failure") or "运行组件没有完成此步骤") + "；已有数据保留")

    def initialize(self):
        def progress(stage, message, status="running"):
            order = ["runtime", "verify", "initialize", "database", "migrate", "qq", "ready"]
            previous = read_json(self.install_path)
            write_json(self.install_path, {"job": self.job, "stage": stage, "status": status,
                "message": message, "completed": order[:order.index(stage)],
                "stage_started_at": previous.get("stage_started_at", int(time.time())) if previous.get("stage") == stage else int(time.time()),
                "updated_at": int(time.time())})
        try:
            with file_lock(self.root / ".initialize.lock", timeout=0.1):
                if (self.directory / "config/update-worker.json").exists():
                    return
                if read_json(self.install_path)["status"] == "awaiting_setup":
                    if (self.directory / "config/maintenance.json").exists():
                        self.resume()
                        return
                    desired = read_json(self.desired_path) if self.desired_path.exists() else {}
                    self.render(desired.get("bot_enabled") is True)
                    services = ["database", *(["napcat"] if start_allowed(self.directory) else [])]
                    self.compose(["up", "-d", *services], timeout=120)
                    self.resume()
                    return
                progress("database", "正在下载数据库与 QQ 组件，首次安装需要联网")
                self.render(False)
                self.install_compose(["pull", "database", "napcat"], timeout=1800)
                self.install_compose(["up", "-d", "--wait", "--wait-timeout", "180", "database"], timeout=210)
                progress("migrate", "正在初始化独立数据库")
                self.install_compose(["run", "--rm", "--no-deps", "-T", "bot", "migrate"], timeout=600)
                progress("qq", "正在启动本机 QQ 登录入口")
                self.install_compose(["up", "-d", "napcat"], timeout=120)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        qq = self.napcat.status()
                        if qq.get("online") or qq.get("qrcode"):
                            break
                    except SetupError:
                        pass
                    if time.monotonic() >= deadline:
                        raise SetupError("QQ 登录入口尚未就绪")
                    time.sleep(2)
                progress("ready", "本机环境已就绪，请扫码并配置自己的模型", "awaiting_setup")
        except Exception as error:
            stage = read_json(self.install_path)["stage"]
            labels = {"database": "数据库与 QQ 组件", "migrate": "数据库初始化", "qq": "QQ 登录入口"}
            reason = str(error) if isinstance(error, SetupError) else "请检查 Docker Desktop 的网络和磁盘；已有数据保留"
            progress(stage, f"{labels.get(stage, '本机安装')}未完成：{reason}", "failed")


def initialize_seed(payload):
    job, checksum = payload.get("job", ""), payload.get("checksum", "")
    if (set(payload) != {"job", "checksum", "token", "bot_port"}
            or not re.fullmatch(r"[a-f0-9]{32}", job) or not re.fullmatch(r"[a-f0-9]{64}", checksum)
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", payload.get("token", ""))
            or type(payload["bot_port"]) is not int or not 1024 <= payload["bot_port"] <= 65535):
        raise ValueError("Invalid local installation")
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "jobs").mkdir(mode=0o700, exist_ok=True)
    with file_lock(ROOT / ".seed.lock"):
        if SEED.exists():
            if read_json(SEED) != payload:
                raise ValueError("Another installation owns this volume")
            return
        store = InstanceStore(ROOT / "data")
        # Resume a crash before the seed commit instead of creating another database.
        records = store.list()
        if len(records) > 1:
            raise ValueError("Unexpected local instances")
        config = records[0] if records else store.create("本机高级")
        config["ports"] = {"bot": payload["bot_port"], "napcat": 6099,
                           "database": 5432}
        directory = store.directory(config["id"])
        write_json(directory / "instance.json", config)
        _, private = store.snapshot(config["id"])
        for child in ("napcat/QQ", "napcat/config", "runtime-config", "database-config"):
            (directory / child).mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in ("state", "cache", "files", "runtime-config"):
            os.chown(directory / child, 10001, 10001)
        for filename, content in (("config/admin.secret", secrets.token_bytes(32)),
                                  ("config/postgres.password", private["database_password"].encode()),
                                  ("database-config/postgres-password", private["database_password"].encode())):
            path = directory / filename
            if not path.exists():
                with path.open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(content)
        secret = directory / "config/admin.secret"
        os.chown(secret, 10001, 10001)
        runtime_secret = directory / "runtime-config/admin.secret"
        if not runtime_secret.exists():
            runtime_secret.write_bytes(secret.read_bytes())
            runtime_secret.chmod(0o600)
            os.chown(runtime_secret, 10001, 10001)
        write_json(directory / "napcat/config/webui.json", {"host": "0.0.0.0", "port": 6099,
            "prefix": "", "token": private["napcat_token"], "loginRate": 3})
        write_json(directory / "config/control.json", {"port": 8090, "token": payload["token"]})
        deployment = {"instance_id": config["id"], "job": job, "checksum": checksum,
            "runtime_image": "gaoji-app-runtime:" + checksum, "docker": "/usr/local/bin/docker",
            "docker_host": "unix:///var/run/docker.sock", "volume": f"gaoji-desktop-{job}-data",
            "network": f"gaoji-desktop-{job}"}
        write_json(ROOT / "jobs" / (job + ".json"), deployment)
        write_json(INSTALL, {"job": job, "status": "queued", "stage": "database",
                             "message": "本机组件准备中", "completed": []})
        write_json(SEED, payload)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "sandbox":
        import asyncio
        from .sandbox_service import maintenance_allows_start
        from src.sandbox_runtime.server import load_config, serve
        identifier = sys.argv[2]
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise SetupError("沙盒实例编号无效")
        directory = InstanceStore(ROOT / "data").directory(identifier)
        if not (directory / "config/sandbox.enabled").exists() or not maintenance_allows_start(directory):
            return
        asyncio.run(serve(load_config(directory / "config/sandbox-runner.json")))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "update":
        from .desktop_updates import DesktopUpdateWorker
        worker = DesktopUpdateWorker(sys.argv[2])
        worker.run()
        worker.save()
        # Keep one lightweight receipt container; Docker restart resumes the same
        # journal. The next successful update removes this finished worker only.
        threading.Event().wait()
        return
    if sys.argv[1:] == ["prepare-update"]:
        from .desktop_updates import prepare_update
        prepare_update(json.loads(sys.stdin.read(8192)))
        return
    if sys.argv[1:] == ["init"]:
        initialize_seed(json.loads(sys.stdin.read(8192)))
        return
    if sys.argv[1:] != ["serve"]:
        raise ValueError("Invalid desktop operation")
    seed = read_json(SEED)
    controller = DesktopController(seed["job"])
    with file_lock(controller.directory / ".manager.lock", timeout=0.1):
        server = ManagerServer(controller, bind_host="0.0.0.0")
        threading.Thread(target=controller.initialize, daemon=True).start()
        controller.storage.start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            controller.storage.shutdown.set()


if __name__ == "__main__":
    main()
