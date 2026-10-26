"""Durable Docker Desktop updates using the shared database-copy transaction."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace

from .desktop import DesktopController, DesktopMaintenance, ROOT, SOURCE
from .files import file_lock, read_json, write_json
from .releases import checksum, runtime_check_arguments, RUNTIME_CHECK_TIMEOUT
from .setup_clients import SetupError, request_json
from .update_worker import UpdateWorker, validate_request
from .updates import RuntimeUpdate


def docker(*args, timeout=60):
    try:
        result = subprocess.run(["/usr/local/bin/docker", "--host", "unix:///var/run/docker.sock", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False)
        if result.returncode:
            raise SetupError("本机更新组件操作未完成，数据仍保留")
        return result.stdout.decode()
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("本机更新组件暂不可用，请继续原更新任务") from None


def image(role, digest):
    name = ("gaoji-app-runtime:" if role == "runtime" else "gaoji-desktop-controller:") + checksum(digest)
    row = json.loads(docker("image", "inspect", name))[0]
    if (not re.fullmatch(r"sha256:[a-f0-9]{64}", row.get("Id", ""))
            or row.get("Config", {}).get("Labels", {}).get("gaoji.desktop.runtime-digest") != digest):
        raise SetupError("本机运行镜像身份不匹配，未切换实例")
    return name, row["Id"]


def engine_socket_matches(row):
    target = "/var/run/docker.sock"
    mounts = [m for m in row.get("Mounts", []) if m.get("Destination") == target]
    if len(mounts) != 1 or mounts[0].get("Type") != "bind" or not mounts[0].get("RW"):
        return False
    if mounts[0].get("Source") == target:
        return True
    # Desktop may normalize both requested and effective paths to its proxy.
    requested = [m for m in row.get("HostConfig", {}).get("Mounts", []) if m.get("Target") == target]
    return (mounts[0].get("Source") == "/run/host-services/docker.proxy.sock"
        and len(requested) == 1 and requested[0].get("Type") == "bind"
        and requested[0].get("Source") in {target, "/run/host-services/docker.proxy.sock"}
        and not requested[0].get("ReadOnly"))


def register_release(root, digest):
    """Read metadata from a verified immutable image, not host-supplied paths."""
    name, runtime_id = image("runtime", digest)
    _, controller_id = image("controller", digest)
    folder = root / "releases" / checksum(digest)
    for parent in (folder.parent, folder):
        if parent.is_symlink():
            raise SetupError("本机版本记录目录异常")
        parent.mkdir(mode=0o700, exist_ok=True)
    receipt = {"checksum": digest, "image": name, "image_id": runtime_id, "controller_id": controller_id}
    if (folder / "desktop-release.json").exists():
        if read_json(folder / "desktop-release.json") != receipt:
            raise SetupError("同一版本镜像身份已变化，未覆盖原版本记录")
        return
    for filename in ("manifest.json", "images.lock.json"):
        raw = docker("run", "--rm", "--pull=never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "64m",
            "--entrypoint", "cat", controller_id, "/opt/gaoji/" + filename)
        if len(raw) > 8 * 1024**2:
            raise SetupError("本机版本元数据过大")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise SetupError("本机版本元数据无效")
        write_json(folder / filename, value)
    write_json(folder / "desktop-release.json", receipt)


class DesktopRuntimeUpdate(RuntimeUpdate, DesktopMaintenance):

    def begin(self, kind, *, metadata=None):
        super().begin(kind, metadata=metadata)
        self.c.compose(["up", "--detach", "--wait", "--wait-timeout", "90", "database"], timeout=120)
        # Recheck after writers have stopped, so a new task cannot race this gate.
        present = self.command(["exec", "-T", "database", "psql", "-U", "gaoji", "-d", "gaoji", "-Atc",
            "SELECT to_regclass('qq_bot.subagent_tasks') IS NOT NULL"], timeout=30)
        if present.strip() == b"t":
            active = self.command(["exec", "-T", "database", "psql", "-U", "gaoji", "-d", "gaoji", "-Atc",
                "SELECT count(*) FROM qq_bot.subagent_tasks WHERE status IN "
                "('received','planning','running','verifying','cancelling','revising','waiting_external')"], timeout=30)
            if int(active.strip()):
                raise SetupError("还有未完成的 Agent 任务；请先完成或取消任务，再更新机器人")

    def prepare_release(self):
        target = checksum(self.c.operation["target_checksum"])
        folder = self.c.root / "releases" / target
        receipt = read_json(folder / "desktop-release.json")
        name, identity = image("runtime", target)
        if receipt.get("image_id") != identity or receipt.get("checksum") != target:
            raise SetupError("待更新镜像与已验证记录不符")
        arguments = runtime_check_arguments(self.c.deployment["docker"], name)
        # Docker Desktop selects its native architecture, including Apple Silicon.
        offset = arguments.index("--platform")
        del arguments[offset:offset + 2]
        self.runtime_command(arguments, timeout=RUNTIME_CHECK_TIMEOUT)
        return SimpleNamespace(checksum=target, image=name, directory=folder,
            images=read_json(folder / "images.lock.json"))


class DesktopWorkerController(DesktopController):
    def persist(self, **changes):
        super().persist(**changes)
        if callback := getattr(self, "on_progress", None):
            callback(self.public_operation())


def prepare_update(payload, root=ROOT):
    if not isinstance(payload, dict) or set(payload) != {"request", "runner_checksum", "port"}:
        raise SetupError("本机更新请求无效")
    request = validate_request(payload["request"])
    runner = checksum(payload["runner_checksum"])
    port = payload["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise SetupError("本机管理端口无效")
    seed = read_json(root / "desktop.json")
    if request["job"] != seed["job"]:
        raise SetupError("更新任务不属于此本机实例")
    directory = root / "updates"
    if directory.is_symlink():
        raise SetupError("更新任务目录异常")
    directory.mkdir(mode=0o700, exist_ok=True)
    with file_lock(root / "jobs/install.lock", timeout=0.1):
        path = directory / (request["id"] + ".json")
        if path.exists():
            current = read_json(path)
            if any(current.get(key) != value for key, value in payload.items()):
                raise SetupError("相同任务编号的更新内容不一致")
            return
        for previous in directory.glob("*.json"):
            state = read_json(previous)
            if state.get("status") != "completed" and not state.get("finished"):
                raise SetupError("还有未完成更新，请继续原任务")
        controller = DesktopController(request["job"], root)
        controller.mutable()
        state = {"schema": 1, **payload, "status": "queued", "finished": False}
        if (controller.deployment["checksum"] != request["expected_checksum"]
                or controller.store.read(controller.identifier)["revision"] != request["revision"]):
            # A recorded refusal is terminal, unlike an uncertain Docker timeout.
            # The receipt worker reports it without ever stopping the instance.
            state.update(status="failed", finished=True, resumable=False, stage="refused",
                         message="准备期间配置或运行版本已变化；未开始更新，请重新查询后提交")
        write_json(path, state)


class DesktopUpdateWorker(UpdateWorker):
    controller_class = DesktopWorkerController
    transaction_class = DesktopRuntimeUpdate

    @property
    def name(self):
        return "gaoji-desktop-" + self.request["job"] + "-controller"

    def container(self, name=None):
        name = name or self.name
        names = docker("ps", "-a", "--format", "{{.Names}}").splitlines()
        if name not in names:
            return None
        row = json.loads(docker("container", "inspect", name))[0]
        job = self.request["job"]
        mounts = row.get("Mounts", [])
        data = [m for m in mounts if m["Destination"] == str(ROOT)]
        ports = row.get("HostConfig", {}).get("PortBindings", {}).get("8090/tcp")
        if (row.get("Config", {}).get("Labels", {}).get("gaoji.desktop.job") != job
                or row["Config"].get("Cmd") != ["serve"]
                or not re.fullmatch(r"[a-f0-9]{64}", row.get("Id", ""))
                or len(data) != 1 or data[0].get("Name") != f"gaoji-desktop-{job}-data"
                or data[0].get("Type") != "volume" or not data[0].get("RW")
                or len(mounts) != 2 or not engine_socket_matches(row)
                or row["HostConfig"].get("Privileged")
                or row["HostConfig"].get("NetworkMode") != "gaoji-desktop-" + job
                or ports != [{"HostIp": "127.0.0.1", "HostPort": str(self.state["port"])}]):
            raise SetupError("本机管理组件归属、挂载或端口异常，未替换")
        tag = row["Config"].get("Image", "")
        if not re.fullmatch(r"gaoji-desktop-controller:[a-f0-9]{64}", tag):
            raise SetupError("管理组件镜像来源无法核实")
        _, identity = image("controller", tag.split(":")[1])
        if row.get("Image") != identity:
            raise SetupError("原管理组件镜像身份已变化")
        return row

    def prepare(self, controller):
        register_release(self.root, controller.deployment["checksum"])
        register_release(self.root, self.request["target_checksum"])
        _, runner_id = image("controller", self.state["runner_checksum"])
        if "runner_image_id" in self.state and self.state["runner_image_id"] != runner_id:
            raise SetupError("更新执行器镜像已变化，未继续")
        self.save(runner_image_id=runner_id)
        self.container()

    def save(self, **values):
        super().save(**values)
        public = read_json(self.public_path)
        deployment = read_json(self.root / "jobs" / (self.request["job"] + ".json"))
        public.update(current_checksum=deployment["checksum"],
            controller_checksum=deployment.get("controller_checksum", deployment["checksum"]))
        print(json.dumps(public, ensure_ascii=False), flush=True)

    def install_services(self, deployment):
        # Replacement is deferred until the transaction has a durable outcome.
        checksum(deployment["controller_checksum"])

    def retire_bootstrap(self):
        pass

    def manager_state(self, unit):
        row = self.container()
        return "active" if row and row["State"]["Running"] else "inactive"

    def stop_manager(self, unit):
        if row := self.container():
            docker("update", "--restart=no", row["Id"])
            docker("stop", "--time", "30", row["Id"])

    def start_manager(self, unit):
        deployment = read_json(self.root / "jobs" / (self.request["job"] + ".json"))
        digest = deployment.get("controller_checksum", deployment["checksum"])
        tag, identity = image("controller", digest)
        row = self.container()
        retired = self.name + "-before-" + self.request["id"]
        if row and row["Image"] != identity:
            if row["State"]["Running"] or self.container(retired):
                raise SetupError("管理组件尚未安全停止或旧组件已存在，未覆盖")
            docker("rename", row["Id"], retired)
            row = None
        if not row:
            job = self.request["job"]
            docker("create", "--name", self.name, "--label", "gaoji.desktop.job=" + job,
                "--restart", "no", "--network", "gaoji-desktop-" + job,
                "--publish", f"127.0.0.1:{self.state['port']}:8090", "--memory", "512m", "--cpus", "1",
                "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                "--mount", f"type=volume,source=gaoji-desktop-{job}-data,target={ROOT}",
                "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                tag, "serve")
            row = self.container()
        docker("start", row["Id"])
        docker("update", "--restart=unless-stopped", row["Id"])

    def wait_controller(self):
        controller = self.controller or self.controller_class(self.request["job"], self.root)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                response = request_json(f"http://{self.name}:8090/versions", {},
                    {"Authorization": "Bearer " + controller.control["token"]}, timeout=2)
                data = response.get("data", {})
                if (data.get("instance_id") == controller.identifier
                        and data.get("checksum") == controller.deployment["checksum"]):
                    retired = self.container(self.name + "-before-" + self.request["id"])
                    if retired and not retired["State"]["Running"]:
                        docker("rm", retired["Id"])
                    return
            except (SetupError, AttributeError):
                pass
            time.sleep(0.5)
        raise SetupError("本机管理组件尚未通过连接检查，请继续原任务")
