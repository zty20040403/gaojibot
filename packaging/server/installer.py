"""Root-owned, resumable installation of one isolated Gaoji application instance.

This file is uploaded and hash-verified by the desktop before execution. It accepts
only a fixed job ID and a verified application archive, never shell commands.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen


ROOT = Path("/var/lib/gaoji-app")
APP_OWNER = "Gaoji desktop managed unit"
STAGES = ("prepare", "engine", "runtime", "database", "service", "verify")


class InstallError(RuntimeError):
    pass


def atomic(path: Path, value: dict, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise InstallError("Refusing a symlinked application file")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_directory(path: Path, mode: int = 0o700) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise InstallError("Application directory is not owned by the installer")
    os.chmod(path, mode)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def extract_bundle(archive: Path, destination: Path, expected: str) -> dict:
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > 512 * 1024**2 or digest(archive) != expected:
        raise InstallError("Application archive checksum does not match")
    if destination.exists():
        marker = destination / ".bundle.json"
        if marker.is_file() and json.loads(marker.read_text()).get("sha256") == expected:
            return json.loads((destination / "manifest.json").read_text())
        raise InstallError("Application release directory already exists and is not verified")
    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=destination.parent))
    try:
        total = 0
        files: set[str] = set()
        with tarfile.open(archive, "r:gz") as source:
            for index, member in enumerate(source):
                name = PurePosixPath(member.name)
                if index > 10000 or name.is_absolute() or ".." in name.parts or "\\" in member.name:
                    raise InstallError("Invalid application archive path")
                if not member.isdir() and not member.isfile():
                    raise InstallError("Links and devices are not permitted in the application archive")
                target = temporary.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                canonical = name.as_posix()
                if canonical in files or member.size > 25 * 1024**2:
                    raise InstallError("Duplicate or oversized application file")
                total += member.size
                if total > 512 * 1024**2:
                    raise InstallError("Application archive is too large")
                files.add(canonical)
                target.parent.mkdir(parents=True, exist_ok=True)
                input_stream = source.extractfile(member)
                if input_stream is None:
                    raise InstallError("Unreadable application archive member")
                with input_stream, target.open("xb") as output:
                    shutil.copyfileobj(input_stream, output)
        manifest = json.loads((temporary / "manifest.json").read_text())
        if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list):
            raise InstallError("Unsupported application manifest")
        expected_files = {"manifest.json"}
        for item in manifest["files"]:
            name = item.get("path", "")
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or not name or name in expected_files:
                raise InstallError("Invalid application manifest path")
            expected_files.add(name)
            target = temporary / name
            if not target.is_file() or target.stat().st_size != item.get("bytes") or digest(target) != item.get("sha256"):
                raise InstallError("Application file verification failed")
        if files != expected_files:
            raise InstallError("Unlisted files were found in the application archive")
        atomic(temporary / ".bundle.json", {"sha256": expected})
        os.rename(temporary, destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def read_os() -> dict[str, str]:
    values = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    return values


def owned_unit(name: str, body: str) -> None:
    from src.local_runtime.services import write_unit
    write_unit(name, body)


class Installer:
    def __init__(self, job: str, archive: Path, checksum: str, previous_checksum: str | None = None):
        if not re.fullmatch(r"[a-f0-9]{32}", job) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise InstallError("Invalid installation identifier")
        self.job = job
        self.archive = archive
        self.checksum = checksum
        private_directory(ROOT, 0o755)
        for directory in ("jobs", "releases", "data"):
            private_directory(ROOT / directory)
        private_directory(ROOT / "status", 0o755)
        self.record = ROOT / "jobs" / (job + ".json")
        self.status_path = ROOT / "status" / (job + ".json")
        self.log_path = ROOT / "jobs" / (job + ".log")
        if self.record.exists():
            self.state = json.loads(self.record.read_text())
            if self.state["checksum"] != checksum:
                validate_repair(self.state, previous_checksum)
                self.state["previous_checksum"] = self.state["checksum"]
                self.state["checksum"] = checksum
                self.state["completed"] = [stage for stage in self.state["completed"] if stage in {"prepare", "engine"}]
                self.save()
        else:
            self.state = {"schema": 1, "job": job, "checksum": checksum, "stage": "prepare", "completed": [], "status": "queued", "created_at": int(time.time())}
            self.save()
        self.release = ROOT / "releases" / checksum
        self.environment = {"PATH": "/var/lib/gaoji-app/tools/bin:/run/current-system/sw/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root", "LANG": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive"}
        self.docker = ""

    def save(self, **values) -> None:
        self.state.update(values, updated_at=int(time.time()))
        atomic(self.record, self.state)
        self.publish_status()

    def publish_status(self) -> None:
        public = {key: self.state[key] for key in ("schema", "job", "checksum", "stage", "completed", "status", "created_at", "updated_at", "message", "instance_id", "app_id", "progress", "rejected") if key in self.state}
        atomic(self.status_path, public, 0o644)

    def run(self, arguments: list[str], timeout: int = 1800, *, capture: bool = False, check: bool = True) -> str:
        descriptor = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "ab", buffering=0) as log:
                if not capture and ("build" in arguments[1:2] or "pull" in arguments[1:2] or "pull" in arguments and "compose" in arguments):
                    from src.local_runtime.image_install import run_observed
                    code = run_observed(arguments, timeout=timeout, environment=self.environment, log=log,
                        report=lambda value: self.save(progress=value))
                    if check and code:
                        hint = (self.state.get("progress") or {}).get("failure")
                        raise InstallError((hint or "下载或构建失败，详细原因已保存在私有安装日志") + "；已有数据保留，可继续原任务")
                    return ""
                result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else log,
                    stderr=log, timeout=timeout, env=self.environment, check=False)
            if check and result.returncode:
                raise InstallError("此步骤没有完成，详细诊断已保存在服务器的私有安装日志中")
            return (result.stdout or b"").decode("utf-8", errors="replace") if not check or result.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            raise InstallError("此步骤耗时超过上限；请重新核对安装状态后重试") from None

    def setup_engine(self) -> None:
        self.docker = shutil.which("docker", path=self.environment["PATH"]) or ""
        if Path("/var/run/docker.sock").exists():
            if not self.docker:
                raise InstallError("已有 Docker 服务但未找到客户端；不会覆盖现有服务")
            self.environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
            self.run([self.docker, "info"], timeout=30)
            self.run([self.docker, "compose", "version"], timeout=30)
            self.state["engine_unit"] = "docker.service"
            return
        os_info = read_os()
        if os_info.get("ID") == "nixos":
            self.docker = str(ROOT / "tools/bin/docker")
            if not Path(self.docker).exists():
                raise InstallError("NixOS 安装工具尚未准备完成")
            owned_unit("gaoji-app-engine.service", """[Unit]
