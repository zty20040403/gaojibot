"""Controller-owned lifecycle of the fixed per-instance sandbox service."""
from __future__ import annotations

import asyncio
import argparse
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time

from .files import file_lock, read_json, write_json
from .setup_clients import SetupError
from .lifecycle_state import lifecycle_present
from src.sandbox_runtime.client import SandboxClient
from src.sandbox_runtime.protocol import TOKEN_PATTERN
from src.sandbox_runtime.server import load_config, manager_from_config
from src.sandbox_runtime.quiesce import stop_instance


LIMITS = {"max_per_owner": 2, "max_total": 2, "memory_mib": 1024,
          "cpus": 1, "pids_limit": 256, "max_file_bytes": 20 * 1024 * 1024}
CONTAINER_SOCKET = "/run/gaoji-sandbox/runner.sock"


class SandboxService:
    def __init__(self, directory: Path, deployment: dict):
        self.directory, self.deployment = directory, deployment
        self.identifier = deployment["instance_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", self.identifier):
            raise ValueError("Invalid sandbox service instance")
        self.unit = "gaoji-app-sandbox-" + self.identifier + ".service"
        self.config_path = directory / "config/sandbox-runner.json"
        self.enabled_path = directory / "config/sandbox.enabled"
        self.socket_path = directory / "sandbox/socket/runner.sock"
        self.state_directory = directory / "sandbox/state"

    @property
    def available(self) -> bool:
        # The installer supplies this only after the pinned image was built and
        # checked. An RPC listener by itself is not a usable sandbox toolchain.
        return bool(re.fullmatch(r"gaoji-app-sandbox:[a-f0-9]{64}", self.deployment.get("sandbox_image", ""))
            and re.fullmatch(r"sha256:[a-f0-9]{64}", self.deployment.get("sandbox_image_id", "")))

    def command(self, *arguments: str, timeout: int = 90) -> str:
        try:
            result = subprocess.run(["systemctl", *arguments, self.unit], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
                env={"PATH": "/run/current-system/sw/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
        except (OSError, subprocess.TimeoutExpired):
            raise SetupError("沙盒服务操作结果未确认，请检查实例状态，未删除工作文件") from None
        if result.returncode:
            raise SetupError("沙盒服务未完成操作，工作文件已保留")
        return result.stdout.decode("utf-8", errors="replace").strip()

    def prepare(self) -> dict | None:
        if not self.available:
            return None
        for path, mode in ((self.directory / "sandbox", 0o700),
                           (self.socket_path.parent, 0o750), (self.state_directory, 0o700)):
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise SetupError("沙盒目录归属异常，未启动服务")
            os.chmod(path, mode)
        if os.geteuid() == 0:
            os.chown(self.socket_path.parent, -1, 10001)
        token = secrets.token_urlsafe(32)
        if self.config_path.exists() or self.config_path.is_symlink():
            info = self.config_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise SetupError("沙盒配置权限异常，未启动服务")
            previous = read_json(self.config_path)
            if previous.get("instance_id") != self.identifier or not TOKEN_PATTERN.fullmatch(previous.get("token", "")):
                raise SetupError("沙盒配置不属于当前实例")
            token = previous["token"]
        config = {"schema": 1, "instance_id": self.identifier, "socket_path": str(self.socket_path),
            "state_directory": str(self.state_directory), "token": token,
            "capacity_file": str(self.directory / "capacity/status.json"),
            "docker": self.deployment["docker"], "docker_host": self.deployment["docker_host"],
            "image": self.deployment["sandbox_image"], "limits": dict(LIMITS)}
        manager_from_config(config)
        if len(os.fsencode(self.socket_path)) > 100:
            raise SetupError("沙盒运行目录路径过长")
        write_json(self.config_path, config)
        return {"socket": CONTAINER_SOCKET, "token": token}

    def pause(self, *, service_stopped: bool = False) -> bool:
        was_enabled = self.enabled_path.exists()
        self.enabled_path.unlink(missing_ok=True)
        if was_enabled:
            descriptor = os.open(self.enabled_path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if self.config_path.exists():
            if not service_stopped:
                self.command("stop", timeout=180)
            # Even an already-inactive unit may have left workload containers.
            # Recheck Docker under the runner lock, not just systemd's Result.
            try:
                config = load_config(self.config_path)
                with file_lock(self.state_directory / ".runner.lock", timeout=1):
                    async def quiesce():
                        return await asyncio.wait_for(stop_instance(manager_from_config(config)), timeout=120)
                    asyncio.run(quiesce())
            except Exception:
                raise SetupError("沙盒暂停后复查失败，尚未开始备份，工作文件保留") from None
        return was_enabled

    def verify_image(self) -> None:
        config = load_config(self.config_path)
        async def inspect():
            return await manager_from_config(config)._run("docker", "image", "inspect", "--format", "{{.Id}}",
                config["image"], timeout=20)
        result = asyncio.run(inspect())
        if result.returncode or result.stdout.strip() != self.deployment["sandbox_image_id"]:
            raise SetupError("沙盒镜像缺失或与安装记录不一致，需要修复运行组件；不会自动拉取未知镜像")

    def start(self) -> None:
        connection = self.prepare()
        if connection is None:
            raise SetupError("沙盒镜像尚未安装并验证，不能启用")
        if not maintenance_allows_start(self.directory):
            raise SetupError("实例仍在维护中，不能启动沙盒")
        self.verify_image()
        write_json(self.enabled_path, {"schema": 1, "instance_id": self.identifier})
        try:
            self.command("start", timeout=30)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    async def probe():
                        return await asyncio.wait_for(SandboxClient(str(self.socket_path), connection["token"]).health(), timeout=2)
                    health = asyncio.run(probe())
                    if (health.get("channel_ready") is True and health.get("instance_id") == self.identifier
                            and health.get("image") == self.deployment["sandbox_image"]):
                        return
                except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                    pass
                time.sleep(0.25)
            raise SetupError("沙盒通道没有就绪，未启动新的聊天进程")
        except Exception:
            self.pause()
            raise


def service_unit(directory: Path, release: Path, python: str, engine_unit: str) -> str:
    identifier = directory.name
    if not re.fullmatch(r"[a-f0-9]{32}", identifier) or not re.fullmatch(r"(?:docker|gaoji-app-engine)\.service", engine_unit):
        raise ValueError("Invalid sandbox service target")
    paths = (str(directory), str(release), python)
    if any(not value.startswith("/") or re.search(r"[\s%$\x00]", value) for value in paths):
        raise ValueError("Invalid sandbox service path")
    config = directory / "config/sandbox-runner.json"
    return f"""[Unit]
Description=Gaoji isolated sandbox runner {identifier}
After=network-online.target {engine_unit}
Requires={engine_unit}
PartOf=gaoji-app-{identifier}.service
Before=gaoji-app-{identifier}.service
ConditionPathExists={directory}/config/sandbox.enabled
[Service]
Type=simple
WorkingDirectory={release}
Environment=PYTHONPATH={release}
Environment=PATH=/var/lib/gaoji-app/tools/bin:/run/current-system/sw/bin:/usr/bin:/bin
ExecCondition={python} -m src.local_runtime.sandbox_service --directory {directory}
ExecStart={python} -m src.sandbox_runtime.server --config {config}
ExecStopPost={python} -m src.sandbox_runtime.quiesce --config {config}
Restart=on-failure
RestartSec=5
TimeoutStopSec=180
MemoryMax=512M
CPUQuota=100%
TasksMax=128
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths={directory}/sandbox
RestrictAddressFamilies=AF_UNIX
[Install]
WantedBy=multi-user.target
"""


def maintenance_allows_start(directory: Path) -> bool:
    if lifecycle_present(directory):
        return False
    marker = directory / "config/maintenance.json"
    return not marker.exists() or read_json(marker).get("stage") == "resuming"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    return 0 if maintenance_allows_start(args.directory) else 1


if __name__ == "__main__":
    sys.exit(main())
