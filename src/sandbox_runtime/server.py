"""Per-instance Unix-socket runner. Only this process has Docker access."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import os
from pathlib import Path
import secrets
import signal
import stat

from src.local_runtime.files import file_lock, read_json
from .docker import DockerSandboxManager, SandboxError
from .protocol import (ID_PATTERN, MUTATIONS, REQUEST_LIMIT, TOKEN_PATTERN, VERSION,
                       ProtocolError, fingerprint, read_header, validate_request, write_frame)
from .receipts import Receipts


class SandboxRunner:
    def __init__(self, manager: DockerSandboxManager, *, socket_path: Path, state_directory: Path,
                 token: str, socket_gid: int | None = None, concurrency: int = 4, capacity_check=None,
                 project_volumes=None):
        if not manager.namespace:
            raise ValueError("The runner requires an isolated instance driver")
        if not TOKEN_PATTERN.fullmatch(token) or not socket_path.is_absolute() or len(os.fsencode(socket_path)) > 100:
            raise ValueError("Invalid runner address or credential")
        if not 1 <= concurrency <= 8:
            raise ValueError("Invalid runner concurrency")
        self.manager, self.socket_path, self.state_directory = manager, socket_path, state_directory
        self.token, self.socket_gid, self.concurrency = token, socket_gid, concurrency
        self.capacity_check = capacity_check
        if project_volumes is not None and (project_volumes.instance_id != manager.namespace
                or project_volumes.root != (state_directory / 'project-volumes').resolve()):
            raise ValueError("Project volumes must belong to this runner's private state")
        self.project_volumes = project_volumes
        manager.project_workspaces = None
        if project_volumes is not None:
            from .project_workspaces import ProjectWorkspaces
            manager.project_workspaces = ProjectWorkspaces(manager, project_volumes)
        self.server = self.receipts = self.lock = None
        self.inflight: dict[str, tuple[str, asyncio.Task]] = {}
        self.connections: set[asyncio.Task] = set()

    async def start(self):
        self.state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_directory.is_symlink() or self.state_directory.stat().st_uid != os.geteuid() or self.state_directory.stat().st_mode & 0o077:
            raise ValueError("Runner state must be private")
        self.lock = file_lock(self.state_directory / ".runner.lock", timeout=0.1)
        try:
            self.lock.__enter__()
        except BaseException:
            self.lock = None
            raise
        try:
            parent = self.socket_path.parent
            parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            if parent.is_symlink() or parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o027:
                raise ValueError("Runner socket directory is not protected")
            if self.socket_gid is not None:
                os.chown(parent, -1, self.socket_gid)
            if self.socket_path.exists() or self.socket_path.is_symlink():
                info = self.socket_path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                    raise ValueError("Refusing to replace a non-runner socket")
                try:
                    _, writer = await asyncio.open_unix_connection(self.socket_path)
                except ConnectionRefusedError:
                    self.socket_path.unlink()
                else:
                    writer.close()
                    await writer.wait_closed()
                    raise ValueError("A runner is already listening")
            self.receipts = Receipts(self.state_directory)
            self.server = await asyncio.start_unix_server(self.handle, path=self.socket_path, limit=REQUEST_LIMIT)
            os.chmod(self.socket_path, 0o660)
            if self.socket_gid is not None:
                os.chown(self.socket_path, -1, self.socket_gid)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        server, self.server = self.server, None
        if server:
            server.close()
        tasks = [task for _, task in self.inflight.values()] + list(self.connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            await asyncio.wait_for(server.wait_closed(), timeout=10)
            self.socket_path.unlink(missing_ok=True)
        if self.receipts:
            self.receipts.close()
            self.receipts = None
        if self.lock:
            self.lock.__exit__(None, None, None)
            self.lock = None

    @staticmethod
    def failure(identifier: str, message: str, *, uncertain: bool = False) -> dict:
        return {"schema": VERSION, "id": identifier, "ok": False, "error": message,
                "uncertain": uncertain, "payload_size": 0}

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self.connections) >= self.concurrency * 2:
            writer.close()
            await writer.wait_closed()
            return
        self.connections.add(task)
        identifier = ""
        try:
            request = await read_header(reader, limit=REQUEST_LIMIT)
            candidate = request.get("id")
            if isinstance(candidate, str) and ID_PATTERN.fullmatch(candidate):
                identifier = candidate
            supplied = request.get("token")
            if not isinstance(supplied, str) or not secrets.compare_digest(supplied.encode(), self.token.encode()):
                raise ProtocolError("沙盒通道身份验证失败。")
            method, args = validate_request(request, file_limit=self.manager.max_file_bytes)
            payload = await asyncio.wait_for(reader.readexactly(request["payload_size"]), timeout=30)
            digest = fingerprint(request, payload)
            if identifier in self.inflight:
                previous, operation = self.inflight[identifier]
                if previous != digest:
                    raise ProtocolError("请求编号已用于不同的沙盒操作。")
            else:
                if method in MUTATIONS:
                    previous = self.receipts.lookup(identifier, digest)
                    if previous is not None:
                        await write_frame(writer, previous)
                        return
                if len(self.inflight) >= self.concurrency and method != "cancel":
                    raise ProtocolError("本实例沙盒通道正忙，请稍后重试。")
                if method in MUTATIONS:
                    self.receipts.expire()
                    self.receipts.begin(identifier, digest)

                async def eligible():
                    nonce = secrets.token_hex(16)
                    await write_frame(writer, {"schema": VERSION, "id": identifier,
                        "callback": "eligible", "nonce": nonce, "payload_size": 0})
                    answer = await read_header(reader, limit=1024, timeout=10)
                    return (set(answer) == {"schema", "id", "nonce", "eligible", "payload_size"}
                        and answer["schema"] == VERSION and answer["id"] == identifier and answer["nonce"] == nonce
                        and answer["eligible"] is True and answer["payload_size"] == 0)

                operation = asyncio.create_task(self.perform(identifier, method, args, payload, eligible), name=f"sandbox:{method}")
                self.inflight[identifier] = (digest, operation)
                operation.add_done_callback(lambda done, key=identifier: self.inflight.pop(key, None))
            response, output = await asyncio.shield(operation)
            await write_frame(writer, response, output)
        except SandboxError as error:
            try:
                await write_frame(writer, self.failure(identifier, str(error)))
            except (OSError, asyncio.TimeoutError):
                pass
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception:
            try:
                await write_frame(writer, self.failure(identifier, "沙盒通道状态未确认，请先检查实例。", uncertain=True))
            except (OSError, asyncio.TimeoutError):
                pass
        finally:
            self.connections.discard(task)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=5)
            except (OSError, asyncio.TimeoutError):
                pass

    async def perform(self, identifier, method, args, payload, eligible):
        output = b""
        try:
            if self.capacity_check is not None and method in {"create", "ensure_default", "exec", "write_file", "install_readonly_file", "start_owned",
                                                              "project_volume_create", "project_volume_mount", "project_volume_resize",
                                                              "project_workspace_start"}:
                from src.storage_admission import CapacityError, MIB
                try:
                    self.capacity_check(max(len(payload), 256 * MIB))
                except CapacityError as error:
                    raise SandboxError(str(error)) from None
            if method == "health":
                data = {"channel_ready": True, "instance_id": self.manager.namespace,
                        "image": self.manager.image,
                        "max_file_bytes": self.manager.max_file_bytes, "active_operations": len(self.inflight)}
                data['project_volumes_enabled'] = self.project_volumes is not None
                data['project_workspaces_enabled'] = self.manager.project_workspaces is not None
            elif method.startswith('project_rebuild_'):
                if self.manager.project_workspaces is None:
                    raise ProtocolError("此沙盒通道尚未配置项目重建。")
                from .project_dependencies import ProjectDependencies
                data = await ProjectDependencies(self.manager.project_workspaces).operate(
                    method.removeprefix('project_rebuild_'), **args)
            elif method.startswith('project_files_'):
                if self.manager.project_workspaces is None:
                    raise ProtocolError("此沙盒通道尚未配置项目文件维护。")
                parameters = dict(args)
                if method in {'project_files_restore_manifest', 'project_files_restore_file'}:
                    parameters['payload'] = payload
                if method == 'project_files_read':
                    parameters['max_bytes'] = min(parameters.get('max_bytes', 1024 * 1024), self.manager.max_file_bytes)
                if self.capacity_check is not None and method in {'project_files_snapshot', 'project_files_restore_begin',
                                                                 'project_files_restore_manifest', 'project_files_restore_file'}:
                    self.capacity_check(max(len(payload), 64 * 1024 * 1024))
                data, output = await self.manager.project_workspaces.files(method.removeprefix('project_files_'), **parameters)
            elif method.startswith('project_workspace_'):
                if self.manager.project_workspaces is None:
                    raise ProtocolError("此沙盒通道尚未配置项目工作区。")
                operation = method.removeprefix('project_workspace_')
                if operation == 'status':
                    data = await self.manager.project_workspaces.status(**args)
                else:
                    data = await self.manager.project_workspaces.operate(operation, **args)
            elif method.startswith('project_volume_'):
                if self.project_volumes is None:
                    raise ProtocolError("此沙盒通道尚未配置项目配额卷。")
                operation = method.removeprefix('project_volume_')
                if operation == 'create':
                    function, parameters = self.project_volumes.create, args
                elif operation == 'status':
                    function, parameters = self.project_volumes.status, args
                else:
                    function, parameters = self.project_volumes.mutate, {**args, 'operation': operation}
                worker = asyncio.create_task(asyncio.to_thread(function, **parameters))
                try:
                    data = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # A filesystem utility must finish or time out before this
                    # runner releases its lock. Cancellation is not rollback.
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            continue
                    worker.result()
                    raise
            elif method == "cancel":
                active = self.inflight.get(args["request_id"])
                if active:
                    operation = active[1]
                    if operation.get_name() == "sandbox:cancel":
                        raise ProtocolError("取消请求不能作为新的取消目标。")
                    operation.cancel()
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        pass
                data = {"cancel_requested": bool(active), "request_id": args["request_id"], "state": "unconfirmed"}
            else:
                # Never dispatch arbitrary attributes, Docker arguments, mounts,
                # images or host commands from the request.
                handlers = {name: getattr(self.manager, name) for name in (
                    "create", "ensure_default", "list", "admin_snapshot", "destroy", "exec",
                    "search_nix_packages", "write_file", "install_readonly_file", "export_artifact",
                    "read_file", "start_owned", "stop_owned", "reclaim_stopped")}
                parameters = dict(args)
                if method in {"write_file", "install_readonly_file"}:
                    parameters["content"] = payload
                if method == "reclaim_stopped":
                    parameters["eligible"] = eligible
                result = await handlers[method](**parameters)
                if method == "exec":
                    data = asdict(result)
                elif method == "read_file":
                    data, output = None, result
                elif method == "export_artifact":
                    output, directory = result
                    data = {"directory": directory}
                else:
                    data = result
            if len(output) > self.manager.max_file_bytes:
                raise ProtocolError("输出文件超过本实例传输上限。")
            response = {"schema": VERSION, "id": identifier, "ok": True, "data": data, "payload_size": len(output)}
            if method in MUTATIONS:
                self.receipts.finish(identifier, response)
            return response, output
        except (SandboxError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as error:
            message = str(error) if isinstance(error, SandboxError) else "沙盒操作未能完整确认，请核对当前状态。"
            response = self.failure(identifier, message)
            if method in MUTATIONS:
                self.receipts.finish(identifier, response)
            return response, b""
        except asyncio.CancelledError:
            # Leave a running receipt. A new runner marks it uncertain rather
            # than assuming Docker side effects were rolled back.
            raise
        except Exception:
            return self.failure(identifier, "沙盒执行结果未确认，请先检查当前状态。", uncertain=True), b""


def manager_from_config(config: dict) -> DockerSandboxManager:
    if set(config) - {"capacity_file"} != {"schema", "instance_id", "socket_path", "state_directory", "token", "docker", "docker_host", "image", "limits"} or config["schema"] != VERSION:
        raise ValueError("Unsupported runner configuration")
    if "capacity_file" in config and config["capacity_file"] != str(Path(config["state_directory"]).parent.parent / "capacity/status.json"):
        raise ValueError("Invalid runner capacity state")
    limits = config["limits"]
    if not isinstance(limits, dict) or set(limits) != {"max_per_owner", "max_total", "memory_mib", "cpus", "pids_limit", "max_file_bytes"}:
        raise ValueError("Invalid runner limits")
    if any(type(limits[name]) is not int for name in limits) or not 1 <= limits["max_per_owner"] <= limits["max_total"] <= 16:
        raise ValueError("Invalid runner capacity")
    return DockerSandboxManager(namespace=config["instance_id"], image=config["image"],
        docker_executable=config["docker"], docker_host=config["docker_host"], **limits)


async def serve(config: dict):
    manager = manager_from_config(config)
    from src.storage_admission import require_capacity
    runner = SandboxRunner(manager, socket_path=Path(config["socket_path"]),
        state_directory=Path(config["state_directory"]), token=config["token"], socket_gid=10001,
        capacity_check=(lambda size: require_capacity(size, filename=Path(config["capacity_file"]), instance_id=config["instance_id"])) if config.get("capacity_file") else None)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for value in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(value, stop.set)
    try:
        await runner.start()
        await stop.wait()
    finally:
        await runner.close()


def load_config(filename: Path) -> dict:
    info = filename.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise ValueError("Runner configuration must be private")
    config = read_json(filename)
    manager_from_config(config)
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(load_config(args.config)))


if __name__ == "__main__":
    main()
