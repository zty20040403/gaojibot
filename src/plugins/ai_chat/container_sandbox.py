"""Conversation workspaces run as NixOS containers (systemd-nspawn).

WorkspaceSandboxManager owns the guest-independent upper layer: ownership,
host locks, metadata, command execution bookkeeping, workspace file staging /
export scripts, operations SSH configuration, and conversation lifecycle hooks.
ContainerSandboxManager implements the container primitives via the root
container broker (start, stop, status, exec, remove).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Callable

from .sandbox import (
    SANDBOX_ID_PATTERN,
    SandboxError,
    SandboxExecutionActivity,
    SandboxObservedManifest,
    SandboxResult,
)

logger = logging.getLogger(__name__)
IMAGE = "nixos-container"
CONTAINER_PREFIX = "gct-"

INSTALL_FILE_SCRIPT = """
import os, shutil, sys, uuid
source, target, readonly = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
parts = target.split('/')[2:]
fd = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for part in parts[:-1]:
        if part in ('', '.', '..'):
            raise ValueError('Invalid workspace path')
        created = False
        try:
            os.mkdir(part, 0o755, dir_fd=fd)
            created = True
        except FileExistsError:
            pass
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        if readonly:
            os.fchown(child, 0, 0)
            os.fchmod(child, 0o755)
        else:
            if created:
                os.fchown(child, 1000, 1000)
            owner = os.fstat(child).st_uid
            if owner != 1000:
                raise ValueError('Directory is not writable by sandbox')
        os.close(fd)
        fd = child
    temporary = '.upload-' + uuid.uuid4().hex
    output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    try:
        with open(source, 'rb') as data, os.fdopen(output, 'wb') as target_file:
            shutil.copyfileobj(data, target_file)
            target_file.flush()
            os.fsync(target_file.fileno())
            os.fchown(target_file.fileno(), 0 if readonly else 1000,
                      0 if readonly else 1000)
            os.fchmod(target_file.fileno(), 0o444 if readonly else 0o644)
        if readonly:
            try:
                existing = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW,
                                   dir_fd=fd)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                with os.fdopen(existing, 'rb') as old, open(source, 'rb') as new:
                    if old.read() != new.read():
                        raise ValueError('Read-only artifact conflict')
                    sys.exit(0)
        os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
finally:
    os.close(fd)
