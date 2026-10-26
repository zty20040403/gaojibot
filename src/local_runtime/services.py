"""Owned systemd definitions shared by initial installation and runtime updates."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile

from .setup_clients import SetupError
from .sandbox_service import service_unit


APP_OWNER = "Gaoji desktop managed unit"
RUNTIME_PATH = "/var/lib/gaoji-app/tools/bin:/run/current-system/sw/bin:/usr/sbin:/usr/bin:/sbin:/bin"
UNIT_ROOT = Path("/usr/local/lib/systemd/system")


def retire_installer(job: str, root: Path = UNIT_ROOT) -> bool:
    """Remove only the completed job's owned boot link, retaining its receipt/unit."""
    if not re.fullmatch(r"[a-f0-9]{32}", job):
        raise SetupError("无效的安装任务编号")
    name = f"gaoji-app-install-{job}.service"
    for directory in (root, root / "multi-user.target.wants"):
        if not directory.exists() and not directory.is_symlink():
            return False
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise SetupError("安装器服务目录归属不符，未修改启动项")
    link = root / "multi-user.target.wants" / name
    if not link.exists() and not link.is_symlink():
        return False
    if not link.is_symlink() or os.readlink(link) != "../" + name:
        raise SetupError("安装器启动项指向其他位置，未修改")
    from .releases import read_regular
    body = read_regular(root / name, 65536).decode()
    header = body.splitlines()[0] if body else ""
    if not re.fullmatch(r"# Gaoji installer " + job + r" [a-f0-9]{64}", header):
        raise SetupError("同名安装器不属于此任务，未修改")
    link.unlink()
    from .maintenance import sync_directory
    sync_directory(link.parent)
    return True


def write_unit(name: str, body: str, root: Path = UNIT_ROOT) -> None:
    if not re.fullmatch(r"gaoji-app-[a-z0-9-]+\.service", name):
        raise SetupError("无效的实例服务名称")
    for directory in (root, root / "multi-user.target.wants"):
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise SetupError("服务目录归属不符，未修改系统服务")
    filename, link = root / name, root / "multi-user.target.wants" / name
    target = "../" + name
    if filename.is_symlink():
        raise SetupError("已有其他服务使用此名称，未覆盖")
    if filename.exists():
        info = filename.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or not filename.read_text().startswith("# " + APP_OWNER + "\n")):
            raise SetupError("已有其他服务使用此名称，未覆盖")
    if (link.is_symlink() and os.readlink(link) != target) or (not link.is_symlink() and link.exists()):
        raise SetupError("已有其他启动项使用此名称，未覆盖")
    descriptor, temporary = tempfile.mkstemp(dir=root)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("# " + APP_OWNER + "\n" + body)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, filename)
        if not link.is_symlink():
            link.symlink_to(target)
        for directory in (root, link.parent):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def controller_units(root: Path, directory: Path, job: str, deployment: dict, python: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-f0-9]{32}", job) or not re.fullmatch(r"[a-f0-9]{64}", deployment["checksum"]):
        raise SetupError("无效的实例版本或部署编号")
    controller_checksum = deployment.get("controller_checksum", deployment["checksum"])
    if not re.fullmatch(r"[a-f0-9]{64}", controller_checksum):
        raise SetupError("无效的管理器版本")
    release = root / "releases" / controller_checksum
    sandbox = service_unit(directory, release, python, deployment["engine_unit"])
    identifier = deployment["instance_id"]
    if directory != root / "data/instances" / identifier:
        raise SetupError("控制服务路径不属于此实例")
    docker, docker_host = deployment["docker"], deployment["docker_host"]
    if (not re.fullmatch(r"/[a-zA-Z0-9/._-]+", docker)
            or not re.fullmatch(r"unix:///[a-zA-Z0-9/._-]+", docker_host)):
        raise SetupError("沙盒网络安装器只允许本机 Docker")
    return {"gaoji-app-sandbox-" + identifier + ".service": sandbox,
        "gaoji-app-network-" + identifier + ".service": f"""[Unit]
Description=Gaoji sandbox egress policy {identifier}
After={deployment['engine_unit']}
Requires={deployment['engine_unit']}
PartOf={deployment['engine_unit']}
Before=gaoji-app-sandbox-{identifier}.service
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={release}
Environment=PYTHONPATH={release}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PATH={RUNTIME_PATH}
Environment=DOCKER_HOST={docker_host}
ExecStart={python} -m src.sandbox_runtime.network --docker {docker}
TimeoutStartSec=120
UMask=0077
[Install]
WantedBy={deployment['engine_unit']}
""",
        "gaoji-app-control-" + identifier + ".service": f"""[Unit]
Description=Gaoji per-instance desktop controller
After=network-online.target {deployment['engine_unit']}
[Service]
Type=simple
WorkingDirectory={release}
Environment=PYTHONPATH={release}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PATH={RUNTIME_PATH}
ExecCondition=/bin/sh -c 'test ! -e {directory}/config/update-worker.json'
ExecStart={python} -m src.local_runtime.manager --job {job}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={directory} {root / 'data/instances'} {root / 'sandbox-images'}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
[Install]
WantedBy=multi-user.target
"""}