Description=Gaoji application container engine
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
Environment=PATH=/var/lib/gaoji-app/tools/bin:/run/current-system/sw/bin
ExecStart=/var/lib/gaoji-app/tools/bin/dockerd --host=unix:///run/gaoji-app/docker.sock --data-root=/var/lib/gaoji-app/docker --exec-root=/run/gaoji-app/exec --pidfile=/run/gaoji-app/docker.pid
RuntimeDirectory=gaoji-app
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=5
Delegate=yes
KillMode=process
TasksMax=infinity
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
""")
            self.run(["systemctl", "daemon-reload"], timeout=30)
            self.run(["systemctl", "start", "gaoji-app-engine.service"], timeout=180)
            self.environment["DOCKER_HOST"] = "unix:///run/gaoji-app/docker.sock"
            self.state["engine_unit"] = "gaoji-app-engine.service"
        elif os_info.get("ID") in {"debian", "ubuntu"}:
            if (self.docker or Path("/var/lib/docker").exists()) and not self.state.get("docker_repository_owned"):
                raise InstallError("检测到未运行的旧 Docker 环境，请确认状态后再安装；不会自动替换")
            distro, version = os_info["ID"], os_info.get("VERSION_ID")
            if (distro, version) not in {("debian", "12"), ("debian", "13"), ("ubuntu", "22.04"), ("ubuntu", "24.04")}:
                raise InstallError("Unsupported Linux version")
            for package in ("docker.io", "docker-compose", "podman-docker", "containerd", "runc"):
                installed = self.run(["dpkg-query", "-W", "-f=${Status}", package], timeout=30, capture=True, check=False)
                if "install ok installed" in installed:
                    raise InstallError("存在可能冲突的容器组件，不会自动卸载它们")
            suite = {("debian", "12"): "bookworm", ("debian", "13"): "trixie", ("ubuntu", "22.04"): "jammy", ("ubuntu", "24.04"): "noble"}[(distro, version)]
            self.run(["apt-get", "update"])
            self.run(["apt-get", "install", "-y", "--no-install-recommends", "ca-certificates", "curl"])
            key = Path("/etc/apt/keyrings/gaoji-docker.asc")
            key.parent.mkdir(parents=True, exist_ok=True)
            if key.exists() or Path("/etc/apt/sources.list.d/gaoji-docker.sources").exists():
                if not self.state.get("docker_repository_owned"):
                    raise InstallError("安装源文件已存在且不属于此任务")
            self.save(docker_repository_owned=True)
            self.run(["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--tlsv1.2", "--max-time", "60",
                f"https://download.docker.com/linux/{distro}/gpg", "-o", str(key)], timeout=90)
            os.chmod(key, 0o644)
            source = Path("/etc/apt/sources.list.d/gaoji-docker.sources")
            source.write_text(f"Types: deb\nURIs: https://download.docker.com/linux/{distro}\nSuites: {suite}\nComponents: stable\nArchitectures: amd64\nSigned-By: {key}\n")
            self.run(["apt-get", "update"])
            self.run(["apt-get", "install", "-y", "--no-install-recommends", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"])
            self.docker = "/usr/bin/docker"
            self.run(["systemctl", "enable", "--now", "docker.service"], timeout=180)
            self.environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
            self.state["engine_unit"] = "docker.service"
        else:
            raise InstallError("Unsupported Linux distribution")
        self.run([self.docker, "info"], timeout=60)
        self.run([self.docker, "compose", "version"], timeout=30)

    def execute(self) -> None:
        if self.state.get("purged_at"):
            raise InstallError("原实例已被明确删除，请新建安装任务，不要重放旧任务")
        request = self.archive.parent / "application.json"
        if request.exists():
            self.execute_application(request)
            return
        self.execute_gaoji()

    def execute_application(self, request: Path) -> None:
        with (ROOT / "jobs/install.lock").open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.state = json.loads(self.record.read_text())
            if self.state.get("status") == "completed":
                self.publish_status()
                return
            try:
                os_info = read_os()
                if (os_info.get("ID") != "ubuntu" or os_info.get("VERSION_ID") not in {"22.04", "24.04"}
                        or os.uname().machine != "x86_64" or not Path("/run/systemd/system").is_dir()):
                    raise InstallError("网站部署当前仅支持 Ubuntu 22.04/24.04 x86_64")
                if request.is_symlink() or request.stat().st_size > 8192:
                    raise InstallError("软件任务请求无效")
                value = json.loads(request.read_text())
                if value.get("operation") in {"install", "update"} and shutil.disk_usage(ROOT).free < 2 * 1024**3:
                    raise InstallError("可用空间不足 2 GiB，未开始部署")
                extract_bundle(self.archive, self.release, self.checksum)
                sys.path.insert(0, str(self.release))
                from src.deployment_platform.static_site import StaticSite
                StaticSite(self, value, root=ROOT).execute()
            except Exception as error:
                import traceback
                descriptor = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(descriptor, "a") as log:
                    traceback.print_exc(file=log)
                self.save(status="failed", message=str(error) if isinstance(error, (InstallError, ValueError)) else "网站操作未完成，原有数据保留，请查询原任务")

    def execute_gaoji(self) -> None:
        if self.state.get("purged_at"):
            raise InstallError("原实例已被明确删除，请新建安装任务，不要重放旧任务")
        if self.state.get("instance_id") and (ROOT / "data/instances" / self.state["instance_id"] / "config/lifecycle.json").exists():
            raise InstallError("实例已由服务维护任务接管，请从原任务重新启用，不要重新安装覆盖")
        lock = ROOT / "jobs/install.lock"
        with lock.open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            # A queued old installer must not overwrite a maintenance result
            # produced while it was waiting for the shared installation lock.
            self.state = json.loads(self.record.read_text())
            if self.state.get("purged_at"):
                raise InstallError("原实例已被明确删除，请新建安装任务，不要重放旧任务")
            if self.state.get("instance_id") and (ROOT / "data/instances" / self.state["instance_id"] / "config/lifecycle.json").exists():
                raise InstallError("实例已由服务维护任务接管，请从原任务重新启用，不要重新安装覆盖")
            if self.state["checksum"] != self.checksum:
                raise InstallError("安装版本在等待期间变化，请读取原任务状态")
            if self.state["status"] == "awaiting_setup":
                # Recover the public projection after an earlier bootstrap or
                # interrupted status write without replaying installation.
                self.publish_status()
                return
            try:
                self.save(status="running", stage="prepare", message="正在核对运行包")
                if os.uname().machine != "x86_64" or not Path("/run/systemd/system").is_dir():
                    raise InstallError("安装器仅支持带 systemd 的 x86_64 Linux 服务器")
                if not self.state["completed"] and shutil.disk_usage(ROOT).free < 10 * 1024**3:
                    raise InstallError("可用磁盘空间不足 10 GiB，未开始安装运行组件")
                manifest = extract_bundle(self.archive, self.release, self.checksum)
                sys.path.insert(0, str(self.release))
                from src.local_runtime.instances import InstanceStore
                from src.local_runtime.official_configuration import platforms
                from src.local_runtime.capacity import prepare_directory
                from src.local_runtime.files import write_json
                from src.local_runtime.runtime import compose_document, runtime_environment
                store = InstanceStore(ROOT / "data")
                if not self.state.get("instance_id"):
                    config = store.create("高级")
                    platform_path = self.archive.parent.parent / "platforms.json"
                    if platform_path.exists():
                        if platform_path.is_symlink():
                            raise InstallError("平台配置文件无效")
                        selected = platforms(json.loads(platform_path.read_text()))
                        settings = {**config["settings"], "platforms": selected}
                        config = store.update(config["id"], settings, expected_revision=config["revision"])
                    self.save(instance_id=config["id"])
                identifier = self.state["instance_id"]
                config, private = store.snapshot(identifier)
                selected = platforms(config["settings"].get("platforms"))
                directory = store.directory(identifier)
                prepare_directory(directory)
                self.done("prepare")
                self.save(stage="engine", message="正在准备容器运行环境")
                self.setup_engine()
                self.save(docker=self.docker, docker_host=self.environment["DOCKER_HOST"])
                self.done("engine")
                self.save(stage="runtime", message="正在安装机器人组件，首次安装可能较久")
                content_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                image = "gaoji-app-runtime:" + content_hash
                self.save(runtime_image=image)
                image_id = self.run([self.docker, "image", "ls", "--quiet", image], timeout=30, capture=True).strip()
                if not image_id:
                    from src.local_runtime.image_install import import_policy, prepare_image
                    from src.local_runtime.releases import ReleaseError
                    try:
                        import_policy(self.archive.parent, self.release, self.checksum)
                        prepare_image(self.docker, self.release, self.checksum, image, self.run, lambda message: self.save(message=message, progress=None))
                    except ReleaseError as error:
                        raise InstallError(str(error)) from None
                self.run([self.docker, "image", "inspect", image], timeout=30)
                self.check_runtime(image)
                self.done("runtime")
                self.save(stage="database", database_started=True, message="正在准备独立数据库与 QQ 会话目录")
                for child in ("napcat/QQ", "napcat/config"):
                    private_directory(directory / child)
                for child in ("state", "cache", "files"):
                    os.chown(directory / child, 10001, 10001)
                for filename, data in (("postgres.password", private["database_password"].encode()), ("admin.secret", secrets.token_bytes(32))):
                    path = directory / "config" / filename
                    if not path.exists():
                        with path.open("xb") as secret:
                            os.fchmod(secret.fileno(), 0o600)
                            secret.write(data)
                            secret.flush()
                            os.fsync(secret.fileno())
                    if filename == "admin.secret":
                        os.chown(path, 10001, 10001)
                webui = directory / "napcat/config/webui.json"
                if selected["onebot"] and not webui.exists():
                    write_json(directory / "napcat/config/napcat.json", {})
                    write_json(webui, {"host": "0.0.0.0", "port": 6099, "prefix": "", "token": private["napcat_token"], "loginRate": 3})
                runtime_path = directory / "config/runtime.json"
                if not runtime_path.exists():
                    write_json(runtime_path, {"schema": 1, "environment": runtime_environment(config, private, configured=False)})
                    os.chown(runtime_path, 10001, 10001)
                images = json.loads((self.release / "images.lock.json").read_text())
                compose = directory / "compose.json"
                write_json(compose, compose_document(config, directory, runtime_image=image, images=images))
                command = [self.docker, "compose", "--file", str(compose)]
                self.run(command + ["pull", "database", *(["napcat"] if selected["onebot"] else [])], timeout=1800)
                self.run(command + ["up", "--detach", "--wait", "--wait-timeout", "180", "database"], timeout=210)
                self.check_database(command)
                self.run(command + ["run", "--rm", "--no-deps", "bot", "migrate"], timeout=600)
                self.done("database")
                self.save(stage="service", message="正在配置后台常驻服务")
                unit = "gaoji-app-" + identifier + ".service"
                owned_unit(unit, f"""[Unit]
