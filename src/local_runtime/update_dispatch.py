"""Dispatch only hash-verified desktop update requests, never renderer commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

from .backups import backup_id
from .files import file_lock, read_json, status_directory, write_json
from .instances import InstanceStore
from .releases import checksum, load_release, read_regular
from .services import RUNTIME_PATH, controller_units, write_unit
from .setup_clients import SetupError
from .storage import cleanup_pending
from .lifecycle_state import check_lifecycle
from .update_worker import validate_request


def worker_unit(root: Path, request: dict, runner_checksum: str, python: str, deployment: dict) -> str:
    request = validate_request(request)
    runner = root / "releases" / checksum(runner_checksum)
    # Reuse the service renderer's strict instance/path/engine validation.
    controller_units(root, root / "data/instances" / deployment["instance_id"], request["job"], deployment, python)
    return f"""[Unit]
Description=Gaoji version task {request['id']}
After=network-online.target {deployment['engine_unit']}
Requires={deployment['engine_unit']}
[Service]
Type=oneshot
WorkingDirectory={runner}
Environment=PYTHONPATH={runner}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PATH={RUNTIME_PATH}
ExecStart={python} -m src.local_runtime.update_worker --operation {request['id']}
Restart=on-failure
RestartSec=15
TimeoutStartSec=infinity
UMask=0077
[Install]
WantedBy=multi-user.target
"""


def dispatch(root: Path, job: str, operation: str, runner_checksum: str, python: str, *,
             register=write_unit, systemctl=None) -> dict:
    backup_id(job)
    backup_id(operation)
    runner_checksum = checksum(runner_checksum)
    path = root / "bootstrap" / job / runner_checksum / "requests" / (operation + ".json")
    request = validate_request(json.loads(read_regular(path, 32768)))
    if request["id"] != operation or request["job"] != job:
        raise SetupError("上传的更新请求编号不一致")
    if request["kind"] == "update" and request["target_checksum"] != runner_checksum:
        raise SetupError("目标版本不是当前已校验的客户端运行包")
    load_release(root / "releases", runner_checksum)
    load_release(root / "releases", request["target_checksum"])
    with file_lock(root / "jobs/install.lock", timeout=1):
        deployment = read_json(root / "jobs" / (job + ".json"))
        directory = InstanceStore(root / "data").directory(deployment["instance_id"])
        check_lifecycle(directory)
        operations = root / "updates"
        operations.mkdir(mode=0o700, exist_ok=True)
        if operations.is_symlink() or operations.stat().st_uid != os.geteuid() or operations.stat().st_mode & 0o077:
            raise SetupError("更新记录目录权限异常")
        record_path = operations / (operation + ".json")
        if record_path.exists():
            record = json.loads(read_regular(record_path, 32768))
            if record.get("request") != request or record.get("runner_checksum") != runner_checksum:
                raise SetupError("此任务编号已用于其他更新，未重复派发")
        else:
            if deployment["checksum"] != request["expected_checksum"]:
                raise SetupError("服务器版本已经变化，请刷新后重新确认")
            if read_json(directory / "instance.json")["revision"] != request["revision"]:
                raise SetupError("配置已经变化，请刷新后重新确认")
            if (directory / "config/maintenance.json").exists() or (directory / "config/update-worker.json").exists() or cleanup_pending(directory):
                raise SetupError("已有未结束维护，未提交新的版本任务")
            prior_path = directory / "config/operation.json"
            prior = read_json(prior_path) if prior_path.exists() else {}
            if prior.get("status") in {"queued", "running"}:
                raise SetupError("实例正在处理另一项操作，请稍后更新")
            record = {"schema": 1, "request": request, "runner_checksum": runner_checksum,
                "status": "queued", "stage": "prepare", "message": "更新请求已校验并保存", "created_at": int(time.time())}
            write_json(record_path, record)
        if record.get("finished") or record.get("status") == "completed":
            return record
        public_root = status_directory(root, "updates")
        public_path = public_root / (operation + ".json")
        if not public_path.exists():
            write_json(public_path, {"schema": 1, "id": operation, "job": job, "target_checksum": request["target_checksum"],
                "status": record["status"], "stage": record["stage"], "message": record["message"], "resumable": True})
            os.chmod(public_path, 0o644)
        name = "gaoji-app-update-" + operation + ".service"
        register(name, worker_unit(root, request, runner_checksum, python, deployment))
        def run(*arguments):
            result = subprocess.run(["systemctl", *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, check=False, env={"PATH": RUNTIME_PATH})
            if result.returncode:
                raise SetupError("更新任务已保存，但尚未确认启动，请查询原任务")
        command = systemctl or run
        command("daemon-reload")
        command("start", "--no-block", name)
        return record
