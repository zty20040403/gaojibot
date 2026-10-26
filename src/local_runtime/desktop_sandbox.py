"""Docker Desktop lifecycle for the existing authenticated sandbox runner."""
from __future__ import annotations

import json
import re
from pathlib import Path
import http.client
import socket

from .sandbox_image import SandboxImage, SandboxImageError
from .sandbox_service import SandboxService
from .setup_clients import SetupError
from src.storage_admission import CapacityError

ROOT = Path("/var/lib/gaoji-app")


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def sandbox_volume_sizes(identifier, names):
    if len(names) > 128 or any(not re.fullmatch(r"gaoji-app-" + identifier + r"-(nix|work-s[0-9a-f]{6,32})", name) for name in names):
        raise CapacityError("本机沙盒工作卷归属无法确认")
    connection = DockerConnection("localhost", timeout=10)
    try:
        # Desktop volume paths belong to its Linux VM. Ask the local engine for
        # sizes rather than treating absent macOS/Windows paths as zero bytes.
        connection.request("GET", "/system/df?type=volume")
        response = connection.getresponse()
        body = response.read(8 * 1024**2 + 1)
        if response.status != 200 or len(body) > 8 * 1024**2:
            raise CapacityError("本机工作卷容量暂不可读")
        found = {}
        for row in json.loads(body).get("Volumes") or []:
            if row.get("Name") not in names:
                continue
            labels = row.get("Labels") or {}
            kind = "nix-cache" if row["Name"] == "gaoji-app-" + identifier + "-nix" else "workspace"
            size = (row.get("UsageData") or {}).get("Size")
            if (labels.get("gaoji.app.instance") != identifier or labels.get("gaoji.app.resource") != kind
                    or type(size) is not int or size < 0 or row["Name"] in found):
                raise CapacityError("本机工作卷容量或归属不完整")
            found[row["Name"]] = size
        if set(found) != set(names):
            raise CapacityError("本机工作卷统计尚未完成")
        return list(found.values())
    except (OSError, http.client.HTTPException):
        raise CapacityError("本机工作卷容量暂不可读") from None
    finally:
        connection.close()


class DesktopSandboxImage(SandboxImage):
    def __init__(self, source, cache, *, deployment, root=ROOT):
        super().__init__(source, cache, docker=deployment["docker"], docker_host=deployment["docker_host"])
        self.volume = deployment["volume"]
        if self.volume != "gaoji-desktop-" + deployment["job"] + "-data":
            raise SandboxImageError("本机沙盒构建卷与实例不一致")
        self.prefix = self.root.relative_to(root).as_posix()
        # Two local instances may use the same recipe without sharing writable
        # build directories or taking over each other's build container.
        self.container += "-" + deployment["job"]

    def builder_mount_arguments(self):
        return [value for child in ("recipe", "out") for value in ("--mount",
            f"type=volume,source={self.volume},target=/{child},volume-subpath={self.prefix}/{child}"
            + (",readonly" if child == "recipe" else ""))]

    def builder_mounts_match(self, value):
        mounts = value.get("HostConfig", {}).get("Mounts", [])
        if len(mounts) != 2 or len(value.get("Mounts", [])) != 2:
            return False
        for child in ("recipe", "out"):
            matches = [m for m in mounts if m.get("Target") == "/" + child]
            if (len(matches) != 1 or matches[0].get("Type") != "volume" or matches[0].get("Source") != self.volume
                    or bool(matches[0].get("ReadOnly")) != (child == "recipe")
                    or matches[0].get("VolumeOptions", {}).get("Subpath") != self.prefix + "/" + child):
                return False
        return True


class DesktopSandboxService(SandboxService):
    def container(self):
        from .desktop_updates import docker, image, engine_socket_matches
        name = "gaoji-desktop-" + self.deployment["job"] + "-runner"
        names = docker("ps", "-a", "--format", "{{.Names}}").splitlines()
        if name not in names:
            return None
        row = json.loads(docker("container", "inspect", name))[0]
        settings = row.get("Config", {})
        labels = settings.get("Labels", {})
        mounts = row.get("Mounts", [])
        data = [m for m in mounts if m.get("Destination") == str(ROOT)]
        if (labels.get("gaoji.desktop.job") != self.deployment["job"]
                or labels.get("gaoji.desktop.runner") != self.identifier
                or settings.get("Cmd") != ["sandbox", self.identifier]
                or not re.fullmatch(r"gaoji-desktop-controller:[a-f0-9]{64}", settings.get("Image", ""))
                or len(mounts) != 2 or len(data) != 1 or data[0].get("Type") != "volume"
                or data[0].get("Name") != self.deployment["volume"] or not data[0].get("RW")
                or not engine_socket_matches(row)
                or row.get("HostConfig", {}).get("NetworkMode") != "none"
                or row["HostConfig"].get("Privileged")):
            raise SetupError("本机沙盒通道归属或挂载异常，未替换")
        if image("controller", settings["Image"].split(":")[1])[1] != row.get("Image"):
            raise SetupError("本机沙盒通道镜像与记录不符")
        return row

    def command(self, *arguments, timeout=90):
        from .desktop_updates import docker, image
        if arguments not in {("start",), ("stop",)}:
            raise SetupError("不支持的本机沙盒生命周期操作")
        row = self.container()
        if arguments == ("stop",):
            if row:
                docker("update", "--restart=no", row["Id"])
                docker("stop", "--time", "30", row["Id"], timeout=timeout)
            return ""
        digest = self.deployment.get("controller_checksum", self.deployment["checksum"])
        tag, identity = image("controller", digest)
        if row and row["Image"] != identity:
            if row["State"]["Running"]:
                raise SetupError("旧沙盒通道仍在运行，未覆盖")
            docker("rm", row["Id"])
            row = None
        if not row:
            job = self.deployment["job"]
            docker("create", "--name", "gaoji-desktop-" + job + "-runner", "--pull=never",
                "--label", "gaoji.desktop.job=" + job, "--label", "gaoji.desktop.runner=" + self.identifier,
                "--network", "none", "--restart", "no", "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
                "--log-driver", "local", "--log-opt", "max-size=5m", "--log-opt", "max-file=2",
                "--mount", f"type=volume,source={self.deployment['volume']},target={ROOT}",
                "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock", tag, "sandbox", self.identifier)
            row = self.container()
        docker("start", row["Id"])
        docker("update", "--restart=unless-stopped", row["Id"])
        return ""
