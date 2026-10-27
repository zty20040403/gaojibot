"""Docker sandbox driver, independent of NoneBot and application credentials."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import secrets
import shlex
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Awaitable, Callable

from .errors import SandboxError as SandboxError


SANDBOX_ID_PATTERN = re.compile(r"^s[0-9a-f]{6}$")
NIX_ATTRIBUTE_PATTERN = re.compile(
    r"^[A-Za-z0-9_+\-]+(?:\.[A-Za-z0-9_+\-]+)*$"
)
RUNTIME_IMAGES = {
    "python": "python:3.12-slim",
    "node": "node:22-slim",
    "debian": "debian:bookworm-slim",
}


@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    manifest: "SandboxObservedManifest | None" = None


@dataclass(frozen=True)
class SandboxObservedManifest:
    command: str
    duration_ms: int
    stdout_sha256: str
    stdout_bytes: int
    stderr_sha256: str
    stderr_bytes: int
    changed_workspace_paths: tuple[str, ...]
    container_diff: tuple[str, ...]
    network_mode: str


@dataclass(frozen=True)
class SandboxExecutionActivity:
    activity_id: str
    sandbox_id: str
    owner: str
    command: str
    started_at: int
    status: str = "running"
    finished_at: int | None = None
    returncode: int | None = None
    error: str = ""


class DockerSandboxManager:
    backend = "oci"

    def __init__(
        self,
        *,
        image: str = "",
        max_per_owner: int = 2,
        max_total: int = 8,
        default_timeout_seconds: int = 120,
        max_output_chars: int = 12000,
        max_file_bytes: int = 20 * 1024 * 1024,
        nix_cache_volume: str = "gaoji-nix-v2",
        max_packages_per_exec: int = 16,
        namespace: str = "",
        docker_executable: str = "docker",
        docker_host: str | None = None,
        memory_mib: int = 8192,
        cpus: int | None = None,
        pids_limit: int | None = None,
        network: str = "isolated",
    ) -> None:
        if namespace and re.fullmatch(r"[a-f0-9]{32}", namespace) is None:
            raise ValueError("Sandbox namespace must be an instance ID")
        if namespace and not re.fullmatch(
            r"(?:gaoji-app-sandbox:[a-f0-9]{64}|[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64})", image
        ):
            raise ValueError("An instance sandbox requires a pinned image")
        if docker_executable != "docker" and not PurePosixPath(docker_executable).is_absolute():
            raise ValueError("Docker executable must be absolute")
        if docker_host is not None and not re.fullmatch(r"unix:///[^\x00-\x20]+", docker_host):
            raise ValueError("Sandbox driver requires a local Docker socket")
        if type(memory_mib) is not int or not 256 <= memory_mib <= 8192:
            raise ValueError("Sandbox memory must be between 256 and 8192 MiB")
        self.namespace = namespace
        if network not in {"isolated", "none", "bridge"}:
            raise ValueError("Sandbox network must be isolated, none or bridge")
        self.network = network
        self.docker_executable = docker_executable
        self.docker_host = docker_host or ("unix:///var/run/docker.sock" if namespace else None)
        self.memory_mib = memory_mib
        self.cpus = 2 if cpus is None else cpus
        self.pids_limit = 256 if pids_limit is None else pids_limit
        if self.cpus is not None and (type(self.cpus) is not int or not 1 <= self.cpus <= 16):
            raise ValueError("Sandbox CPUs must be between 1 and 16")
        if self.pids_limit is not None and (type(self.pids_limit) is not int or not 32 <= self.pids_limit <= 4096):
            raise ValueError("Sandbox process limit must be between 32 and 4096")
        self.image = image.strip()
        self.max_per_owner = max(1, max_per_owner)
        self.max_total = max(1, max_total)
        self.default_timeout_seconds = max(5, default_timeout_seconds)
        self.max_output_chars = max(1000, max_output_chars)
        self.max_file_bytes = max(0, int(max_file_bytes))
        if namespace and not 1024 <= self.max_file_bytes <= 256 * 1024 * 1024:
            raise ValueError("Instance file transfers must have a bounded size (1 KiB to 256 MiB)")
        self.nix_cache_volume = (f"gaoji-app-{namespace}-nix" if namespace
                                 else nix_cache_volume.strip() or "gaoji-nix-v2")
        self.max_packages_per_exec = min(max(int(max_packages_per_exec), 1), 32)
        self._active_execs: dict[str, SandboxExecutionActivity] = {}
        self._last_execs: dict[str, SandboxExecutionActivity] = {}
        self._owner_create_locks: dict[str, asyncio.Lock] = {}
        self._exec_locks: dict[str, asyncio.Lock] = {}
        self._nix_volume_lock = asyncio.Lock()
        self._nix_volume_ready = False
        self._allocation_lock = asyncio.Lock()
        self._helper_lock = asyncio.Lock()
        self.project_workspaces = None

    @property
    def _sandbox_label(self) -> str:
        return "gaoji.app.sandbox" if self.namespace else "qqbot.sandbox"

    def _namespace_filters(self) -> tuple[str, ...]:
        if not self.namespace:
            return ()
        return ("--filter", f"label=gaoji.app.instance={self.namespace}",
                "--filter", "label=gaoji.app.sandbox=true")

    def _resource_labels(self, kind: str) -> tuple[str, ...]:
        if not self.namespace:
            return ()
        return ("--label", f"gaoji.app.instance={self.namespace}", "--label", f"gaoji.app.resource={kind}")

    async def create(
        self,
        owner: str,
        runtime: str = "python",
        *,
        purpose: str = "task",
    ) -> dict[str, str]:
        # Reserve capacity across owners, including concurrent Sub-Agent starts.
        async with self._allocation_lock:
            return await self._create(owner, runtime, purpose=purpose)

    async def _create(self, owner: str, runtime: str, *, purpose: str, project=None) -> dict[str, str]:
        if purpose == "project" and project is None:
            raise SandboxError("项目工作区必须通过项目生命周期创建。")
        if self.namespace and runtime not in RUNTIME_IMAGES:
            raise SandboxError("不支持的运行环境。")
        image = project["workspace"]["image"] if project else self.image or RUNTIME_IMAGES.get(runtime)
        if image is None:
            raise SandboxError("不支持的运行环境。")
        normalized_purpose = "project" if project else "shell" if purpose == "shell" else "task"

        owner_hash = self._owner_hash(owner)
        owner_sandboxes = await self.list(owner)
        owner_running = [
            item for item in owner_sandboxes if self._status_is_running(item)
        ]
        if len(owner_running) >= self.max_per_owner:
            raise SandboxError(
                f"你最多同时运行 {self.max_per_owner} 个沙盒，"
                "请等待当前任务结束或让管理员回收旧工作区。"
            )
        all_sandboxes = await self._list_by_label(f"{self._sandbox_label}=true")
        if sum(self._status_is_running(item) for item in all_sandboxes) >= self.max_total:
            raise SandboxError("机器人沙盒总数已达到上限。")

        sandbox_id = project["workspace"]["sandbox_id"] if project else "s" + secrets.token_hex(3)
        name = self._container_name(sandbox_id)
        workspace_volume = self._workspace_volume_name(sandbox_id)
        if self.image:
            await self._ensure_nix_volume(image)
            if project is None:
                await self._prepare_workspace_volume(image, workspace_volume, owner=owner, sandbox_id=sandbox_id)
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{self._sandbox_label}=true",
            *self._resource_labels("sandbox"),
            "--label",
            f"qqbot.owner={owner_hash}",
            "--label",
            f"qqbot.owner_ref={self._owner_ref(owner)}",
            "--label",
            f"qqbot.id={sandbox_id}",
            "--label",
            f"qqbot.runtime={runtime}",
            "--label",
            f"qqbot.purpose={normalized_purpose}",
            "--memory",
            self._memory_limit(),
            "--memory-swap",
            self._memory_limit(),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m",
            "--workdir",
            "/workspace",
            "--restart",
            "no",
        ]
        if self.image:
            command.extend(
                [
                    "--pull=never",
                    "--read-only",
                    "--user",
                    "1000:1000",
                    "--env",
                    "HOME=/home/sandbox",
                    "--env",
                    "USER=sandbox",
                    "--tmpfs",
                    "/home/sandbox:rw,nosuid,nodev,size=256m,uid=1000,gid=1000,mode=700",
                    "--mount",
                    (self.project_workspaces.mount_spec(project) if project else
                     f"type=volume,source={workspace_volume},target=/workspace"),
                    "--mount",
                    (
                        "type=volume,source="
                        f"{self.nix_cache_volume},target=/nix,readonly"
                    ),
                ]
            )
        if project:
            command.extend(("--entrypoint", "sh"))
            for key, value in self.project_workspaces.labels(project).items():
                command.extend(("--label", f"{key}={value}"))
        command.extend(
            [
                image,
                *([] if project else ["sh"]),
                "-lc",
                (self.project_workspaces.startup_command(project) if project else
                 "mkdir -p /workspace && exec sleep infinity"),
            ]
        )
        result = await self._run(*command, timeout=300)
        if result.returncode != 0:
            if project is not None:
                raise SandboxError("Project container creation was not confirmed; reconcile its existing operation")
            if self.image and project is None:
                await self._remove_volume(workspace_volume, owner=owner, sandbox_id=sandbox_id)
            detail = result.stderr or result.stdout
            raise SandboxError(self._docker_error(detail))
        return {
            "sandbox_id": sandbox_id,
            "runtime": runtime,
            "image": image,
            "status": "running",
            "purpose": normalized_purpose,
            "toolset": (
                "advanced"
                if self.image
                else f"minimal-{runtime}"
            ),
        }

    async def ensure_default(
        self,
        owner: str,
        runtime: str = "debian",
    ) -> dict[str, str]:
        """Return the owner's reusable shell sandbox, creating it if needed."""
        owner_key = self._owner_hash(owner)
        lock = self._owner_create_locks.setdefault(owner_key, asyncio.Lock())
        async with lock:
            sandboxes = sorted(
                await self.list(owner),
                key=lambda item: str(item.get("sandbox_id") or ""),
            )
            shell_sandboxes = [
                item for item in sandboxes if item.get("purpose") == "shell"
            ]
            if shell_sandboxes:
                selected = shell_sandboxes[0]
                sandbox_id = str(selected["sandbox_id"])
                if not str(selected.get("status") or "").lower().startswith("up "):
                    await self.start_owned(owner, sandbox_id)
                return {
                    **selected,
                    "status": "running",
                    "purpose": "shell",
                    "toolset": "advanced" if self.image else f"minimal-{runtime}",
                }
            return await self.create(owner, runtime, purpose="shell")

    async def list(self, owner: str) -> list[dict[str, str]]:
        return await self._list_by_label(
            f"qqbot.owner={self._owner_hash(owner)}"
        )

    async def admin_snapshot(self) -> dict[str, object]:
        result = await self._run(
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={self._sandbox_label}=true",
            *self._namespace_filters(),
            "--format",
            (
                '{{.Names}}|{{.Label "qqbot.id"}}|'
                '{{.Label "qqbot.runtime"}}|{{.Label "qqbot.owner_ref"}}|'
                '{{.Label "qqbot.owner"}}|{{.Label "qqbot.purpose"}}|'
                '{{.Status}}'
            ),
            timeout=20,
        )
        if result.returncode != 0:
            raise SandboxError(self._docker_error(result.stderr))

        containers: list[dict[str, object]] = []
        running_names: list[str] = []
        for line in result.stdout.splitlines():
            parts = (line.split("|", 6) + [""] * 7)[:7]
            name, sandbox_id, runtime, owner_ref, owner_hash, purpose, status = parts
            if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
                continue
            if self.namespace and name != self._container_name(sandbox_id):
                continue
            running = status.lower().startswith("up ")
            if running:
                running_names.append(name)
            containers.append(
                {
                    "sandbox_id": sandbox_id,
                    "name": name,
                    "runtime": runtime,
                    "purpose": purpose or "task",
                    "owner": owner_ref,
                    "owner_hash": owner_hash,
                    "status": status,
                    "running": running,
                    "cpu_percent": "-",
                    "memory_usage": "-",
                    "memory_percent": "-",
                    "workspace_size_bytes": 0,
                    "workspace_file_count": 0,
                    "workspace_files": [],
                    "workspace_error": "",
                }
            )

        stats = await self._container_stats(running_names)
        now = int(time.time())
        active_by_sandbox: dict[str, list[dict[str, object]]] = {}
        for activity in self._active_execs.values():
            active_by_sandbox.setdefault(activity.sandbox_id, []).append(
                self._activity_payload(activity, now=now)
            )

        for container in containers:
            name = str(container["name"])
            sandbox_id = str(container["sandbox_id"])
            container.update(stats.get(name, {}))
            container["activities"] = active_by_sandbox.get(sandbox_id, [])
            last_activity = self._last_execs.get(sandbox_id)
            container["last_activity"] = (
                self._activity_payload(last_activity, now=now)
                if last_activity is not None
                else None
            )
            if bool(container["running"]):
                container.update(await self._workspace_inventory(name))

        return {
            "items": containers,
            "active_commands": len(self._active_execs),
            "backend": self.backend,
        }

    async def destroy(self, owner: str, sandbox_id: str) -> None:
        self._ensure_idle(sandbox_id)
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            self._ensure_idle(sandbox_id)
            name = await self._owned_container(
                owner,
                sandbox_id,
                require_running=False,
                allow_project=False,
            )
            workspace_volume = None
            if self.image:
                mounted = await self._run(
                    "docker", "inspect", "--format", "{{json .Mounts}}", name,
                    timeout=15,
                )
                if mounted.returncode != 0:
                    raise SandboxError(self._docker_error(mounted.stderr))
                # Read the mount before removal so pre-rename workspaces are not orphaned.
                for mount in json.loads(mounted.stdout):
                    if mount.get("Destination") == "/workspace" and mount.get("Type") == "volume":
                        candidate = mount.get("Name")
                        if candidate not in self._workspace_volumes(sandbox_id):
                            raise SandboxError("工作区卷不属于当前沙盒，已取消销毁。")
                        if self.namespace:
                            await self._check_volume(candidate, kind="workspace", owner=owner, sandbox_id=sandbox_id)
                        workspace_volume = candidate
            result = await self._run(
                "docker",
                "rm",
                "-f",
                name,
                timeout=30,
            )
            if result.returncode != 0:
                raise SandboxError(self._docker_error(result.stderr))
            if workspace_volume:
                await self._remove_volume(workspace_volume, owner=owner, sandbox_id=sandbox_id)
        self._exec_locks.pop(sandbox_id, None)

    async def reclaim_stopped(self, owner: str, sandbox_id: str, *, eligible: Callable[[], bool | Awaitable[bool]],
                              not_used_since: int | None = None) -> None:
        """Reclaim an expired host-issued ticket, without killing running work."""
        if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
            raise SandboxError("沙盒 ID 格式错误。")
        self._ensure_idle(sandbox_id)
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            self._ensure_idle(sandbox_id)
            name = self._container_name(sandbox_id)
            result = await self._run("docker", "inspect", "--format",
                "[{{json .Config.Labels}},{{json .State.Running}},{{json .Mounts}},{{json .State.StartedAt}}]", name, timeout=20)
            allowed_volumes = self._workspace_volumes(sandbox_id)
            volumes = []
            if result.returncode:
                detail = (result.stderr or result.stdout).lower()
                if "no such object" not in detail and "no such container" not in detail:
                    raise SandboxError(self._docker_error(result.stderr or result.stdout))
                # Resume a cleanup interrupted after container removal but before volume removal.
                volumes = sorted(allowed_volumes)
            else:
                labels, running, mounts, started_at = json.loads(result.stdout)
                if (labels.get(self._sandbox_label) != "true" or labels.get("qqbot.id") != sandbox_id
                        or labels.get("qqbot.owner") != self._owner_hash(owner)
                        or (self.namespace and labels.get("gaoji.app.instance") != self.namespace)
                        or labels.get("qqbot.purpose", "task") != "task"):
                    raise SandboxError("沙盒不属于到期任务，已跳过自动回收。")
                if running:
                    raise SandboxError("沙盒已重新运行，已跳过自动回收。")
                if not_used_since is not None and datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp() > not_used_since:
                    raise SandboxError("沙盒在交付后被重新使用，已保留新的工作内容。")
                for mount in mounts:
                    if mount.get("Destination") == "/workspace" and mount.get("Type") == "volume":
                        if mount.get("Name") not in allowed_volumes:
                            raise SandboxError("工作区卷不属于当前沙盒，已跳过自动回收。")
                        if self.namespace:
                            await self._check_volume(mount["Name"], kind="workspace", owner=owner, sandbox_id=sandbox_id)
                        volumes.append(mount["Name"])
                if not await self._eligible(eligible):
                    raise SandboxError("任务状态已变化，已取消自动回收。")
                removed = await self._run("docker", "rm", name, timeout=30)
                if removed.returncode:
                    raise SandboxError(self._docker_error(removed.stderr or removed.stdout))
            for volume in volumes:
                if not await self._eligible(eligible):
                    raise SandboxError("任务状态已变化，已保留工作区。")
                await self._remove_volume(volume, owner=owner, sandbox_id=sandbox_id)
            self._last_execs.pop(sandbox_id, None)

    @staticmethod
    async def _eligible(check: Callable[[], bool | Awaitable[bool]]) -> bool:
        value = check()
        if inspect.isawaitable(value):
            value = await value
        return value is True

    @asynccontextmanager
    async def _project_write_guard(self, owner, sandbox_id, **options):
        if self.project_workspaces is None:
            yield
        else:
            async with self.project_workspaces.write_guard(owner, sandbox_id, **options):
                yield

    async def exec(
        self,
        owner: str,
        sandbox_id: str,
        command: str,
        timeout_seconds: int | None = None,
        packages: list[str] | tuple[str, ...] | None = None,
    ) -> SandboxResult:
        if not command.strip():
            raise SandboxError("命令不能为空。")
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock, self._project_write_guard(owner, sandbox_id, dependencies=bool(packages)):
            name = await self._owned_container(owner, sandbox_id)
            timeout = min(
                max(timeout_seconds or self.default_timeout_seconds, 1),
                300,
            )
            store_paths = await self._prepare_packages(
                packages or (),
                timeout_seconds=max(timeout, 120),
            )
            wrapped_command = self._wrap_packages(store_paths, command)
            activity_id = secrets.token_hex(8)
            activity = SandboxExecutionActivity(
                activity_id=activity_id,
                sandbox_id=sandbox_id,
                owner=self._owner_ref(owner),
                command=command[:2000],
                started_at=int(time.time()),
            )
            self._active_execs[activity_id] = activity
            marker = f"/tmp/qqbot-observe-{secrets.token_hex(8)}"
            marker_ready = False
            try:
                network_mode = await self._network_mode(name)
                marker_result = await self._run(
                    "docker", "exec", name, "touch", marker, timeout=10
                )
                marker_ready = marker_result.returncode == 0
                started = time.monotonic()
                stdout_bytes, stderr_bytes, returncode = await self._run_bytes(
                    "docker",
                    "exec",
                    name,
                    "timeout",
                    "--signal=TERM",
                    "--kill-after=5s",
                    "--preserve-status",
                    str(timeout),
                    "sh",
                    "-lc",
                    wrapped_command,
                    timeout=timeout + 10,
                )
                duration_ms = max(int((time.monotonic() - started) * 1000), 0)
                changed_paths = (
                    await self._changed_workspace_paths(name, marker)
                    if marker_ready
                    else ()
                )
                container_diff = await self._container_diff(name, marker)
                result = SandboxResult(
                    stdout=self._trim_output(
                        stdout_bytes.decode("utf-8", errors="replace")
                    ),
                    stderr=self._trim_output(
                        stderr_bytes.decode("utf-8", errors="replace")
                    ),
                    returncode=returncode,
                    manifest=SandboxObservedManifest(
                        command=command,
                        duration_ms=duration_ms,
                        stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
                        stdout_bytes=len(stdout_bytes),
                        stderr_sha256=hashlib.sha256(stderr_bytes).hexdigest(),
                        stderr_bytes=len(stderr_bytes),
                        changed_workspace_paths=changed_paths,
                        container_diff=container_diff,
                        network_mode=network_mode,
                    ),
                )
            except asyncio.CancelledError:
                self._remember_exec(activity, status="cancelled")
                raise
            except Exception as exc:
                self._remember_exec(activity, status="failed", error=str(exc))
                raise
            else:
                self._remember_exec(
                    activity,
                    status="completed" if result.returncode == 0 else "failed",
                    returncode=result.returncode,
                )
                return result
            finally:
                self._active_execs.pop(activity_id, None)
                if marker_ready:
                    await self._remove_observation_marker(name, marker)

    async def search_nix_packages(self, query: str) -> list[dict[str, str]]:
        if not self.image:
            raise SandboxError("当前不是 Nix 高级沙盒，无法搜索按需软件包。")
        normalized = " ".join(query.split()).strip()
        if not normalized or len(normalized) > 80:
            raise SandboxError("软件包搜索词必须为 1 到 80 个字符。")
        await self._ensure_nix_volume(self.image)
        result = await self._run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            self._memory_limit(2048),
            "--memory-swap",
            self._memory_limit(2048),
            "--mount",
            f"type=volume,source={self.nix_cache_volume},target=/nix",
            self.image,
            "nix-env",
            "-qaP",
            "-f",
            "<nixpkgs>",
            "--description",
            f".*{normalized}.*",
            timeout=120,
        )
        if result.returncode != 0:
            raise SandboxError(
                "Nix 软件包搜索失败：" + self._docker_error(result.stderr)
            )
        matches: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) < 2:
                continue
            matches.append(
                {
                    "attribute": fields[0],
                    "package": fields[1],
                    "description": fields[2] if len(fields) > 2 else "",
                }
            )
            if len(matches) >= 30:
                break
        return matches

    async def _ensure_nix_volume(self, image: str) -> None:
        if self._nix_volume_ready:
            return
        async with self._nix_volume_lock:
            if self._nix_volume_ready:
                return
            created = await self._create_volume(self.nix_cache_volume, kind="nix-cache")
            if created.returncode != 0:
                raise SandboxError(
                    "Nix 共享缓存卷创建失败："
                    + self._docker_error(created.stderr or created.stdout)
                )
            result = await self._run(
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                self._memory_limit(512),
                "--memory-swap",
                self._memory_limit(512),
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs",
                "/root:rw,nosuid,nodev,size=32m,mode=700",
                "--mount",
                f"type=volume,source={self.nix_cache_volume},target=/cache/nix",
                image,
                "gaoji-cache-seed",
                timeout=300,
            )
            if result.returncode != 0:
                raise SandboxError(
                    f"Nix 共享缓存初始化失败（退出码 {result.returncode}）："
                    + self._docker_error(result.stderr or result.stdout)
                )
            self._nix_volume_ready = True

    async def _prepare_workspace_volume(self, image: str, volume: str, *, owner: str, sandbox_id: str) -> None:
        created = await self._create_volume(volume, kind="workspace", owner=owner, sandbox_id=sandbox_id)
        if created.returncode != 0:
            raise SandboxError(
                "沙盒工作卷创建失败："
                + self._docker_error(created.stderr or created.stdout)
            )
        result = await self._run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--read-only",
            "--mount",
            f"type=volume,source={volume},target=/workspace",
            image,
            "sh",
            "-lc",
            "chmod 700 /workspace && chown 1000:1000 /workspace",
            timeout=120,
        )
        if result.returncode != 0:
            try:
                await self._remove_volume(volume, owner=owner, sandbox_id=sandbox_id)
            except SandboxError:
                pass
            raise SandboxError(
                "沙盒工作卷初始化失败："
                + self._docker_error(result.stderr or result.stdout)
            )

    async def _remove_volume(self, volume: str, *, owner: str = "", sandbox_id: str = "") -> None:
        if self.namespace:
            if volume != self._workspace_volume_name(sandbox_id):
                raise SandboxError("工作区卷不属于本实例。")
            if not await self._check_volume(volume, kind="workspace", owner=owner, sandbox_id=sandbox_id):
                return
        result = await self._run(
            "docker",
            "volume",
            "rm",
            "-f",
            volume,
            timeout=30,
        )
        detail = result.stderr or result.stdout
        if result.returncode != 0 and "no such volume" not in detail.lower():
            raise SandboxError(self._docker_error(detail))

    def _volume_labels(self, kind: str, owner: str, sandbox_id: str) -> dict[str, str]:
        labels = {"gaoji.app.instance": self.namespace, "gaoji.app.resource": kind}
        if kind == "workspace":
            if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id) or not owner:
                raise SandboxError("工作区缺少所属任务。")
            labels.update({"qqbot.owner": self._owner_hash(owner), "qqbot.id": sandbox_id})
        return labels

    async def _check_volume(self, volume: str, *, kind: str, owner: str = "", sandbox_id: str = "") -> bool:
        expected = self._volume_labels(kind, owner, sandbox_id)
        result = await self._run("docker", "volume", "inspect", "--format", "{{json .Labels}}", volume, timeout=15)
        if result.returncode:
            if "no such volume" in (result.stderr or result.stdout).lower():
                return False
            raise SandboxError("无法核对工作卷归属，已停止操作。")
        try:
            labels = json.loads(result.stdout)
        except (ValueError, TypeError):
            labels = None
        if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected.items()):
            raise SandboxError("已有工作卷不属于本实例或当前任务，已停止操作。")
        return True

    async def _create_volume(self, volume: str, *, kind: str, owner: str = "", sandbox_id: str = "") -> SandboxResult:
        arguments: list[str] = []
        if self.namespace:
            if await self._check_volume(volume, kind=kind, owner=owner, sandbox_id=sandbox_id):
                if kind == "workspace":
                    raise SandboxError("新任务工作卷已存在，已停止创建，未覆盖原有文件。")
                return SandboxResult(volume, "", 0)
            for key, value in self._volume_labels(kind, owner, sandbox_id).items():
                arguments.extend(("--label", f"{key}={value}"))
        result = await self._run("docker", "volume", "create", *arguments, volume, timeout=30)
        if self.namespace and result.returncode == 0:
            if not await self._check_volume(volume, kind=kind, owner=owner, sandbox_id=sandbox_id):
                raise SandboxError("工作卷创建后不存在，已停止操作。")
        return result

    async def _prepare_packages(
        self,
        packages: list[str] | tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> tuple[str, ...]:
        unique = tuple(dict.fromkeys(str(item).strip() for item in packages))
        if not unique:
            return ()
        if not self.image:
            raise SandboxError("只有 Nix 高级沙盒支持按需软件包。")
        if len(unique) > self.max_packages_per_exec:
            raise SandboxError(
                f"一次最多请求 {self.max_packages_per_exec} 个 Nix 软件包。"
            )
        invalid = [
            item
            for item in unique
            if len(item) > 128 or NIX_ATTRIBUTE_PATTERN.fullmatch(item) is None
        ]
        if invalid:
            raise SandboxError(f"Nix 软件包属性名无效：{invalid[0]}")

        await self._ensure_nix_volume(self.image)
        expression = self._package_expression(unique)
        cache_key = hashlib.sha256("\0".join(unique).encode()).hexdigest()[:24]
        root = f"/nix/var/nix/gcroots/gaoji-packages/{cache_key}"
        timeout = min(max(int(timeout_seconds), 30), 300)
        script = (
            "set -eu; root=$1; expr=$2; rm -rf -- \"$root\"; "
            "mkdir -p -- \"$root\"; "
            f"paths=$(timeout --signal=TERM --kill-after=5s {timeout}s "
            "nix-build --no-out-link --expr \"$expr\"); "
            "i=0; for path in $paths; do "
            "case \"$path\" in /nix/store/*) ;; *) exit 71;; esac; "
            "ln -s \"$path\" \"$root/$i\"; i=$((i+1)); done; "
            "test \"$i\" -gt 0; touch \"$root\"; printf '%s\\n' \"$paths\""
        )
        result = await self._run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            self._network_name,
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            self._memory_limit(),
            "--memory-swap",
            self._memory_limit(),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=2g,mode=1777",
            "--tmpfs",
            "/root:rw,nosuid,nodev,size=128m,mode=700",
            "--mount",
            f"type=volume,source={self.nix_cache_volume},target=/nix",
            self.image,
            "sh",
            "-lc",
            script,
            "gaoji-package-helper",
            root,
            expression,
            timeout=timeout + 20,
        )
        if result.returncode != 0:
            raise SandboxError(
                "Nix 按需软件包准备失败："
                + self._docker_error(result.stderr or result.stdout)
            )
        paths = tuple(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("/nix/store/")
            and "/" not in line.strip().removeprefix("/nix/store/")
        )
        if not paths:
            raise SandboxError("Nix 没有返回可用的软件包路径。")
        return paths

    @classmethod
    def _package_expression(cls, packages: tuple[str, ...]) -> str:
        python: list[str] = []
        ordinary: list[str] = []
        for attribute in packages:
            prefix = "python3Packages."
            if attribute.startswith(prefix):
                python.append(attribute.removeprefix(prefix))
            else:
                ordinary.append(attribute)
        values = [cls._attribute_expression("pkgs", item) for item in ordinary]
        if python:
            python_values = " ".join(
                cls._attribute_expression("ps", item) for item in python
            )
            values.append(
                f"(pkgs.python3.withPackages (ps: [ {python_values} ]))"
            )
        return "let pkgs = import <nixpkgs> {}; in [ " + " ".join(values) + " ]"

    @staticmethod
    def _attribute_expression(root: str, attribute: str) -> str:
        expression = root
        for segment in attribute.split("."):
            expression = f'(builtins.getAttr "{segment}" {expression})'
        return expression

    @staticmethod
    def _wrap_packages(store_paths: tuple[str, ...], command: str) -> str:
        if not store_paths:
            return command
        package_path = ":".join(f"{path}/bin" for path in store_paths)
        return (
            f"export PATH={shlex.quote(package_path)}:\"$PATH\"; "
            f"exec sh -lc {shlex.quote(command)}"
        )

    async def write_file(
        self,
        owner: str,
        sandbox_id: str,
        path: str,
        content: bytes,
        *,
        allow_large: bool = False,
    ) -> int:
        async with self._exec_locks.setdefault(sandbox_id, asyncio.Lock()), self._project_write_guard(owner, sandbox_id, incoming=len(content)):
            return await self._write_file(owner, sandbox_id, path, content, allow_large=allow_large)

    async def _write_file(self, owner, sandbox_id, path, content, *, allow_large=False):
        limit = self._file_limit(
            None
            if allow_large or self.max_file_bytes == 0
            else 512 * 1024
        )
        if limit is not None and len(content) > limit:
            raise SandboxError(f"单次写入文件不能超过 {limit} 字节。")
        name = await self._owned_container(owner, sandbox_id)
        container_path = self._workspace_path(path)
        parent = str(PurePosixPath(container_path).parent)
        mkdir_result = await self._run(
            "docker",
            "exec",
            name,
            "mkdir",
            "-p",
            "--",
            parent,
            timeout=20,
        )
        if mkdir_result.returncode != 0:
            raise SandboxError(mkdir_result.stderr or "创建目录失败。")
        result = await self._run(
            "docker",
            "exec",
            "-i",
            name,
            "tee",
            container_path,
            input_bytes=content,
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError(result.stderr or "写入文件失败。")
        return len(content)

    async def install_readonly_file(self, owner: str, sandbox_id: str, path: str, content: bytes) -> None:
        async with self._exec_locks.setdefault(sandbox_id, asyncio.Lock()), self._project_write_guard(owner, sandbox_id, incoming=len(content)):
            await self._install_readonly_file(owner, sandbox_id, path, content)

    async def _install_readonly_file(self, owner, sandbox_id, path, content):
        name = await self._owned_container(owner, sandbox_id)
        target = self._workspace_path(path)
        if not target.startswith("/workspace/upstream/") or not self.image:
            raise SandboxError("只读交接需要非 root 的高级沙盒和 upstream 路径。")
        # Root owns both the file and its ancestors; uid 1000 cannot chmod or unlink the snapshot.
        script = """import os,stat,sys,uuid
content=sys.stdin.buffer.read()
parts=sys.argv[1].split('/')[2:]
fd=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
for part in parts[:-1]:
    try: os.mkdir(part,0o755,dir_fd=fd)
    except FileExistsError: pass
    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    os.close(fd); fd=child
    os.fchown(fd,0,0); os.fchmod(fd,0o755)
try:
    old=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
except FileNotFoundError:
    old=None
if old is not None:
    with os.fdopen(old,'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o222:
            raise ValueError('Untrusted existing artifact')
        if f.read()!=content: raise ValueError('Artifact content conflict')
else:
    temporary='.import-'+uuid.uuid4().hex
    try:
        out=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(out,'wb') as f:
            f.write(content); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(),0o444)
        os.replace(temporary,parts[-1],src_dir_fd=fd,dst_dir_fd=fd)
    finally:
        try: os.unlink(temporary,dir_fd=fd)
        except FileNotFoundError: pass
os.close(fd)
"""
        if self.namespace:
            project = (await self.project_workspaces.for_sandbox(owner, sandbox_id)
                       if self.project_workspaces else None)
            volume = self._workspace_volume_name(sandbox_id)
            if project is None and not await self._check_volume(volume, kind="workspace", owner=owner, sandbox_id=sandbox_id):
                raise SandboxError("工作区已不存在，无法导入只读产物。")
            if project:
                identity = project["workspace"]["root_identity"]
                script = ("import os\nroot=os.stat('/workspace')\n"
                          f"if str(root.st_dev)+':'+str(root.st_ino)!={identity!r}:\n"
                          "    raise RuntimeError('Project mount mismatch')\n" + script)
            # The running worker keeps zero capabilities. A short-lived helper
            # gets only this workspace, never the host filesystem or Docker socket.
            command = ("docker", "run", "--pull=never", "--rm", "-i", "--network", "none",
                "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
                "--security-opt", "no-new-privileges", "--read-only",
                "--memory", "256m", "--memory-swap", "256m",
                "--mount", (self.project_workspaces.mount_spec(project) if project else
                            f"type=volume,source={volume},target=/workspace"),
                "--mount", f"type=volume,source={self.nix_cache_volume},target=/nix,readonly",
                *(("--entrypoint", "python") if project else ()),
                (project["workspace"]["image"] if project else self.image),
                *(() if project else ("python",)), "-c", script, target)
        else:
            command = ("docker", "exec", "-i", "--user", "0", name, "python", "-c", script, target)
        result = await self._run(*command, input_bytes=content, timeout=60)
        if result.returncode:
            raise SandboxError("无法导入只读产物")

    async def export_artifact(self, owner: str, sandbox_id: str, path: str) -> tuple[bytes, bool]:
        """Export a file or a ZIP snapshot of a directory without following links."""
        async with self._exec_locks.setdefault(sandbox_id, asyncio.Lock()):
            return await self._export_artifact(owner, sandbox_id, path)

    async def _export_artifact(self, owner, sandbox_id, path):
        name = await self._owned_container(owner, sandbox_id)
        target = self._workspace_path(path)
        kind = await self._run("docker", "exec", name, "sh", "-lc",
            f"if [ -L {shlex.quote(target)} ]; then exit 1; "
            f"elif [ -f {shlex.quote(target)} ]; then printf file; "
            f"elif [ -d {shlex.quote(target)} ]; then printf directory; else exit 1; fi", timeout=20)
        if kind.returncode:
            raise SandboxError(f"产物不存在、不可读取或为符号链接：{path}")
        if kind.stdout.strip() == "file":
            return await self._read_file(owner, sandbox_id, path, max_bytes=None), False
        if kind.stdout.strip() != "directory":
            raise SandboxError(f"不支持的产物类型：{path}")
        script = """
import os, shutil, stat, sys, tempfile, zipfile
from pathlib import Path
root = Path(sys.argv[1])
if root.is_symlink() or not root.is_dir():
    raise ValueError('Artifact directory changed')
with tempfile.TemporaryFile() as output:
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            files.sort()
            directory = Path(current)
            archive.mkdir(directory.relative_to(root.parent).as_posix() + '/')
            for filename in dirs + files:
                entry = directory / filename
                mode = entry.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError('Cannot archive link or special file: ' + str(entry))
            for filename in files:
                entry = directory / filename
                archive.write(entry, entry.relative_to(root.parent).as_posix())
    output.seek(0)
    shutil.copyfileobj(output, sys.stdout.buffer)
"""
        content, error, code = await self._run_bytes(
            "docker", "exec", name, "python", "-c", script, target, timeout=120)
        if code:
            raise SandboxError(f"目录产物打包失败：{path}；{error.decode(errors='replace')[-500:]}")
        return content, True

    async def read_file(
        self,
        owner: str,
        sandbox_id: str,
        path: str,
        max_bytes: int | None = 64 * 1024,
    ) -> bytes:
        async with self._exec_locks.setdefault(sandbox_id, asyncio.Lock()):
            return await self._read_file(owner, sandbox_id, path, max_bytes)

    async def _read_file(self, owner, sandbox_id, path, max_bytes=64 * 1024):
        name = await self._owned_container(owner, sandbox_id)
        container_path = self._workspace_path(path)
        size_result = await self._run(
            "docker",
            "exec",
            name,
            "sh",
            "-lc",
            f"wc -c < {shlex.quote(container_path)}",
            timeout=20,
        )
        if size_result.returncode != 0:
            raise SandboxError("文件不存在或无法读取。")
        try:
            size = int(size_result.stdout.strip())
        except ValueError as exc:
            raise SandboxError("无法读取文件大小。") from exc
        limit = self._file_limit(max_bytes)
        if limit is not None and size > limit:
            raise SandboxError(f"文件大小 {size} 字节，超过读取上限 {limit}。")
        result = await self._run_bytes(
            "docker",
            "exec",
            name,
            "cat",
            "--",
            container_path,
            timeout=60,
        )
        if result[2] != 0:
            raise SandboxError(
                result[1].decode("utf-8", errors="replace") or "读取文件失败。"
            )
        return result[0]

    def _file_limit(self, requested: int | None) -> int | None:
        limits: list[int] = []
        if self.max_file_bytes > 0:
            limits.append(self.max_file_bytes)
        if requested is not None and requested > 0:
            limits.append(int(requested))
        return min(limits) if limits else None

    async def start_owned(self, owner: str, sandbox_id: str) -> None:
        async with self._allocation_lock:
            await self._start_owned(owner, sandbox_id)

    async def _start_owned(self, owner: str, sandbox_id: str) -> None:
        name = await self._owned_container(owner, sandbox_id, require_running=False, allow_project=False)
        owned = await self.list(owner)
        target = next(
            (
                item
                for item in owned
                if str(item.get("sandbox_id") or "") == sandbox_id
            ),
            None,
        )
        if target is not None and self._status_is_running(target):
            return
        if sum(self._status_is_running(item) for item in owned) >= self.max_per_owner:
            raise SandboxError(
                f"你最多同时运行 {self.max_per_owner} 个沙盒，请先停止其他沙盒。"
            )
        all_sandboxes = await self._list_by_label(f"{self._sandbox_label}=true")
        if sum(self._status_is_running(item) for item in all_sandboxes) >= self.max_total:
            raise SandboxError("机器人正在运行的沙盒总数已达到上限。")
        result = await self._run("docker", "start", name, timeout=30)
        if result.returncode:
            raise SandboxError(self._docker_error(result.stderr))

    async def stop_owned(self, owner: str, sandbox_id: str) -> None:
        """Stop a container without deleting its persistent workspace volume."""
        self._ensure_idle(sandbox_id)
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            self._ensure_idle(sandbox_id)
            name = await self._owned_container(owner, sandbox_id, require_running=False, allow_project=False)
            owned = await self.list(owner)
            target = next(
                (
                    item
                    for item in owned
                    if str(item.get("sandbox_id") or "") == sandbox_id
                ),
                None,
            )
            if target is not None and not self._status_is_running(target):
                return
            result = await self._run(
                "docker", "stop", "--time", "5", name, timeout=15
            )
            if result.returncode:
                raise SandboxError(self._docker_error(result.stderr))

    @staticmethod
    def _status_is_running(item: dict[str, str]) -> bool:
        return str(item.get("status") or "").lower().startswith("up ")

    def _ensure_idle(self, sandbox_id: str) -> None:
        if any(
            activity.sandbox_id == sandbox_id
            for activity in self._active_execs.values()
        ):
            raise SandboxError("沙盒正在执行命令，不能停止或删除。")

    async def _owned_container(
        self,
        owner: str,
        sandbox_id: str,
        *,
        require_running: bool = True,
        allow_project: bool = True,
    ) -> str:
        if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
            raise SandboxError("沙盒 ID 格式错误。")
        name = self._container_name(sandbox_id)
        result = await self._run(
            "docker",
            "inspect",
            "--format",
            ('[{{json .Config.Labels}},{{json .State.Running}}]' if self.namespace
             else '{{ index .Config.Labels "qqbot.owner" }}|{{ .State.Running }}'),
            name,
            timeout=20,
        )
        if result.returncode != 0:
            raise SandboxError("沙盒不存在。")
        if self.namespace:
            try:
                labels, running = json.loads(result.stdout)
            except (ValueError, TypeError):
                raise SandboxError("无法核对沙盒归属。") from None
            if (not isinstance(labels, dict) or type(running) is not bool
                    or labels.get("gaoji.app.instance") != self.namespace
                    or labels.get(self._sandbox_label) != "true" or labels.get("qqbot.id") != sandbox_id):
                raise SandboxError("沙盒不属于本实例。")
            owner_hash = labels.get("qqbot.owner")
            running = str(running)
        else:
            owner_hash, _, running = result.stdout.strip().partition("|")
        if owner_hash != self._owner_hash(owner):
            raise SandboxError("你无权访问这个沙盒。")
        if require_running and running.lower() != "true":
            raise SandboxError("沙盒没有运行。")
        if self.namespace and labels.get("qqbot.purpose") == "project":
            if not allow_project or self.project_workspaces is None:
                raise SandboxError("项目工作区必须通过项目生命周期管理。")
            return await self.project_workspaces.authorize(owner, sandbox_id, labels, require_running=require_running)
        return name

    async def _network_mode(self, name: str) -> str:
        result = await self._run(
            "docker",
            "inspect",
            "--format",
            "{{ .HostConfig.NetworkMode }}",
            name,
            timeout=10,
        )
        mode = result.stdout.strip()[:80] if result.returncode == 0 else "unknown"
        if mode != self._network_name:
            raise SandboxError("旧沙盒网络不符合当前策略，请管理员重建工作区后再执行。")
        return mode

    @property
    def _network_name(self) -> str:
        return "gaoji-sandbox" if self.network == "isolated" else self.network

    async def _verify_network_policy(self) -> None:
        if self.network != "isolated":
            return
        from .network import NETWORK, validate_network
        result = await self._run("docker", "network", "inspect", NETWORK, timeout=10)
        try:
            if result.returncode:
                raise ValueError("Network is unavailable")
            validate_network(json.loads(result.stdout))
        except (ValueError, TypeError, KeyError) as exc:
            raise SandboxError("沙盒隔离网络未配置，请管理员运行网络安装步骤；不会退回普通 bridge。") from exc

    async def _changed_workspace_paths(
        self,
        name: str,
        marker: str,
    ) -> tuple[str, ...]:
        command = (
            "find /workspace -xdev -type f -newer "
            f"{shlex.quote(marker)} -print 2>/dev/null"
        )
        result = await self._run(
            "docker",
            "exec",
            name,
            "sh",
            "-lc",
            command,
            timeout=20,
        )
        if result.returncode != 0:
            return ()
        paths = []
        for raw_path in result.stdout.splitlines():
            path = raw_path.strip()
            if not path.startswith("/workspace/"):
                continue
            relative = path.removeprefix("/workspace/")
            if relative and relative not in paths:
                paths.append(relative[:500])
            if len(paths) >= 200:
                break
        return tuple(paths)

    async def _container_diff(
        self,
        name: str,
        marker: str,
    ) -> tuple[str, ...]:
        result = await self._run("docker", "diff", name, timeout=20)
        if result.returncode != 0:
            return ()
        entries = []
        for line in result.stdout.splitlines():
            normalized = " ".join(line.split())[:600]
            if not normalized or marker in normalized:
                continue
            if normalized not in entries:
                entries.append(normalized)
            if len(entries) >= 200:
                break
        return tuple(entries)

    async def _remove_observation_marker(self, name: str, marker: str) -> None:
        try:
            await self._run(
                "docker",
                "exec",
                name,
                "rm",
                "-f",
                marker,
                timeout=10,
            )
        except SandboxError:
            return

    async def _list_by_label(self, label: str) -> list[dict[str, str]]:
        result = await self._run(
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={label}",
            *self._namespace_filters(),
            "--format",
            (
                '{{.Label "qqbot.id"}}|{{.Label "qqbot.runtime"}}|'
                '{{.Label "qqbot.purpose"}}|{{.Status}}'
            ),
            timeout=20,
        )
        if result.returncode != 0:
            raise SandboxError(self._docker_error(result.stderr))
        sandboxes: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            sandbox_id, runtime, purpose, status = (
                line.split("|", 3) + ["", "", ""]
            )[:4]
            if SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
                sandboxes.append(
                    {
                        "sandbox_id": sandbox_id,
                        "runtime": runtime,
                        "purpose": purpose or "task",
                        "status": status,
                    }
                )
        return sandboxes

    async def _container_stats(
        self,
        names: list[str],
    ) -> dict[str, dict[str, str]]:
        if not names:
            return {}
        result = await self._run(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
            *names,
            timeout=20,
        )
        if result.returncode != 0:
            return {}
        stats: dict[str, dict[str, str]] = {}
        for line in result.stdout.splitlines():
            name, cpu, memory, memory_percent = (
                line.split("|", 3) + ["", "", "", ""]
            )[:4]
            if name:
                stats[name] = {
                    "cpu_percent": cpu,
                    "memory_usage": memory,
                    "memory_percent": memory_percent,
                }
        return stats

    async def _workspace_inventory(self, name: str) -> dict[str, object]:
        script = (
            "printf '__TOTAL__|'; "
            "du -sb /workspace 2>/dev/null | cut -f1; "
            "printf '__COUNT__|'; "
            "find /workspace -xdev -type f -printf '.' 2>/dev/null | wc -c; "
            "find /workspace -xdev -type f -printf '%P|%s\\n' "
            "2>/dev/null | sort | head -n 100"
        )
        result = await self._run(
            "docker",
            "exec",
            name,
            "sh",
            "-lc",
            script,
            timeout=20,
        )
        if result.returncode != 0:
            return {"workspace_error": self._docker_error(result.stderr)}

        total_size = 0
        file_count = 0
        files: list[dict[str, object]] = []
        for line in result.stdout.splitlines():
            if line.startswith("__TOTAL__|"):
                total_size = self._safe_int(line.partition("|")[2])
                continue
            if line.startswith("__COUNT__|"):
                file_count = self._safe_int(line.partition("|")[2])
                continue
            path, separator, raw_size = line.rpartition("|")
            if not separator or not path:
                continue
            files.append(
                {
                    "path": path[:500],
                    "size_bytes": self._safe_int(raw_size),
                }
            )
        return {
            "workspace_size_bytes": total_size,
            "workspace_file_count": file_count,
            "workspace_files": files,
            "workspace_error": "",
        }

    def _remember_exec(
        self,
        activity: SandboxExecutionActivity,
        *,
        status: str,
        returncode: int | None = None,
        error: str = "",
    ) -> None:
        self._last_execs[activity.sandbox_id] = SandboxExecutionActivity(
            activity_id=activity.activity_id,
            sandbox_id=activity.sandbox_id,
            owner=activity.owner,
            command=activity.command,
            started_at=activity.started_at,
            status=status,
            finished_at=int(time.time()),
            returncode=returncode,
            error=error[:500],
        )

    @staticmethod
    def _activity_payload(
        activity: SandboxExecutionActivity,
        *,
        now: int,
    ) -> dict[str, object]:
        finished_at = activity.finished_at
        elapsed_until = finished_at if finished_at is not None else now
        return {
            "activity_id": activity.activity_id,
            "command": activity.command,
            "started_at": activity.started_at,
            "finished_at": finished_at,
            "elapsed_seconds": max(elapsed_until - activity.started_at, 0),
            "status": activity.status,
            "returncode": activity.returncode,
            "error": activity.error,
        }

    @staticmethod
    def _safe_int(value: object) -> int:
        try:
            return max(int(str(value).strip()), 0)
        except (TypeError, ValueError):
            return 0

    async def _run(
        self,
        *command: str,
        input_bytes: bytes | None = None,
        timeout: int,
    ) -> SandboxResult:
        stdout, stderr, returncode = await self._run_bytes(
            *command,
            input_bytes=input_bytes,
            timeout=timeout,
        )
        errors = stderr.decode("utf-8", errors="replace")
        if returncode and len(errors) > self.max_output_chars:
            errors = "[省略前面的进度日志]\n" + errors[-self.max_output_chars:]
        return SandboxResult(
            stdout=self._trim_output(stdout.decode("utf-8", errors="replace")),
            stderr=errors if returncode else self._trim_output(errors),
            returncode=returncode,
        )

    async def _run_bytes(
        self,
        *command: str,
        input_bytes: bytes | None = None,
        timeout: int,
    ) -> tuple[bytes, bytes, int]:
        command = self._docker_command(command)
        if len(command) > 1 and command[1] in {"run", "exec"}:
            if command[1] == "exec" or command[command.index("--network") + 1] != "none":
                await self._verify_network_policy()
        helper = None
        if self.namespace and len(command) > 1 and command[1] == "run" and "-d" not in command:
            helper = command[command.index("--name") + 1]
        if helper is not None:
            async with self._helper_lock:
                try:
                    return await self._run_process(command, input_bytes=input_bytes, timeout=timeout)
                finally:
                    await self._cleanup_helper(helper)
        return await self._run_process(command, input_bytes=input_bytes, timeout=timeout)

    async def _cleanup_helper(self, name: str) -> None:
        result = await self._run("docker", "inspect", "--format", "{{json .Config.Labels}}", name, timeout=15)
        if result.returncode:
            if any(marker in (result.stderr or result.stdout).lower() for marker in ("no such object", "no such container")):
                return
            raise SandboxError("辅助沙盒回收未确认，请检查实例沙盒状态。")
        try:
            labels = json.loads(result.stdout)
        except (TypeError, ValueError):
            labels = None
        if not isinstance(labels, dict) or labels.get("gaoji.app.instance") != self.namespace or labels.get("gaoji.app.resource") != "helper":
            raise SandboxError("辅助沙盒归属不符，已拒绝回收。")
        removed = await self._run("docker", "rm", "-f", name, timeout=20)
        if removed.returncode and "no such container" not in (removed.stderr or removed.stdout).lower():
            raise SandboxError("辅助沙盒未能回收，请检查实例沙盒状态。")

    async def _run_process(self, command: tuple[str, ...], *, input_bytes: bytes | None, timeout: int) -> tuple[bytes, bytes, int]:
        if self.namespace and input_bytes is not None and len(input_bytes) > self.max_file_bytes:
            raise SandboxError("输入文件超过本实例单次传输上限。")
        environment = None
        if self.docker_host:
            environment = {key: value for key, value in os.environ.items() if key not in {"DOCKER_HOST", "DOCKER_CONTEXT"}}
            environment["DOCKER_HOST"] = self.docker_host
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                **({"env": environment} if environment is not None else {}),
                stdin=(
                    asyncio.subprocess.PIPE
                    if input_bytes is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SandboxError("没有安装 Docker 命令行工具。") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                self._communicate_bounded(process, input_bytes) if self.namespace else process.communicate(input_bytes),
                timeout=timeout,
            )
        except (asyncio.CancelledError, SandboxError):
            await self._terminate_process(process)
            raise
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            raise SandboxError(f"沙盒操作超过 {timeout} 秒，已终止等待。") from exc
        return stdout, stderr, process.returncode or 0

    async def _communicate_bounded(self, process, content: bytes | None) -> tuple[bytes, bytes]:
        async def read(stream, limit):
            output = bytearray()
            while chunk := await stream.read(65536):
                if len(output) + len(chunk) > limit:
                    raise SandboxError("沙盒输出超过本实例单次传输上限，请缩小输出或拆分文件。")
                output.extend(chunk)
            return bytes(output)

        async def write():
            if process.stdin is None:
                return
            try:
                for offset in range(0, len(content or b""), 65536):
                    process.stdin.write(content[offset:offset + 65536])
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(read(process.stdout, self.max_file_bytes)),
                 asyncio.create_task(read(process.stderr, min(self.max_file_bytes, 1024 * 1024))),
                 asyncio.create_task(write()), asyncio.create_task(process.wait())]
        try:
            stdout, stderr, _, _ = await asyncio.gather(*tasks)
            return stdout, stderr
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _terminate_process(self, process) -> None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        if not self.namespace:
            await process.communicate()
            return

        async def discard(stream):
            while await stream.read(65536):
                pass

        await asyncio.wait_for(asyncio.gather(process.wait(), discard(process.stdout), discard(process.stderr)), timeout=10)

    def _workspace_path(self, path: str) -> str:
        candidate = PurePosixPath(path.strip())
        if (
            not path.strip()
            or candidate.is_absolute()
            or ".." in candidate.parts
            or str(candidate) in {".", ""}
        ):
            raise SandboxError("文件路径必须是 /workspace 下的安全相对路径。")
        return str(PurePosixPath("/workspace") / candidate)

    def _trim_output(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        return text[: self.max_output_chars].rstrip() + "\n[输出过长，已截断]"

    def _container_name(self, sandbox_id: str) -> str:
        if self.namespace:
            return f"gaoji-app-{self.namespace}-{sandbox_id}"
        return f"qqbot-{sandbox_id}"

    def _workspace_volume_name(self, sandbox_id: str) -> str:
        if self.namespace:
            return f"gaoji-app-{self.namespace}-work-{sandbox_id}"
        return f"gaoji-work-{sandbox_id}"

    def _workspace_volumes(self, sandbox_id: str) -> set[str]:
        volumes = {self._workspace_volume_name(sandbox_id)}
        if not self.namespace:
            volumes.add(f"kennethbot-work-{sandbox_id}")
        return volumes

    def _memory_limit(self, ceiling_mib: int | None = None) -> str:
        value = min(self.memory_mib, ceiling_mib) if ceiling_mib is not None else self.memory_mib
        return f"{value // 1024}g" if value % 1024 == 0 else f"{value}m"

    def _docker_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
        if not command or command[0] != "docker":
            raise SandboxError("沙盒驱动只接受内部 Docker 操作。")
        additions: list[str] = []
        if len(command) > 1 and command[1] == "run":
            if "--network" not in command:
                additions.extend(("--network", self._network_name))
            elif command[command.index("--network") + 1] not in {"none", self._network_name}:
                raise SandboxError("辅助沙盒不能绕过当前网络隔离策略。")
            additions.extend(("--sysctl", "net.ipv6.conf.all.disable_ipv6=1"))
            if self.cpus is not None:
                additions.extend(("--cpus", str(self.cpus)))
            if self.pids_limit is not None:
                additions.extend(("--pids-limit", str(self.pids_limit)))
            if self.namespace:
                additions.extend(("--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=2"))
                if "-d" not in command:
                    additions.extend(("--name", f"gaoji-app-{self.namespace}-helper-{secrets.token_hex(8)}"))
                    additions.extend(self._resource_labels("helper"))
        return (self.docker_executable, *command[1:2], *additions, *command[2:])

    @staticmethod
    def _owner_ref(owner: str) -> str:
        return " ".join(str(owner).split())[:200]

    @staticmethod
    def _owner_hash(owner: str) -> str:
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _docker_error(detail: str) -> str:
        lowered = detail.lower()
        if "no such image" in lowered:
            return "沙盒镜像未在本机加载或已被清理，请管理员恢复沙盒镜像；不是模型 API 或登录问题。"
        if (
            "cannot connect to the docker daemon" in lowered
            or "failed to connect to the docker api" in lowered
            or "is the docker daemon running" in lowered
        ):
            return "Docker 服务没有启动。请先打开 Docker Desktop。"
        if "permission denied" in lowered and "docker" in lowered:
            return "机器人没有访问 Docker 的权限。"
        return detail.strip()[-500:] or "Docker 操作失败。"