Description=Independent Gaoji application {identifier}
After=network-online.target {self.state['engine_unit']}
Requires={self.state['engine_unit']}
[Service]
Type=oneshot
RemainAfterExit=yes
Environment=DOCKER_HOST={self.environment['DOCKER_HOST']}
Environment=PATH={self.environment['PATH']}
ExecStart={self.docker} compose --file {compose} up --detach
ExecStop={self.docker} compose --file {compose} stop
TimeoutStartSec=300
TimeoutStopSec=90
[Install]
WantedBy=multi-user.target
""")
                self.run(["systemctl", "daemon-reload"], timeout=30)
                self.run(["systemctl", "start", unit], timeout=330)
                self.setup_controller(directory)
                self.done("service")
                if selected["onebot"]:
                    self.save(stage="verify", message="正在检查 QQ 登录入口")
                    ready = False
                    for _ in range(60):
                        try:
                            with urlopen(f"http://127.0.0.1:{config['ports']['napcat']}/webui/", timeout=3) as response:
                                ready = response.status == 200
                        except Exception:
                            pass
                        if ready:
                            break
                        time.sleep(2)
                    if not ready:
                        raise InstallError("QQ 登录入口暂未就绪，现有数据库会保留，可重试检测")
                    self.done("verify")
                    self.save(status="awaiting_setup", message="安装环境已就绪，等待登录自己的 QQ 并配置模型")
                else:
                    self.done("verify")
                    self.save(status="awaiting_setup", message="官方环境已就绪，等待配置官方凭据与模型")
            except Exception as error:
                self.save(status="failed", message=str(error) if isinstance(error, InstallError) else "安装未完成，请查看私有安装日志并重试")
                raise

    def done(self, stage: str) -> None:
        completed = list(self.state["completed"])
        if stage not in completed:
            completed.append(stage)
        self.save(completed=completed, progress=None)

    def check_runtime(self, image: str) -> None:
        from src.local_runtime.releases import RUNTIME_CHECK_TIMEOUT, runtime_check_arguments
        self.save(message="正在检查源码权限、运行依赖、中文字体和浏览器")
        self.run(runtime_check_arguments(self.docker, image), timeout=RUNTIME_CHECK_TIMEOUT)

    def check_database(self, command: list[str]) -> None:
        result = self.run(command + ["exec", "-T", "database", "psql", "-U", "gaoji", "-d", "gaoji", "-Atc",
            "SELECT (current_setting('server_version_num')::int / 10000 = 17)::text || ':' || EXISTS(SELECT 1 FROM pg_available_extensions WHERE name = 'vector')::text"],
            capture=True, timeout=30).strip()
        if result != "true:true":
            raise InstallError("数据库版本或 pgvector 依赖不满足要求，尚未执行数据库迁移")

    def setup_controller(self, directory: Path) -> None:
        from src.local_runtime.files import read_json, write_json
        owner_file = ROOT / "bootstrap" / self.job / "owner.json"
        owner = read_json(owner_file)
        uid = owner.get("uid")
        if type(uid) is not int or not 0 <= uid < 2**31:
            raise InstallError("安装用户身份记录无效")
        control_path = directory / "config/control.json"
        if control_path.exists():
            control = read_json(control_path)
            if control.get("owner_uid") != uid:
                raise InstallError("不能更换现有实例的管理用户")
        else:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            control = {"schema": 1, "port": port, "token": secrets.token_urlsafe(32), "owner_uid": uid, "instance_id": self.state["instance_id"]}
            write_json(control_path, control)
        access_root = ROOT / "control-access"
        private_directory(access_root, 0o711)
        access = access_root / (self.job + ".json")
        write_json(access, control)
        os.chown(access, uid, -1)
        os.chmod(access, 0o600)
        python = shutil.which("python3", path=self.environment["PATH"])
        if not python:
            raise InstallError("未找到实例管理器的运行环境")
        from src.local_runtime.services import controller_units
        private_directory(ROOT / "sandbox-images")
        for name, body in controller_units(ROOT, directory, self.job, self.state, python).items():
            owned_unit(name, body)
        unit = "gaoji-app-control-" + self.state["instance_id"] + ".service"
        self.run(["systemctl", "daemon-reload"], timeout=30)
        self.run(["systemctl", "start", "gaoji-app-network-" + self.state["instance_id"] + ".service"], timeout=130)
        self.run(["systemctl", "start", unit], timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--previous-sha256")
    parser.add_argument("--validate-repair", action="store_true")
    parser.add_argument("--prepare-update", action="store_true")
    parser.add_argument("--operation")
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("Administrator installation privileges are required.", file=sys.stderr)
        return 1
    try:
        if args.prepare_update:
            if args.validate_repair or args.previous_sha256 or not re.fullmatch(r"[a-f0-9]{32}", args.job):
                raise InstallError("Invalid update request")
            release = ROOT / "releases" / args.sha256
            if not re.fullmatch(r"[a-f0-9]{64}", args.sha256):
                raise InstallError("Invalid runtime checksum")
            extract_bundle(args.archive, release, args.sha256)
            sys.path.insert(0, str(release))
            from src.local_runtime.image_install import import_policy
            import_policy(args.archive.parent, release, args.sha256)
            from src.local_runtime.update_dispatch import dispatch
            dispatch(ROOT, args.job, args.operation, args.sha256, sys.executable)
        elif args.validate_repair:
            if not re.fullmatch(r"[a-f0-9]{32}", args.job):
                raise InstallError("Invalid job")
            validate_repair(json.loads((ROOT / "jobs" / (args.job + ".json")).read_text()), args.previous_sha256)
        else:
            installer = Installer(args.job, args.archive, args.sha256, args.previous_sha256)
            installer.execute()
            if installer.state.get("status") == "failed":
                return 1
        return 0
    except Exception:
        print("Installation did not finish. Read the persisted installation status before retrying.", file=sys.stderr)
    return 1


def validate_repair(state: dict, previous: str | None) -> None:
    if not isinstance(previous, str) or not re.fullmatch(r"[a-f0-9]{64}", previous) or state.get("checksum") != previous:
        raise InstallError("Existing installation checksum does not match the repair request")
    if state.get("status") != "failed" or state.get("stage") not in {"prepare", "engine", "runtime"} or state.get("database_started"):
        raise InstallError("只能替换尚未创建数据库的失败安装包；已有实例必须走更新流程")


if __name__ == "__main__":
    raise SystemExit(main())
