"""Privileged, fixed-operation entry point for persistent instance service tasks."""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys

from .files import file_lock
from .backups import backup_id
from .lifecycle import Lifecycle
from .manager import ROOT
from .maintenance import sync_directory
from .releases import checksum, load_release, read_regular
from .services import APP_OWNER, RUNTIME_PATH, controller_units, write_unit
from .setup_clients import SetupError


def worker_unit(root, lifecycle, record, python):
    identifier = backup_id(record.get("id"))
    if record.get("job") != lifecycle.job or record.get("instance_id") != lifecycle.identifier:
        raise SetupError("执行器任务不属于本实例")
    deployment = lifecycle.deployment
    controller_units(root, lifecycle.directory, lifecycle.job, deployment, python)
    runner = root / "releases" / checksum(deployment.get("controller_checksum", deployment["checksum"]))
    return f"""[Unit]
Description=Gaoji instance service task {identifier}
After=network-online.target {deployment['engine_unit']}
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=oneshot
WorkingDirectory={runner}
Environment=PYTHONPATH={runner}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PATH={RUNTIME_PATH}
ExecStart={python} -m src.local_runtime.lifecycle_worker --job {lifecycle.job} --operation {identifier}
Restart=on-failure
RestartSec=15
TimeoutStartSec=infinity
UMask=0077
[Install]
WantedBy=multi-user.target
"""


def operation_task(job, root, identifier):
    record = json.loads(read_regular(root / "lifecycles" / (backup_id(identifier) + ".json"), 2 * 1024**2))
    if record.get("job") != backup_id(job) or record.get("id") != identifier:
        raise SetupError("维护任务不属于此部署")
    if record.get("kind") == "purge":
        from .purge import Purge
        return Purge(job, root)
    return Lifecycle(job, root)


def retire_completed_purge(task, identifier, *, python=sys.executable, systemctl=None):
    """Retire only this completed worker; a crash can replay its persisted receipt."""
    with file_lock(task.root / "jobs/install.lock", timeout=1):
        record = task.load(identifier)
        if record["kind"] != "purge" or record["status"] != "completed":
            return
        name = "gaoji-app-lifecycle-" + backup_id(identifier) + ".service"
        filename = task.unit_root / name
        link = task.unit_root / "multi-user.target.wants" / name
        for directory in (task.unit_root, link.parent):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise SetupError("数据已删除，但执行器目录归属异常，未删除启动项")
        if link.is_symlink():
            if os.readlink(link) != "../" + name:
                raise SetupError("执行器启动项已改变，未删除")
        elif link.exists():
            raise SetupError("执行器启动项归属异常，未删除")
        command = systemctl or (lambda *args: Lifecycle.systemctl(task, *args))
        if filename.exists() or filename.is_symlink():
            expected = "# " + APP_OWNER + "\n" + worker_unit(task.root, task, record, python)
            if read_regular(filename, 65536).decode() != expected:
                raise SetupError("执行器定义已变化，未删除")
            raw = command("show", name, "--property=LoadState,ActiveState,FragmentPath,DropInPaths")
            state = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
            if (set(state) != {"LoadState", "ActiveState", "FragmentPath", "DropInPaths"}
                    or state["FragmentPath"] != str(filename) or state["DropInPaths"]):
                raise SetupError("执行器存在外部覆盖，未删除")
            # Completion is durable before removing the boot link. No service
            # stop is needed: the current oneshot exits after publishing it.
            if link.is_symlink():
                link.unlink()
                sync_directory(link.parent)
            filename.unlink()
            sync_directory(task.unit_root)
        elif link.is_symlink():
            raise SetupError("执行器启动项缺少对应定义，未删除")
        command("daemon-reload")


def dispatch(root, job, identifier, *, confirm=False, erase_instance_id=None, python=sys.executable,
             lifecycle_class=None, register=write_unit, systemctl=None):
    if confirm is not True:
        raise SetupError("请确认本实例服务维护，不会自动派发")
    lifecycle = lifecycle_class(job, root) if lifecycle_class else operation_task(job, root, identifier)
    previous = lifecycle.load(identifier)
    deployment = lifecycle.deployment
    runner = load_release(root / "releases", deployment.get("controller_checksum", deployment["checksum"]))
    modules = ("lifecycle.py", "lifecycle_state.py", "lifecycle_worker.py")
    if previous["kind"] == "purge":
        modules += ("purge.py", "purge_inventory.py")
    for module in modules:
        if not (runner.directory / "src/local_runtime" / module).is_file():
            raise SetupError("服务端版本尚不支持此维护流程，请先更新管理器")
    if previous["kind"] == "purge":
        lifecycle.submit({"id": identifier, "confirm": True, "erase_instance_id": erase_instance_id})
    else:
        if erase_instance_id is not None:
            raise SetupError("普通服务维护不能隐式删除数据")
        lifecycle.submit({"id": identifier, "confirm": True})
    with file_lock(root / "jobs/install.lock", timeout=1):
        record = lifecycle.load(identifier)
        if record["status"] == "completed":
            return lifecycle.public(record)
        name = "gaoji-app-lifecycle-" + record["id"] + ".service"
        register(name, worker_unit(root, lifecycle, record, python))
        def run(*arguments):
            result = subprocess.run(["systemctl", *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                check=False, env={"PATH": RUNTIME_PATH})
            if result.returncode:
                raise SetupError("维护已保存但启动尚未确认，请查询原任务")
        command = systemctl or run
        command("daemon-reload")
        if previous["status"] in {"running", "failed"}:
            command("reset-failed", name)
        command("start", "--no-block", name)
        return lifecycle.public(record)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", choices=("uninstall", "reinstall", "purge"))
    action.add_argument("--operation")
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--erase-instance")
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("Administrator privileges are required for instance service maintenance.", file=sys.stderr)
        return 1
    try:
        if args.prepare:
            if args.dispatch or args.erase_instance:
                raise SetupError("预览不会自动提交维护")
            if args.prepare == "purge":
                from .purge import Purge
                result = Purge(args.job).prepare()
            else:
                result = Lifecycle(args.job).prepare(args.prepare)
        elif args.dispatch:
            result = dispatch(ROOT, args.job, args.operation, confirm=True, erase_instance_id=args.erase_instance)
        else:
            if args.erase_instance:
                raise SetupError("删除确认只能用于提交已预览的任务")
            task = operation_task(args.job, ROOT, args.operation)
            result = task.run(args.operation)
            retire_completed_purge(task, args.operation)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, SetupError) else "Instance maintenance was not confirmed. Read the original persisted task before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