"""

ARCHIVE_DIRECTORY_SCRIPT = """
import os, stat, sys, zipfile
from pathlib import Path
root = Path(sys.argv[1])
output = Path(sys.argv[2])
if root.is_symlink() or not root.is_dir():
    raise ValueError('Artifact directory changed')
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
"""


class WorkspaceSandboxManager:
    backend = "container"

    def __init__(
        self,
        *,
        root: str,
        max_per_owner: int = 2,
        max_total: int = 8,
        default_timeout_seconds: int = 120,
        max_output_chars: int = 12000,
        max_file_bytes: int = 20 * 1024 * 1024,
        memory_mib: int = 4096,
        vcpus: int = 1,
        pids_limit: int = 256,
        operations_owners: frozenset[str] = frozenset(),
        operations_domain: str = "",
    ) -> None:
        self.root = Path(root)
        self.max_per_owner = max(1, max_per_owner)
        self.max_total = max(1, max_total)
        self.default_timeout_seconds = max(5, default_timeout_seconds)
        self.max_output_chars = max(1000, max_output_chars)
        self.max_file_bytes = max(0, max_file_bytes)
        if type(memory_mib) is not int or not 256 <= memory_mib <= 8192:
            raise ValueError("Sandbox memory must be between 256 and 8192 MiB")
        if type(vcpus) is not int or not 1 <= vcpus <= 16:
            raise ValueError("Sandbox CPUs must be between 1 and 16")
        if type(pids_limit) is not int or not 32 <= pids_limit <= 4096:
            raise ValueError("Sandbox process limit must be between 32 and 4096")
        self.memory_mib = memory_mib
        self.vcpus = vcpus
        self.pids_limit = pids_limit
        self._exec_locks: dict[str, asyncio.Lock] = {}
        self._create_lock = asyncio.Lock()
        self._active_execs: dict[str, SandboxExecutionActivity] = {}
        self._last_execs: dict[str, SandboxExecutionActivity] = {}
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._conversation_guests: set[str] = set()
        self.conversation_lifecycle = None
        self._operations_hashes = frozenset(self._owner_hash(owner) for owner in operations_owners)
        self.operations_domain = operations_domain

    @asynccontextmanager
    async def allocation_lock(self):
        from src.sandbox_runtime.project_vm_io import host_lock

        async with self._create_lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            async with host_lock(self.root / ".vm-allocation.lock"):
                yield

    @asynccontextmanager
    async def io_lock(self, sandbox_id: str, *, timeout: int = 10):
        from src.sandbox_runtime.project_vm_io import host_lock

        self._directory(sandbox_id)
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        if timeout == 0 and lock.locked():
            raise SandboxError("Workspace has an active command or file transfer")
        async with lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077):
                raise SandboxError("Sandbox root must be a private owned directory")
            # Outside the sandbox directory: boot and retirement must share this lock.
            async with AsyncExitStack() as stack:
                try:
                    await stack.enter_async_context(host_lock(self.root / f".io-{sandbox_id}.lock", timeout=timeout))
                except TimeoutError:
                    raise SandboxError("Workspace has an active command or file transfer") from None
                yield

    @staticmethod
    def _owner_hash(owner: str) -> str:
        return hashlib.sha256(owner.encode()).hexdigest()[:16]

    @staticmethod
    def _owner_ref(owner: str) -> str:
        return owner[:100]

    @staticmethod
    def _name(sandbox_id: str) -> str:
        return CONTAINER_PREFIX + sandbox_id

    def _directory(self, sandbox_id: str) -> Path:
        if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
            raise SandboxError("沙盒 ID 格式错误。")
        return self.root / sandbox_id

    def _save_metadata(self, sandbox_id: str, data: dict[str, object], *, filename: str = "metadata.json") -> None:
        if filename != "metadata.json":
            raise ValueError("invalid_sandbox_metadata_file")
        data = {key: value for key, value in data.items() if key != "programs"}
        directory = self._directory(sandbox_id)
        temporary = directory / (".metadata-" + secrets.token_hex(8))
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / filename)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _workspace_path(path: str) -> str:
        if path.startswith("/work/"):
            path = "/workspace/" + path[len("/work/"):]
        source = PurePosixPath(path)
        if source.is_absolute():
            try:
                source = source.relative_to("/workspace")
            except ValueError as exc:
                raise SandboxError("文件只能位于 /workspace。") from exc
        if not source.parts or any(part in {"", ".", ".."} for part in source.parts):
            raise SandboxError("文件路径无效。")
        return str(PurePosixPath("/workspace") / source)

    def _operations_sandbox(self, sandbox_id: str) -> bool:
        if not self._operations_hashes or not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
            return False
        try:
            data = json.loads((self.root / sandbox_id / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return data.get("owner_hash") in self._operations_hashes

    async def _metadata(self, owner: str, sandbox_id: str) -> dict[str, object]:
        directory = self._directory(sandbox_id)
        try:
            data = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SandboxError("沙盒不存在或元数据不可读。") from exc
        if data.get("id") != sandbox_id or data.get("owner_hash") != self._owner_hash(owner):
            raise SandboxError("沙盒不属于当前会话。")
        return data

    async def _all_metadata(self) -> list[dict[str, object]]:
        if not self.root.exists():
            return []
        found: list[dict[str, object]] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or not SANDBOX_ID_PATTERN.fullmatch(directory.name):
                continue
            try:
                data = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("id") == directory.name:
                found.append(data)
        return found

    async def _running(self, sandbox_id: str) -> bool:
        raise NotImplementedError

    async def _start(self, sandbox_id: str) -> None:
        raise NotImplementedError

    async def _stop(self, sandbox_id: str) -> None:
        raise NotImplementedError

    async def _remove(self, sandbox_id: str) -> None:
        raise NotImplementedError

    async def _guest_exec_raw(
        self, sandbox_id: str, path: str, args: list[str], *, timeout: int = 60,
        input_bytes: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        raise NotImplementedError

    async def _wait_guest(self, sandbox_id: str, *, timeout: int = 300) -> None:
        raise NotImplementedError

    async def _guest_exec(
        self, sandbox_id: str, path: str, args: list[str], *, timeout: int = 60,
        input_bytes: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        if sandbox_id in self._conversation_guests:
            await self._wait_guest(sandbox_id, timeout=60)
        return await self._guest_exec_raw(sandbox_id, path, args, timeout=timeout, input_bytes=input_bytes)

    async def list(self, owner: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in await self._all_metadata():
            if item.get("owner_hash") != self._owner_hash(owner):
                continue
            sandbox_id = str(item["id"])
            result.append({
                "sandbox_id": sandbox_id, "name": self._name(sandbox_id),
                "runtime": str(item["runtime"]), "purpose": str(item["purpose"]),
                "status": "Up (container)" if await self._running(sandbox_id) else "Exited",
            })
        return result

    async def ensure_default(self, owner: str, runtime: str = "debian") -> dict[str, str]:
        for item in await self.list(owner):
            if not item["status"].startswith("Up "):
                await self.start_owned(owner, item["sandbox_id"])
            return {**item, "status": "running", "toolset": "vm"}
        return await self.create(owner, runtime, purpose="shell")

    async def _owned_running(self, owner: str, sandbox_id: str) -> dict[str, object]:
        metadata = await self._metadata(owner, sandbox_id)
        if not await self._running(sandbox_id):
            raise SandboxError("沙盒没有运行。")
        if owner.startswith("qqo:conversation:"):
            self._conversation_guests.add(sandbox_id)
            metadata["last_used"] = int(time.time())
            self._save_metadata(sandbox_id, metadata)
        return metadata

    async def exec(
        self, owner: str, sandbox_id: str, command: str,
        timeout_seconds: int | None = None,
        packages: list[str] | tuple[str, ...] | None = None,
    ) -> SandboxResult:
        if packages:
            raise SandboxError("容器已预装常用工具；缺少的 Python/Node 包请用 pip/npm/uv 装到 /workspace。")
        if not command.strip():
            raise SandboxError("命令不能为空。")
        async with self.io_lock(sandbox_id):
            await self._owned_running(owner, sandbox_id)
            pids_limit = self.pids_limit
            timeout = min(max(timeout_seconds or self.default_timeout_seconds, 1), 300)
            activity_id = secrets.token_hex(8)
            activity = SandboxExecutionActivity(
                activity_id=activity_id, sandbox_id=sandbox_id,
                owner=self._owner_ref(owner), command=command[:2000],
                started_at=int(time.time()),
            )
            self._active_execs[activity_id] = activity
            started_at = time.monotonic()
            marker = f"/tmp/gaoji-exec-{activity_id}"
            marker_ready = False
            try:
                marker_code, _, _ = await self._guest_exec(
                    sandbox_id, "/usr/bin/touch", [marker], timeout=10,
                )
                marker_ready = marker_code == 0
                code, out, err = await self._guest_exec(
                    sandbox_id, "/usr/sbin/runuser", [
                        "-u", "sandbox", "--", "/usr/bin/prlimit",
                        f"--nproc={pids_limit}:{pids_limit}", "--",
                        "/usr/bin/timeout", "--signal=TERM",
                        "--kill-after=5s", str(timeout), "/bin/sh", "-lc",
                        "cd /workspace && " + command,
                    ], timeout=timeout + 15,
                )
                self._last_execs[sandbox_id] = SandboxExecutionActivity(
                    **{**activity.__dict__, "status": "completed" if code == 0 else "failed",
                       "finished_at": int(time.time()), "returncode": code},
                )
                changed_paths: tuple[str, ...] = ()
                if marker_ready:
                    try:
                        scan_code, scan_out, _ = await self._guest_exec(
                            sandbox_id, "/usr/bin/find", [
                                "/workspace", "-xdev", "-type", "f", "-newer", marker,
                                "-print",
                            ], timeout=20,
                        )
                        if scan_code == 0:
                            changed_paths = tuple(
                                path.removeprefix("/workspace/")[:500]
                                for path in scan_out.decode(errors="replace").splitlines()
                                if path.startswith("/workspace/")
                            )[:200]
                    except SandboxError:
                        pass
                return SandboxResult(
                    stdout=out.decode(errors="replace")[:self.max_output_chars],
                    stderr=err.decode(errors="replace")[:self.max_output_chars],
                    returncode=code,
                    manifest=SandboxObservedManifest(
                        command=command,
                        duration_ms=max(int((time.monotonic() - started_at) * 1000), 0),
                        stdout_sha256=hashlib.sha256(out).hexdigest(), stdout_bytes=len(out),
                        stderr_sha256=hashlib.sha256(err).hexdigest(), stderr_bytes=len(err),
                        changed_workspace_paths=changed_paths, container_diff=(), network_mode="bridge",
                    ),
                )
            finally:
                if marker_ready:
                    try:
                        await self._guest_exec(
                            sandbox_id, "/usr/bin/rm", ["-f", marker], timeout=10,
                        )
                    except SandboxError:
                        pass
                self._active_execs.pop(activity_id, None)

    async def run_container_command(
        self,
        owner: str,
        sandbox_id: str,
        command: str,
        *,
        workdir: str = "/work",
        timeout_seconds: int | None = None,
        input_bytes: bytes | None = None,
    ) -> SandboxResult:
        """Run a foreground command inside the conversation container without holding io_lock.

        Tracking in _active_execs prevents stop/destroy during execution while
        allowing re-entrant /job/tool calls (e.g. send_image / send_file from
        inside run_code or shell) to acquire io_lock without deadlocking.
        """
        if not command.strip():
            raise SandboxError("命令不能为空。")
        await self._owned_running(owner, sandbox_id)
        raw_wd = (workdir or "/work").strip()
        if raw_wd in {"/work", "/workspace"}:
            wd = "/work"
        else:
            rel = self._workspace_path(raw_wd).removeprefix("/workspace/")
            wd = "/work/" + rel
        timeout = min(max(timeout_seconds or self.default_timeout_seconds, 1), 300)
        pids_limit = self.pids_limit
        activity_id = secrets.token_hex(8)
        activity = SandboxExecutionActivity(
            activity_id=activity_id,
            sandbox_id=sandbox_id,
            owner=self._owner_ref(owner),
            command=command[:2000],
            started_at=int(time.time()),
        )
        self._active_execs[activity_id] = activity
        started_at = time.monotonic()
        wrapper = (
            "[ -r /etc/set-environment ] && . /etc/set-environment\n"
            'export HOME=/work PATH="/run/wrappers/bin:/run/current-system/sw/bin:${PATH:-/usr/bin:/bin}"\n'
            f"mkdir -p -- {shlex.quote(wd)} && cd -- {shlex.quote(wd)}\n"
            f"exec bash -lc {shlex.quote(command)}"
        )
        try:
            code, out, err = await self._guest_exec(
                sandbox_id,
                "/usr/bin/prlimit",
                [
                    f"--nproc={pids_limit}:{pids_limit}",
                    "--",
                    "/usr/bin/timeout",
                    "--signal=TERM",
                    "--kill-after=5s",
                    str(timeout),
                    "/bin/sh",
                    "-c",
                    wrapper,
                ],
                timeout=timeout + 15,
                input_bytes=input_bytes,
            )
            self._last_execs[sandbox_id] = SandboxExecutionActivity(
                **{
                    **activity.__dict__,
                    "status": "completed" if code == 0 else "failed",
                    "finished_at": int(time.time()),
                    "returncode": code,
                },
            )
            return SandboxResult(
                stdout=out.decode(errors="replace")[:self.max_output_chars],
                stderr=err.decode(errors="replace")[:self.max_output_chars],
                returncode=code,
                manifest=SandboxObservedManifest(
                    command=command,
                    duration_ms=max(int((time.monotonic() - started_at) * 1000), 0),
                    stdout_sha256=hashlib.sha256(out).hexdigest(),
                    stdout_bytes=len(out),
                    stderr_sha256=hashlib.sha256(err).hexdigest(),
                    stderr_bytes=len(err),
                    changed_workspace_paths=(),
                    container_diff=(),
                    network_mode="bridge",
                ),
            )
        finally:
            self._active_execs.pop(activity_id, None)

    async def write_file(
        self, owner: str, sandbox_id: str, path: str, content: bytes, *, allow_large: bool = False,
    ) -> int:
        return await self._write_content(owner, sandbox_id, path, content,
                                         allow_large=allow_large, readonly=False)

    async def _write_content(
        self, owner: str, sandbox_id: str, path: str, content: bytes, *,
        allow_large: bool, readonly: bool,
    ) -> int:
        async with self.io_lock(sandbox_id):
            return await self._write_content_locked(
                owner, sandbox_id, path, content, allow_large=allow_large, readonly=readonly,
            )

    async def _write_content_locked(
        self, owner: str, sandbox_id: str, path: str, content: bytes, *,
        allow_large: bool, readonly: bool,
    ) -> int:
        await self._owned_running(owner, sandbox_id)
        limit = self.max_file_bytes or None
        if not allow_large:
            limit = min(limit or 512 * 1024, 512 * 1024)
        if limit and len(content) > limit:
            raise SandboxError(f"单次写入文件不能超过 {limit} 字节。")
        target = self._workspace_path(path)
        if readonly != target.startswith("/workspace/upstream/"):
            raise SandboxError("upstream 目录只能用于只读交接。")
        staged = "/root/gaoji-upload-" + secrets.token_hex(12)
        try:
            code, _, err = await self._guest_exec_raw(
                sandbox_id, "/bin/sh", ["-c", "umask 077; cat > " + shlex.quote(staged)],
                timeout=120, input_bytes=content,
            )
            if code:
                raise SandboxError(f"容器文件写入失败：{err.decode(errors='replace')[-500:]}")
            code, _, err = await self._guest_exec(
                sandbox_id, "/usr/bin/python3",
                ["-c", INSTALL_FILE_SCRIPT, staged, target, "1" if readonly else "0"], timeout=120,
            )
            if code:
                raise SandboxError(f"容器文件导入失败：{err.decode(errors='replace')[-500:]}")
        finally:
            await self._guest_exec(sandbox_id, "/bin/rm", ["-f", staged])
        return len(content)

    async def _read_guest_file(
        self, sandbox_id: str, target: str, max_bytes: int | None, *, ignore_manager_cap: bool = False,
    ) -> bytes:
        limit = None if ignore_manager_cap else (self.max_file_bytes or None)
        if max_bytes and max_bytes > 0:
            limit = min(limit, max_bytes) if limit else max_bytes
        code, out, _ = await self._guest_exec(sandbox_id, "/usr/bin/stat", ["-c", "%F:%s", target], timeout=20)
        if code:
            raise SandboxError("文件不存在或无法读取。")
        try:
            kind, raw_size = out.decode().strip().split(":", 1)
            size = int(raw_size)
        except ValueError as exc:
            raise SandboxError("无法读取文件大小。") from exc
        if kind != "regular file":
            raise SandboxError("只能读取普通文件，不能读取符号链接。")
        if limit and size > limit:
            raise SandboxError(f"文件大小 {size} 字节，超过读取上限 {limit}。")
        # cat with O_NOFOLLOW semantics: refuse if the path became a symlink.
        code, data, err = await self._guest_exec(
            sandbox_id, "/usr/bin/python3", [
                "-c",
                "import os,sys;fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW);"
                "f=os.fdopen(fd,'rb');sys.stdout.buffer.write(f.read())",
                target,
            ], timeout=300,
        )
        if code:
            raise SandboxError("文件读取失败：" + err.decode(errors="replace")[-300:])
        if len(data) != size:
            raise SandboxError("文件在读取期间变化，请重试。")
        return data

    async def read_file(
        self, owner: str, sandbox_id: str, path: str, max_bytes: int | None = 64 * 1024,
    ) -> bytes:
        async with self.io_lock(sandbox_id):
            await self._owned_running(owner, sandbox_id)
            target = self._workspace_path(path)
            return await self._read_guest_file(sandbox_id, target, max_bytes)

    async def install_readonly_file(self, owner: str, sandbox_id: str, path: str, content: bytes) -> None:
        target = self._workspace_path(path)
        if not target.startswith("/workspace/upstream/"):
            raise SandboxError("只读交接只能写入 upstream 目录。")
        await self._write_content(owner, sandbox_id, path, content,
                                  allow_large=True, readonly=True)

    async def export_artifact(self, owner: str, sandbox_id: str, path: str, *,
                              file_only: bool = False, max_bytes: int | None = None) -> tuple[bytes, bool]:
        async with self.io_lock(sandbox_id):
            if type(file_only) is not bool:
                raise SandboxError("Invalid export mode")
            if file_only:
                from src.sandbox_runtime.artifact_export import STAGE_FILE_SCRIPT

                if (type(self.max_file_bytes) is not int or self.max_file_bytes <= 0
                        or type(max_bytes) is not int or max_bytes <= 0):
                    raise SandboxError("Bounded export requires positive limits")
                limit = min(self.max_file_bytes, max_bytes, 256 * 1024 * 1024)
                await self._owned_running(owner, sandbox_id)
                target = self._workspace_path(path).removeprefix("/workspace/")
                staged = "/root/gaoji-export-" + secrets.token_hex(12)
                try:
                    code, _, _ = await self._guest_exec(
                        sandbox_id, "/usr/bin/python3",
                        ["-I", "-c", STAGE_FILE_SCRIPT, target, str(limit), staged], timeout=300,
                    )
                    if code:
                        raise SandboxError("Bounded regular-file export failed")
                    content = await self._read_guest_file(sandbox_id, staged, max_bytes=limit)
                finally:
                    await self._guest_exec(sandbox_id, "/bin/rm", ["-f", staged])
                if not 0 < len(content) <= limit:
                    raise SandboxError("Bounded regular-file export failed")
                return content, False
            return await self._export_artifact_locked(owner, sandbox_id, path)

    async def export_temp_bytes(
        self,
        owner: str,
        sandbox_id: str,
        path: str,
        *,
        max_bytes: int,
        delete_after: bool = False,
    ) -> bytes:
        """Read a regular file under /workspace up to max_bytes (<=64 MiB) for multimodal inspection."""
        from src.sandbox_runtime.artifact_export import STAGE_FILE_SCRIPT

        if type(max_bytes) is not int or not 0 < max_bytes <= 64 * 1024 * 1024:
            raise SandboxError("Invalid multimodal export limit")
        async with self.io_lock(sandbox_id):
            await self._owned_running(owner, sandbox_id)
            ws_full = self._workspace_path(path)
            target = ws_full.removeprefix("/workspace/")
            staged = "/root/gaoji-shown-" + secrets.token_hex(12)
            try:
                code, _, _ = await self._guest_exec(
                    sandbox_id,
                    "/usr/bin/python3",
                    ["-I", "-c", STAGE_FILE_SCRIPT, target, str(max_bytes), staged],
                    timeout=180,
                )
                if code:
                    raise SandboxError("Bounded multimodal file export failed")
                content = await self._read_guest_file(
                    sandbox_id, staged, max_bytes=max_bytes, ignore_manager_cap=True,
                )
            finally:
                await self._guest_exec(sandbox_id, "/bin/rm", ["-f", staged])
                if delete_after and ws_full.startswith("/workspace/.gaoji/shown/"):
                    await self._guest_exec(sandbox_id, "/bin/rm", ["-f", ws_full])
            if not 0 < len(content) <= max_bytes:
                raise SandboxError("Bounded multimodal file export failed")
            return content

    async def _export_artifact_locked(self, owner: str, sandbox_id: str, path: str) -> tuple[bytes, bool]:
        await self._owned_running(owner, sandbox_id)
        target = self._workspace_path(path)
        code, out, _ = await self._guest_exec(
            sandbox_id, "/bin/sh", ["-lc",
                "if [ -L " + shlex.quote(target) + " ]; then exit 1; "
                "elif [ -f " + shlex.quote(target) + " ]; then printf file; "
                "elif [ -d " + shlex.quote(target) + " ]; then printf directory; else exit 1; fi"],
        )
        if code:
            raise SandboxError("产物不存在、不可读取或为符号链接。")
        if out == b"file":
            return await self._read_guest_file(sandbox_id, target, max_bytes=None), False
        if out != b"directory":
            raise SandboxError("不支持的产物类型。")
        archive = "/root/gaoji-export-" + secrets.token_hex(12) + ".zip"
        try:
            code, _, err = await self._guest_exec(
                sandbox_id, "/usr/bin/python3", [
                    "-c", ARCHIVE_DIRECTORY_SCRIPT, target, archive,
                ], timeout=120,
            )
            if code:
                raise SandboxError(f"目录打包失败：{err.decode(errors='replace')[-500:]}")
            return await self._read_guest_file(sandbox_id, archive, max_bytes=None), True
        finally:
            await self._guest_exec(sandbox_id, "/bin/rm", ["-f", archive])

    async def search_nix_packages(self, query: str) -> list[dict[str, str]]:
        raise SandboxError("容器已预装常用工具；缺少的包请用 pip/npm/uv 装到 /workspace。")

    async def _configure_operations(self, owner: str, sandbox_id: str) -> None:
        """On the operations network `ssh <host>` reaches the fleet as gaoji, no key needed."""
        if not self.operations_domain or self._owner_hash(owner) not in self._operations_hashes:
            return
        config = (
            "Host * !*.*\n  HostName %h." + self.operations_domain + "\n"
            "Host *\n  User gaoji\n  BatchMode yes\n  ConnectTimeout 15\n  StrictHostKeyChecking accept-new\n"
        )
        script = "mkdir -p /etc/ssh/ssh_config.d && printf %s " + shlex.quote(config) + " > /etc/ssh/ssh_config.d/gaoji-operations.conf"
        code, _, err = await self._guest_exec(sandbox_id, "/bin/sh", ["-c", script], timeout=30)
        if code:
            raise SandboxError("容器的运维 SSH 配置失败：" + err.decode(errors="replace")[-200:])

    async def _conversation_event(self, event: str, owner: str, sandbox_id: str, **kwargs):
        if event == "ready":
            await self._configure_operations(owner, sandbox_id)
        if owner.startswith("qqo:conversation:") and self.conversation_lifecycle is not None:
            return await getattr(self.conversation_lifecycle, event)(owner, sandbox_id, **kwargs)

    async def create(self, owner: str, runtime: str = "python", *, purpose: str = "task") -> dict[str, str]:
        if purpose not in {"task", "shell"}:
            raise SandboxError("持久项目必须通过项目生命周期创建，不能降级为临时沙盒。")
        if runtime not in {"python", "node", "debian"}:
            raise SandboxError("不支持的运行环境。")
        async with self.allocation_lock():
            existing = await self._all_metadata()
            running = [item for item in existing if await self._running(str(item["id"]))]
            if len(running) >= self.max_total:
                raise SandboxError("沙盒总数已达到上限。")
            if sum(item.get("owner_hash") == self._owner_hash(owner) for item in running) >= self.max_per_owner:
                raise SandboxError("你正在运行的沙盒已达到上限。")
            sandbox_id = "s" + secrets.token_hex(3)
            directory = self._directory(sandbox_id)
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            metadata = {
                "id": sandbox_id, "owner_hash": self._owner_hash(owner),
                "owner_ref": self._owner_ref(owner), "runtime": runtime,
                "purpose": "shell" if purpose == "shell" else "task", "backend": self.backend,
                "created_at": int(time.time()), "last_started": int(time.time()), "state": "creating",
            }
            self._save_metadata(sandbox_id, metadata)
            started = False
            try:
                await self._conversation_event("starting", owner, sandbox_id)
                await self._start(sandbox_id)
                started = True
                await self._wait_guest(sandbox_id, timeout=120)
                await self._conversation_event("ready", owner, sandbox_id)
                metadata.update(state="ready", last_used=int(time.time()))
                self._save_metadata(sandbox_id, metadata)
                return {
                    "sandbox_id": sandbox_id, "runtime": runtime, "image": IMAGE,
                    "status": "running", "purpose": str(metadata["purpose"]), "toolset": "vm",
                }
            except (Exception, asyncio.CancelledError):
                try:
                    await self._conversation_event("failed", owner, sandbox_id)
                    if started:
                        await self._conversation_event("stopping", owner, sandbox_id)
                except Exception as error:
                    logger.warning("container failure bookkeeping error=%s", type(error).__name__)
                try:
                    await self._stop(sandbox_id)
                    await self._remove(sandbox_id)
                    shutil.rmtree(directory)
                except Exception as error:
                    logger.warning("container cleanup failed sandbox=%s error=%s", sandbox_id, type(error).__name__)
                raise

    async def start_owned(self, owner: str, sandbox_id: str, *, automatic: bool = False) -> None:
        try:
            await self._start_owned(owner, sandbox_id, automatic=automatic)
        except (Exception, asyncio.CancelledError):
            try:
                await self._conversation_event("failed", owner, sandbox_id)
            except Exception as error:
                logger.warning("container failure bookkeeping error=%s", type(error).__name__)
            raise

    async def _start_owned(self, owner: str, sandbox_id: str, *, automatic: bool = False) -> None:
        metadata = await self._metadata(owner, sandbox_id)
        event_args = {"automatic": True} if automatic else {}
        async with self.allocation_lock():
            metadata = await self._metadata(owner, sandbox_id)
            if owner.startswith("qqo:conversation:"):
                self._conversation_guests.add(sandbox_id)
                metadata["last_used"] = int(time.time())
                self._save_metadata(sandbox_id, metadata)
            if await self._running(sandbox_id):
                if owner.startswith("qqo:conversation:"):
                    await self._wait_guest(sandbox_id, timeout=60)
                    await self._conversation_event("ready", owner, sandbox_id, **event_args)
                return
            existing = await self._all_metadata()
            running = [item for item in existing if await self._running(str(item["id"]))]
            if len(running) >= self.max_total:
                raise SandboxError("沙盒总数已达到上限。")
            if sum(item.get("owner_hash") == self._owner_hash(owner) for item in running) >= self.max_per_owner:
                raise SandboxError("你正在运行的沙盒已达到上限。")
            async with self.io_lock(sandbox_id):
                await self._conversation_event("starting", owner, sandbox_id, **event_args)
                await self._start(sandbox_id)
            await self._wait_guest(sandbox_id, timeout=120)
            await self._conversation_event("ready", owner, sandbox_id, **event_args)
            metadata["last_started"] = int(time.time())
            self._save_metadata(sandbox_id, metadata)

    async def stop_owned(self, owner: str, sandbox_id: str, *, if_idle: bool = False) -> None:
        async with self.io_lock(sandbox_id):
            await self._metadata(owner, sandbox_id)
            if any(item.sandbox_id == sandbox_id for item in self._active_execs.values()):
                raise SandboxError("沙盒正在执行命令，不能停止。")
            if await self._conversation_event(
                "stopping", owner, sandbox_id, **({"automatic": True} if if_idle else {})
            ) is False:
                return
            if await self._running(sandbox_id):
                # /workspace is a host directory: stopping cleanly needs no freeze.
                await self._stop(sandbox_id)
            await self._conversation_event("stopped", owner, sandbox_id)

    async def destroy(self, owner: str, sandbox_id: str, *, if_unused: bool = False) -> None:
        await self._metadata(owner, sandbox_id)
        if any(item.sandbox_id == sandbox_id for item in self._active_execs.values()):
            raise SandboxError("沙盒正在执行命令，不能销毁。")
        async with self.io_lock(sandbox_id):
            if await self._conversation_event(
                "stopping", owner, sandbox_id, **({"automatic": True} if if_unused else {})
            ) is False:
                return
            if await self._running(sandbox_id):
                await self._stop(sandbox_id)
            await self._remove(sandbox_id)
            shutil.rmtree(self._directory(sandbox_id))
        self._exec_locks.pop(sandbox_id, None)

    async def maintain_conversations(
        self, *, now: int | None = None, idle_seconds: int = 7200, retention_seconds: int = 30 * 86400
    ) -> None:
        now = int(time.time()) if now is None else now
        for item in await self._all_metadata():
            owner, sid = str(item.get("owner_ref", "")), str(item["id"])
            if not owner.startswith("qqo:conversation:"):
                continue
            lock = self._conversation_locks.setdefault(owner, asyncio.Lock())
            if lock.locked() or self._exec_locks.get(sid, asyncio.Lock()).locked():
                continue
            async with lock:
                item = await self._metadata(owner, sid)
                age = now - int(item.get("last_used", item.get("last_started", item["created_at"])))
                try:
                    if age >= retention_seconds:
                        await self.destroy(owner, sid, if_unused=True)
                    elif age >= idle_seconds:
                        await self.stop_owned(owner, sid, if_idle=True)
                except Exception as error:
                    logger.warning(
                        "sandbox maintenance sandbox=%s error=%s: %s",
                        sid, type(error).__name__, str(error)[:200],
                    )

    async def reclaim_stopped(
        self, owner: str, sandbox_id: str, *, eligible: Callable[[], bool],
        not_used_since: int | None = None,
    ) -> None:
        metadata = await self._metadata(owner, sandbox_id)
        if metadata.get("purpose") != "task" or await self._running(sandbox_id):
            raise SandboxError("沙盒仍在使用，不能自动回收。")
        if not_used_since is not None and int(metadata.get("last_started", 0)) > not_used_since:
            raise SandboxError("沙盒已在交付后重新启动，不能回收。")
        if not eligible():
            raise SandboxError("任务状态已变化，已取消回收。")
        await self.destroy(owner, sandbox_id)

    async def admin_snapshot(self) -> dict[str, object]:
        items: list[dict[str, object]] = []
        for metadata in await self._all_metadata():
            sandbox_id = str(metadata["id"])
            running = await self._running(sandbox_id)
            items.append({
                "sandbox_id": sandbox_id, "name": self._name(sandbox_id),
                "runtime": metadata.get("runtime", "debian"),
                "purpose": metadata.get("purpose", "task"),
                "owner": metadata.get("owner_ref", ""),
                "owner_hash": metadata.get("owner_hash", ""),
                "status": "Up (container)" if running else "Exited",
                "running": running, "cpu_percent": "-", "memory_usage": "-",
                "memory_percent": "-", "disk_allocated_bytes": 0,
                "workspace_files": [],
                "workspace_error": "", "activities": [
                    {"command": item.command, "started_at": item.started_at, "status": item.status}
                    for item in self._active_execs.values() if item.sandbox_id == sandbox_id
                ], "last_activity": None,
            })
        return {"items": items, "active_commands": len(self._active_execs), "backend": self.backend}


class ContainerSandboxManager(WorkspaceSandboxManager):
    backend = "container"

    def __init__(self, *, root: str, broker: str, **options) -> None:
        super().__init__(root=root, **options)
        self.broker = broker

    async def _broker(self, request, stdin=b"", *, timeout=60):
        from src.sandbox_runtime.source_fence import assert_sandbox_source

        await assert_sandbox_source()
        if stdin:
            request = {**request, "stdin": len(stdin)}
        try:
            async with asyncio.timeout(timeout + 60):
                reader, writer = await asyncio.open_unix_connection(self.broker, limit=1 << 20)
                try:
                    writer.write(json.dumps(request).encode() + b"\n" + stdin)
                    await writer.drain()
                    header = json.loads(await reader.readuntil(b"\n"))
                    out = await reader.readexactly(int(header.get("stdout", 0)))
                    err = await reader.readexactly(int(header.get("stderr", 0)))
                finally:
                    writer.close()
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise SandboxError("容器管理服务不可用。") from exc
        except TimeoutError as exc:
            raise SandboxError("容器管理服务响应超时，结果不确定。") from exc
        if header.get("error"):
            raise SandboxError(f"容器操作失败：{header['error']}")
        return header, out, err

    def _ops(self, sandbox_id: str) -> bool:
        return self._operations_sandbox(sandbox_id)

    async def _running(self, sandbox_id: str) -> bool:
        header, _, _ = await self._broker({"op": "status", "sid": sandbox_id}, timeout=20)
        return bool(header.get("active"))

    async def _start(self, sandbox_id: str) -> None:
        header, _, err = await self._broker(
            {"op": "start", "sid": sandbox_id, "ops": self._ops(sandbox_id)}, timeout=300,
        )
        if header.get("code"):
            raise SandboxError("容器启动失败：" + err.decode(errors="replace")[-500:])

    async def _stop(self, sandbox_id: str) -> None:
        header, _, err = await self._broker({"op": "stop", "sid": sandbox_id}, timeout=120)
        if header.get("code"):
            raise SandboxError("容器停止失败：" + err.decode(errors="replace")[-500:])

    async def _remove(self, sandbox_id: str) -> None:
        await self._broker({"op": "remove", "sid": sandbox_id})

    async def _guest_exec_raw(
        self, sandbox_id: str, path: str, args: list[str], *, timeout: int = 60,
        input_bytes: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        header, out, err = await self._broker(
            {"op": "exec", "sid": sandbox_id, "argv": [path, *args], "timeout": max(1, int(timeout))},
            input_bytes or b"", timeout=timeout + 30,
        )
        return int(header.get("code", -1)), out, err

    async def _wait_guest(self, sandbox_id: str, *, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                code, _, _ = await self._guest_exec_raw(sandbox_id, "/bin/sh", ["-c", "true"], timeout=10)
                if code == 0:
                    return
            except SandboxError:
                pass
            await asyncio.sleep(1)
        raise SandboxError("容器启动超时。")
