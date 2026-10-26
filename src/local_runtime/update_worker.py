"""Persistent, instance-scoped update worker, separate from the HTTP controller."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .backups import backup_id
from .files import file_lock, read_json, status_directory, write_json
from .manager import Controller, PATH, ROOT
from .maintenance import sync_directory
from .releases import checksum, read_regular
from .services import UNIT_ROOT, controller_units, retire_installer, write_unit
from .setup_clients import SetupError, request_json
from .updates import RuntimeUpdate
from .lifecycle_state import check_lifecycle


def validate_request(value: dict) -> dict:
    required = {"id", "job", "kind", "expected_checksum", "target_checksum", "revision"}
    optional = {"rollback_id", "confirm_data_replacement"}
    if not isinstance(value, dict) or set(value) - optional != required:
        raise SetupError("更新请求字段无效")
    for field in ("id", "job"):
        backup_id(value[field])
    for field in ("expected_checksum", "target_checksum"):
        checksum(value[field])
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise SetupError("更新请求缺少配置版本")
    if value["kind"] == "rollback":
        backup_id(value.get("rollback_id"))
        if value.get("confirm_data_replacement") is not True:
            raise SetupError("请明确确认回退将恢复升级前数据")
    elif value["kind"] != "update" or set(value) & optional:
        raise SetupError("无效更新操作")
    return dict(value)


class WorkerController(Controller):
    def persist(self, **changes):
        super().persist(**changes)
        callback = getattr(self, "on_progress", None)
        if callback:
            callback(self.public_operation())


class UpdateWorker:
    controller_class = WorkerController
    transaction_class = RuntimeUpdate
    unit_root = UNIT_ROOT

    def __init__(self, operation: str, root: Path = ROOT):
        self.root = root
        self.path = root / "updates" / (backup_id(operation) + ".json")
        self.state = json.loads(read_regular(self.path, 32768))
        if not isinstance(self.state, dict) or self.state.get("schema") != 1:
            raise SetupError("更新任务格式无效")
        self.request = validate_request(self.state.get("request"))
        if self.request["id"] != operation:
            raise SetupError("更新任务编号不匹配")
        self.public_path = root / "status/updates" / (operation + ".json")
        self.controller = None

    def save(self, **values):
        self.state.update(values, updated_at=int(time.time()))
        write_json(self.path, self.state)
        status_directory(self.root, "updates")
        value = {key: self.state[key] for key in ("schema", "status", "stage", "message", "updated_at", "resumable", "finished") if key in self.state}
        value.update(id=self.request["id"], job=self.request["job"], target_checksum=self.request["target_checksum"])
        write_json(self.public_path, value)
        os.chmod(self.public_path, 0o644)

    def progress(self, operation):
        self.save(**{key: operation[key] for key in ("stage", "message") if key in operation})

    def systemctl(self, *arguments: str) -> str:
        try:
            result = subprocess.run(["systemctl", *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=120, env={"PATH": PATH, "LANG": "C.UTF-8"}, check=False)
            if result.returncode:
                raise SetupError("实例管理服务操作尚未确认，未修改其他服务")
            return result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired):
            raise SetupError("实例管理服务操作超时，需继续原更新任务核对") from None

    def install_services(self, deployment: dict):
        for name, body in controller_units(self.root, self.controller.directory, self.request["job"], deployment, sys.executable).items():
            write_unit(name, body)
        self.systemctl("daemon-reload")

    def wait_controller(self):
        controller = self.controller or self.controller_class(self.request["job"], self.root)
        access = controller.control
        deadline = time.monotonic() + 30
        while True:
            try:
                result = request_json(f"http://127.0.0.1:{access['port']}/status", {},
                    {"Authorization": "Bearer " + access["token"]}, timeout=2)
                if result.get("data", {}).get("instance", {}).get("id") == controller.identifier:
                    return
            except (SetupError, AttributeError):
                pass
            if time.monotonic() >= deadline:
                raise SetupError("管理服务已启动，但配置接口尚未确认可用")
            time.sleep(0.5)

    def manager_state(self, unit):
        return self.systemctl("show", unit, "--property=ActiveState", "--value")

    def stop_manager(self, unit):
        self.systemctl("stop", unit)

    def start_manager(self, unit):
        self.systemctl("start", unit)

    def retire_bootstrap(self):
        retire_installer(self.request["job"], self.unit_root)

    def prepare(self, controller):
        """Platform preparation before the manager is stopped."""

    def run(self):
        with file_lock(self.root / "jobs/install.lock", timeout=0.1):
            if self.state.get("status") == "completed" or self.state.get("finished"):
                return
            unit = None
            restart = False
            outcome = None
            guard = None
            try:
                controller = self.controller_class(self.request["job"], self.root)
                check_lifecycle(controller.directory)
                unit = "gaoji-app-control-" + controller.identifier + ".service"
                prior = controller.operation
                if controller.storage.busy():
                    raise SetupError("副本清理正在进行，未停止管理器或开始更新")
                if prior and prior.get("id") != self.request["id"] and prior.get("status") in {"queued", "running"}:
                    raise SetupError("实例正在处理其他操作，未开始更新")
                if (controller.directory / "config/maintenance.json").exists() and (not prior or prior.get("id") != self.request["id"]):
                    raise SetupError("已有未完成维护，未开始更新")
                self.prepare(controller)
                if "controller_was_running" not in self.state:
                    running = self.manager_state(unit)
                    if running not in {"active", "inactive", "failed"}:
                        raise SetupError("管理服务正在切换状态，请稍后重试")
                    self.save(controller_was_running=running == "active")
                restart = self.state["controller_was_running"]
                self.save(status="running", stage="acquiring", message="正在取得本实例更新权限")
                guard = controller.directory / "config/update-worker.json"
                if guard.exists() and read_json(guard).get("id") != self.request["id"]:
                    guard = None
                    raise SetupError("另一个更新执行器正在接管实例")
                write_json(guard, {"id": self.request["id"]})
                self.stop_manager(unit)
                if self.manager_state(unit) not in {"inactive", "failed"}:
                    raise SetupError("管理服务未停止，尚未执行版本切换")
                # Re-read after stop, catching an operation submitted during the
                # initial inspection. Never kill it and then overwrite its receipt.
                controller = self.controller = self.controller_class(self.request["job"], self.root)
                check_lifecycle(controller.directory)
                prior = controller.operation
                if controller.storage.busy():
                    raise SetupError("副本清理刚刚开始，已放弃更新并恢复管理器")
                if prior and prior.get("id") != self.request["id"] and prior.get("status") in {"queued", "running"}:
                    raise SetupError("另一项操作刚刚开始，已放弃更新并恢复管理器")
                if not prior or prior.get("id") != self.request["id"]:
                    config = controller.store.read(controller.identifier)
                    if config["revision"] != self.request["revision"] or controller.deployment["checksum"] != self.request["expected_checksum"]:
                        raise SetupError("配置或运行版本已变更，未开始更新")
                else:
                    if any(prior.get(key) != value for key, value in self.request.items()):
                        raise SetupError("同一更新编号的请求内容不一致，未重复执行")
                # The initial bootstrap pins the original archive. Once runtime
                # updates own the instance it must never be replayed at boot.
                self.retire_bootstrap()
                # Keep the management protocol current even when explicitly
                # rolling Bot and its database back to an older runtime.
                controller.controller_checksum = checksum(self.state.get("runner_checksum", self.request["target_checksum"]))
                controller.deployment["controller_checksum"] = controller.controller_checksum
                self.install_services(controller.deployment)
                write_json(self.root / "jobs" / (self.request["job"] + ".json"), controller.deployment)
                if not prior or prior.get("id") != self.request["id"]:
                    controller.operation = {**self.request, "status": "queued", "stage": "prepare", "created_at": int(time.time())}
                    controller.persist()
                controller.on_progress = self.progress
                transaction = self.transaction_class(controller, self.install_services)
                transaction.run_update()
                outcome = {key: controller.operation[key] for key in ("status", "stage", "message")}
                self.save(status="running", stage="controller_resume", message="版本操作已记录，正在恢复管理服务")
                status_path = self.root / "status" / (self.request["job"] + ".json")
                if status_path.exists():
                    public = read_json(status_path)
                    public.update(checksum=controller.deployment["checksum"], updated_at=int(time.time()))
                    write_json(status_path, public)
                    os.chmod(status_path, 0o644)
            except Exception as error:
                outcome = {"status": "failed", "stage": "worker_failed", "message": str(error) if isinstance(error, SetupError)
                    else "更新任务尚未完成，原数据已保留；请查询原任务，不要重新安装"}
            finally:
                if guard is not None:
                    try:
                        guard.unlink(missing_ok=True)
                        sync_directory(guard.parent)
                    except OSError:
                        outcome = {"status": "failed", "stage": "controller_unverified", "message": "管理器更新门禁尚未解除，请继续原任务"}
                if unit and restart:
                    try:
                        self.start_manager(unit)
                        if self.manager_state(unit) != "active":
                            raise SetupError("管理服务尚未恢复")
                        self.wait_controller()
                    except Exception:
                        outcome = {"status": "failed", "stage": "controller_unverified",
                            "message": "版本操作已记录，但管理服务尚未恢复；请继续原更新任务"}
                if outcome:
                    pending = self.controller is not None and (self.controller.directory / "config/maintenance.json").exists()
                    resumable = pending or outcome["stage"] == "controller_unverified"
                    self.save(**outcome, resumable=resumable, finished=not resumable)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("Administrator privileges are required for runtime updates.", file=sys.stderr)
        return 1
    worker = UpdateWorker(args.operation)
    worker.run()
    # A handled failure needs explicit retry. A killed/crashed worker may be
    # restarted by systemd with the same operation ID and journal.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
