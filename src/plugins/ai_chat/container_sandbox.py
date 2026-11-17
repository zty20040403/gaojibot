"""Conversation workspaces as NixOS containers instead of KVM guests.

Everything above process execution is VmSandboxManager's: ownership, locks,
metadata, exec bookkeeping, file staging scripts and conversation lifecycle.
This class swaps the primitives underneath: libvirt and the QEMU guest agent
become the root container broker (start, stop, status, exec). There is no
10 MiB guest-agent response limit and no agent channel to break.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shlex
import time
from pathlib import Path

from .sandbox import SandboxError
from .vm_sandbox import VM_PREFIX, VmSandboxManager

logger = logging.getLogger(__name__)
IMAGE = "nixos-container"


class ContainerSandboxManager(VmSandboxManager):
    backend = "container"

    def __init__(self, *, root: str, broker: str, **options) -> None:
        super().__init__(image="", root=root, **options)
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

    def _ops(self, sandbox_id):
        return self._operations_sandbox(sandbox_id)

    async def _running(self, sandbox_id: str) -> bool:
        header, _, _ = await self._broker({"op": "status", "sid": sandbox_id}, timeout=20)
        return bool(header.get("active"))

    async def _defined(self, sandbox_id: str) -> bool:
        return self._directory(sandbox_id).is_dir()

    async def _start(self, sandbox_id):
        header, _, err = await self._broker(
            {"op": "start", "sid": sandbox_id, "ops": self._ops(sandbox_id)}, timeout=300)
        if header.get("code"):
            raise SandboxError("容器启动失败：" + err.decode(errors="replace")[-500:])

    async def _stop(self, sandbox_id):
        header, _, err = await self._broker({"op": "stop", "sid": sandbox_id}, timeout=120)
        if header.get("code"):
            raise SandboxError("容器停止失败：" + err.decode(errors="replace")[-500:])

    async def _virsh(self, *args: str, timeout: int = 30) -> str:
        # The inherited lifecycle speaks virsh; map the verbs it uses.
        verb, target = args[0], args[1] if len(args) > 1 else ""
        sandbox_id = target[len(VM_PREFIX):] if target.startswith(VM_PREFIX) else Path(target).parent.name
        if verb == "start":
            await self._start(sandbox_id)
        elif verb == "destroy":
            await self._stop(sandbox_id)
        elif verb not in {"define", "undefine"}:
            raise SandboxError(f"容器后端不支持 {verb}。")
        return ""

    async def _guest_exec_raw(self, sandbox_id, path, args, *, timeout=60, input_bytes=None):
        header, out, err = await self._broker(
            {"op": "exec", "sid": sandbox_id, "argv": [path, *args], "timeout": max(1, int(timeout))},
            input_bytes or b"", timeout=timeout + 30)
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

    async def _write_content_locked(self, owner, sandbox_id, path, content, *, allow_large, readonly):
        from .vm_sandbox import INSTALL_FILE_SCRIPT
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
                timeout=120, input_bytes=content)
            if code:
                raise SandboxError(f"容器文件写入失败：{err.decode(errors='replace')[-500:]}")
            code, _, err = await self._guest_exec(
                sandbox_id, "/usr/bin/python3",
                ["-c", INSTALL_FILE_SCRIPT, staged, target, "1" if readonly else "0"], timeout=120)
            if code:
                raise SandboxError(f"容器文件导入失败：{err.decode(errors='replace')[-500:]}")
        finally:
            await self._guest_exec(sandbox_id, "/bin/rm", ["-f", staged])
        return len(content)

    async def _read_guest_file(self, sandbox_id, target, max_bytes):
        limit = self.max_file_bytes or None
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
        code, data, err = await self._guest_exec(sandbox_id, "/usr/bin/python3", ["-c",
            "import os,sys;fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW);"
            "f=os.fdopen(fd,'rb');sys.stdout.buffer.write(f.read())", target], timeout=300)
        if code:
            raise SandboxError("文件读取失败：" + err.decode(errors="replace")[-300:])
        if len(data) != size:
            raise SandboxError("文件在读取期间变化，请重试。")
        return data

    async def exec(self, owner, sandbox_id, command, timeout_seconds=None, packages=None):
        if packages:
            raise SandboxError("容器已预装常用工具；缺少的 Python/Node 包请用 pip/npm/uv 装到 /workspace。")
        return await super().exec(owner, sandbox_id, command, timeout_seconds=timeout_seconds)

    async def create(self, owner, runtime="python", *, purpose="task"):
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
                "purpose": "shell" if purpose == "shell" else "task", "backend": "container",
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
                return {"sandbox_id": sandbox_id, "runtime": runtime, "image": IMAGE, "status": "running",
                        "purpose": str(metadata["purpose"]), "toolset": "vm"}
            except (Exception, asyncio.CancelledError):
                try:
                    await self._conversation_event("failed", owner, sandbox_id)
                    if started:
                        await self._conversation_event("stopping", owner, sandbox_id)
                except Exception as error:
                    logger.warning("container failure bookkeeping error=%s", type(error).__name__)
                try:
                    await self._stop(sandbox_id)
                    await self._broker({"op": "remove", "sid": sandbox_id})
                    import shutil
                    shutil.rmtree(directory)
                except Exception as error:
                    logger.warning("container cleanup failed sandbox=%s error=%s", sandbox_id, type(error).__name__)
                raise

    async def stop_owned(self, owner, sandbox_id, *, if_idle=False):
        async with self.io_lock(sandbox_id):
            metadata = await self._metadata(owner, sandbox_id)
            if if_idle and self.program_state.keeps_awake(metadata):
                return
            if any(item.sandbox_id == sandbox_id for item in self._active_execs.values()):
                raise SandboxError("沙盒正在执行命令，不能停止。")
            if await self._conversation_event(
                    "stopping", owner, sandbox_id, **({"automatic": True} if if_idle else {})) is False:
                return
            if await self._running(sandbox_id):
                # /workspace is a host directory: stopping cleanly needs no freeze.
                await self._stop(sandbox_id)
            await self._conversation_event("stopped", owner, sandbox_id)

    async def destroy(self, owner, sandbox_id, *, if_unused=False):
        await super().destroy(owner, sandbox_id, if_unused=if_unused)
        if not self._directory(sandbox_id).exists():
            await self._broker({"op": "remove", "sid": sandbox_id})

    @property
    def conversation_image(self):
        return Path("/nonexistent/gaoji-container-image")

    async def prepare_conversation_image(self):
        return None

    async def search_nix_packages(self, query):
        raise SandboxError("容器已预装常用工具；缺少的包请用 pip/npm/uv 装到 /workspace。")
