"""KVM-backed task sandboxes using per-task disks and the QEMU guest agent."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Callable
from xml.etree import ElementTree

from .sandbox import (
    SANDBOX_ID_PATTERN,
    SandboxError,
    SandboxExecutionActivity,
    SandboxObservedManifest,
    SandboxResult,
)


PACKAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,79}$")
VM_PREFIX = "gaoji-vm-"
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


class VmSandboxManager:
    backend = "vm"

    def __init__(
        self,
        *,
        image: str,
        root: str,
        max_per_owner: int = 2,
        max_total: int = 8,
        default_timeout_seconds: int = 120,
        max_output_chars: int = 12000,
        max_file_bytes: int = 20 * 1024 * 1024,
        memory_mib: int = 4096,
        vcpus: int = 1,
        disk_gib: int = 20,
    ) -> None:
        self.image = Path(image)
        self.root = Path(root)
        self.max_per_owner = max(1, max_per_owner)
        self.max_total = max(1, max_total)
        self.default_timeout_seconds = max(5, default_timeout_seconds)
        self.max_output_chars = max(1000, max_output_chars)
        self.max_file_bytes = max(0, max_file_bytes)
        self.memory_mib = min(max(memory_mib, 512), 8192)
        self.vcpus = min(max(vcpus, 1), 4)
        self.disk_gib = min(max(disk_gib, 4), 100)
        self._exec_locks: dict[str, asyncio.Lock] = {}
        self._create_lock = asyncio.Lock()
        self._active_execs: dict[str, SandboxExecutionActivity] = {}
        self._last_execs: dict[str, SandboxExecutionActivity] = {}

    @staticmethod
    def _owner_hash(owner: str) -> str:
        return hashlib.sha256(owner.encode()).hexdigest()[:16]

    @staticmethod
    def _owner_ref(owner: str) -> str:
        return owner[:100]

    @staticmethod
    def _name(sandbox_id: str) -> str:
        return VM_PREFIX + sandbox_id

    def _directory(self, sandbox_id: str) -> Path:
        if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
            raise SandboxError("沙盒 ID 格式错误。")
        return self.root / sandbox_id

    def _save_metadata(self, sandbox_id: str, data: dict[str, object]) -> None:
        directory = self._directory(sandbox_id)
        temporary = directory / (".metadata-" + secrets.token_hex(8))
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "metadata.json")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _workspace_path(path: str) -> str:
        source = PurePosixPath(path)
        if source.is_absolute():
            try:
                source = source.relative_to("/workspace")
            except ValueError as exc:
                raise SandboxError("文件只能位于 /workspace。") from exc
        if not source.parts or any(part in {"", ".", ".."} for part in source.parts):
            raise SandboxError("文件路径无效。")
        return str(PurePosixPath("/workspace") / source)

    async def _run(self, *args: str, timeout: int = 60) -> tuple[int, bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SandboxError(f"主机命令超时：{args[0]}") from exc
        return process.returncode, stdout, stderr

    async def _checked(self, *args: str, timeout: int = 60) -> str:
        code, out, err = await self._run(*args, timeout=timeout)
        if code:
            raise SandboxError((err or out).decode(errors="replace")[-500:])
        return out.decode(errors="replace").strip()

    async def _virsh(self, *args: str, timeout: int = 30) -> str:
        return await self._checked("virsh", "-c", "qemu:///session", *args, timeout=timeout)

    async def _agent(self, sandbox_id: str, request: dict[str, object], *, timeout: int = 20) -> object:
        answer = await self._virsh(
            "qemu-agent-command", self._name(sandbox_id),
            json.dumps(request, separators=(",", ":")), "--timeout", str(timeout),
            timeout=timeout + 5,
        )
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError as exc:
            raise SandboxError("VM 客体代理返回无效数据。") from exc
        if "error" in parsed:
            raise SandboxError(f"VM 客体代理失败：{parsed['error']}")
        return parsed.get("return", {})

    async def _guest_exec(
        self, sandbox_id: str, path: str, args: list[str], *, timeout: int = 60,
        input_bytes: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        arguments: dict[str, object] = {"path": path, "arg": args, "capture-output": True}
        if input_bytes is not None:
            arguments["input-data"] = base64.b64encode(input_bytes).decode("ascii")
        started = await self._agent(sandbox_id, {"execute": "guest-exec", "arguments": arguments})
        if not isinstance(started, dict):
            raise SandboxError("VM 没有返回命令状态。")
        pid = started.get("pid")
        if not isinstance(pid, int):
            raise SandboxError("VM 没有返回命令编号。")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await self._agent(sandbox_id, {
                "execute": "guest-exec-status", "arguments": {"pid": pid},
            })
            if not isinstance(status, dict):
                raise SandboxError("VM 命令状态无效。")
            if status.get("exited"):
                return (
                    int(status.get("exitcode", -1)),
                    base64.b64decode(status.get("out-data", "")),
                    base64.b64decode(status.get("err-data", "")),
                )
            await asyncio.sleep(0.5)
        # The guest-side timeout wrapper is the authority for cancelling user code.
        raise SandboxError(f"VM 命令超过 {timeout} 秒，执行结果不确定。")

    async def _wait_guest(self, sandbox_id: str, *, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await self._agent(sandbox_id, {"execute": "guest-ping"}, timeout=5)
                return
            except (SandboxError, asyncio.TimeoutError):
                await asyncio.sleep(3)
        raise SandboxError("VM 启动超时，客体代理未就绪。")

    def _domain_xml(self, sandbox_id: str, directory: Path) -> str:
        domain = ElementTree.Element("domain", {"type": "kvm"})
        ElementTree.SubElement(domain, "name").text = self._name(sandbox_id)
        ElementTree.SubElement(domain, "memory", {"unit": "MiB"}).text = str(self.memory_mib)
        ElementTree.SubElement(domain, "vcpu").text = str(self.vcpus)
        os_node = ElementTree.SubElement(domain, "os")
        ElementTree.SubElement(os_node, "type", {"arch": "x86_64"}).text = "hvm"
        devices = ElementTree.SubElement(domain, "devices")
        emulator = shutil.which("qemu-system-x86_64")
        if not emulator:
            raise SandboxError("宿主机缺少 QEMU。")
        ElementTree.SubElement(devices, "emulator").text = emulator
        for file, device, target, bus, fmt in [
            ("disk.qcow2", "disk", "vda", "virtio", "qcow2"),
            ("seed.iso", "cdrom", "sda", "sata", "raw"),
        ]:
            disk = ElementTree.SubElement(devices, "disk", {"type": "file", "device": device})
            ElementTree.SubElement(disk, "driver", {"name": "qemu", "type": fmt})
            ElementTree.SubElement(disk, "source", {"file": str(directory / file)})
            ElementTree.SubElement(disk, "target", {"dev": target, "bus": bus})
            if device == "cdrom":
                ElementTree.SubElement(disk, "readonly")
        network = ElementTree.SubElement(devices, "interface", {"type": "user"})
        ElementTree.SubElement(network, "model", {"type": "virtio"})
        channel = ElementTree.SubElement(devices, "channel", {"type": "unix"})
        ElementTree.SubElement(channel, "source", {
            "mode": "bind", "path": str(directory / "agent.sock"),
        })
        ElementTree.SubElement(channel, "target", {
            "type": "virtio", "name": "org.qemu.guest_agent.0",
        })
        serial = ElementTree.SubElement(devices, "serial", {"type": "file"})
        ElementTree.SubElement(serial, "source", {"path": str(directory / "console.log")})
        ElementTree.SubElement(serial, "target", {"port": "0"})
        return ElementTree.tostring(domain, encoding="unicode")

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
        code, out, _ = await self._run(
            "virsh", "-c", "qemu:///session", "domstate", self._name(sandbox_id), timeout=15,
        )
        return code == 0 and out.strip() == b"running"

    async def _defined(self, sandbox_id: str) -> bool:
        code, _, _ = await self._run(
            "virsh", "-c", "qemu:///session", "dominfo", self._name(sandbox_id), timeout=15,
        )
        return code == 0

    async def create(self, owner: str, runtime: str = "python", *, purpose: str = "task") -> dict[str, str]:
        if runtime not in {"python", "node", "debian"}:
            raise SandboxError("不支持的运行环境。")
        async with self._create_lock:
            existing = await self._all_metadata()
            running = [item for item in existing if await self._running(str(item["id"]))]
            if len(running) >= self.max_total:
                raise SandboxError("VM 沙盒总数已达到上限。")
            if sum(item.get("owner_hash") == self._owner_hash(owner) for item in running) >= self.max_per_owner:
                raise SandboxError("你正在运行的 VM 沙盒已达到上限。")
            if not self.image.is_file():
                raise SandboxError("VM 基础镜像不可用。")
            sandbox_id = "s" + secrets.token_hex(3)
            directory = self._directory(sandbox_id)
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            metadata = {
                "id": sandbox_id, "owner_hash": self._owner_hash(owner),
                "owner_ref": self._owner_ref(owner), "runtime": runtime,
                "purpose": "shell" if purpose == "shell" else "task",
                "created_at": int(time.time()), "last_started": int(time.time()),
                "state": "creating",
            }
            self._save_metadata(sandbox_id, metadata)
            defined = started = False
            try:
                await self._checked(
                    "qemu-img", "create", "-f", "qcow2", "-b", str(self.image),
                    "-F", "qcow2", str(directory / "disk.qcow2"), f"{self.disk_gib}G",
                )
                (directory / "user-data").write_text("""#cloud-config
package_update: true
packages:
  - qemu-guest-agent
  - python3
  - git
  - curl
  - zip
  - unzip
users:
  - default
  - name: sandbox
    uid: 1000
    shell: /bin/bash
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
  - [mkdir, -p, /workspace]
  - [chown, sandbox:sandbox, /workspace]
""", encoding="utf-8")
                (directory / "meta-data").write_text(
                    f"instance-id: {self._name(sandbox_id)}\n"
                    f"local-hostname: {self._name(sandbox_id)}\n", encoding="utf-8",
                )
                await self._checked(
                    "cloud-localds", str(directory / "seed.iso"),
                    str(directory / "user-data"), str(directory / "meta-data"),
                )
                (directory / "domain.xml").write_text(
                    self._domain_xml(sandbox_id, directory), encoding="utf-8",
                )
                await self._virsh("define", str(directory / "domain.xml"))
                defined = True
                await self._virsh("start", self._name(sandbox_id))
                started = True
                await self._wait_guest(sandbox_id)
                code, _, err = await self._guest_exec(
                    sandbox_id, "/bin/sh", ["-lc", "cloud-init status --wait"], timeout=180,
                )
                if code:
                    raise SandboxError(f"VM 初始化失败：{err.decode(errors='replace')[-500:]}")
                metadata["state"] = "ready"
                self._save_metadata(sandbox_id, metadata)
                return {
                    "sandbox_id": sandbox_id, "runtime": runtime,
                    "image": "debian-13-kvm", "status": "running",
                    "purpose": str(metadata["purpose"]), "toolset": "vm",
                }
            except (Exception, asyncio.CancelledError):
                clean = True
                if started:
                    code, _, _ = await self._run(
                        "virsh", "-c", "qemu:///session", "destroy", self._name(sandbox_id),
                    )
                    clean = code == 0
                if defined:
                    code, _, _ = await self._run(
                        "virsh", "-c", "qemu:///session", "undefine", self._name(sandbox_id),
                    )
                    clean = clean and code == 0
                if clean:
                    shutil.rmtree(directory)
                raise

    async def list(self, owner: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in await self._all_metadata():
            if item.get("owner_hash") != self._owner_hash(owner):
                continue
            sandbox_id = str(item["id"])
            result.append({
                "sandbox_id": sandbox_id, "name": self._name(sandbox_id),
                "runtime": str(item["runtime"]), "purpose": str(item["purpose"]),
                "status": "Up (VM)" if await self._running(sandbox_id) else "Exited",
            })
        return result

    async def ensure_default(self, owner: str, runtime: str = "debian") -> dict[str, str]:
        for item in await self.list(owner):
            if item.get("purpose") == "shell":
                if not item["status"].startswith("Up "):
                    await self.start_owned(owner, item["sandbox_id"])
                return {**item, "status": "running", "toolset": "vm"}
        return await self.create(owner, runtime, purpose="shell")

    async def _owned_running(self, owner: str, sandbox_id: str) -> dict[str, object]:
        metadata = await self._metadata(owner, sandbox_id)
        if not await self._running(sandbox_id):
            raise SandboxError("VM 沙盒没有运行。")
        return metadata

    async def exec(
        self, owner: str, sandbox_id: str, command: str,
        timeout_seconds: int | None = None,
        packages: list[str] | tuple[str, ...] | None = None,
    ) -> SandboxResult:
        if not command.strip():
            raise SandboxError("命令不能为空。")
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            await self._owned_running(owner, sandbox_id)
            timeout = min(max(timeout_seconds or self.default_timeout_seconds, 1), 300)
            requested = list(packages or [])
            if len(requested) > 16 or any(not PACKAGE_PATTERN.fullmatch(item) for item in requested):
                raise SandboxError("VM 软件包名称或数量不合法。")
            if requested:
                package_command = "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends " + " ".join(map(shlex.quote, requested))
                code, _, err = await self._guest_exec(
                    sandbox_id, "/bin/sh", ["-lc", package_command], timeout=300,
                )
                if code:
                    raise SandboxError(f"VM 软件包安装失败：{err.decode(errors='replace')[-500:]}")
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
                        "-u", "sandbox", "--", "/usr/bin/timeout", "--signal=TERM",
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
                        changed_workspace_paths=changed_paths, container_diff=(), network_mode="user-nat",
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

    async def _guest_file(self, sandbox_id: str, method: str, arguments: dict[str, object]) -> object:
        return await self._agent(sandbox_id, {"execute": method, "arguments": arguments})

    async def write_file(
        self, owner: str, sandbox_id: str, path: str, content: bytes, *, allow_large: bool = False,
    ) -> int:
        return await self._write_content(owner, sandbox_id, path, content,
                                         allow_large=allow_large, readonly=False)

    async def _write_content(
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
            opened = await self._guest_file(
                sandbox_id, "guest-file-open", {"path": staged, "mode": "w"},
            )
            handle = opened if isinstance(opened, int) else None
            if not isinstance(handle, int):
                raise SandboxError("VM 无法打开临时文件。")
            try:
                for offset in range(0, len(content), 48 * 1024):
                    chunk = content[offset:offset + 48 * 1024]
                    response = await self._guest_file(sandbox_id, "guest-file-write", {
                        "handle": handle, "buf-b64": base64.b64encode(chunk).decode("ascii"),
                    })
                    if not isinstance(response, dict) or response.get("count") != len(chunk):
                        raise SandboxError("VM 文件写入不完整。")
                await self._guest_file(sandbox_id, "guest-file-flush", {"handle": handle})
            finally:
                await self._guest_file(sandbox_id, "guest-file-close", {"handle": handle})
            code, _, err = await self._guest_exec(
                sandbox_id, "/usr/bin/python3", [
                    "-c", INSTALL_FILE_SCRIPT, staged, target, "1" if readonly else "0",
                ], timeout=120,
            )
            if code:
                raise SandboxError(f"VM 文件导入失败：{err.decode(errors='replace')[-500:]}")
        finally:
            await self._guest_exec(sandbox_id, "/bin/rm", ["-f", staged])
        return len(content)

    async def _read_guest_file(self, sandbox_id: str, target: str, max_bytes: int | None) -> bytes:
        limit = self.max_file_bytes or None
        if max_bytes and max_bytes > 0:
            limit = min(limit, max_bytes) if limit else max_bytes
        code, out, _ = await self._guest_exec(
            sandbox_id, "/usr/bin/stat", ["-c", "%F:%s", target], timeout=20,
        )
        if code:
            raise SandboxError("文件不存在或无法读取。")
        try:
            kind, raw_size = out.decode().strip().split(":", 1)
            if kind != "regular file":
                raise SandboxError("只能读取普通文件，不能读取符号链接。")
            size = int(raw_size)
        except ValueError as exc:
            raise SandboxError("无法读取文件大小。") from exc
        if limit and size > limit:
            raise SandboxError(f"文件大小 {size} 字节，超过读取上限 {limit}。")
        opened = await self._guest_file(sandbox_id, "guest-file-open", {"path": target, "mode": "r"})
        handle = opened if isinstance(opened, int) else None
        if not isinstance(handle, int):
            raise SandboxError("VM 无法打开文件。")
        chunks: list[bytes] = []
        received = 0
        try:
            while True:
                response = await self._guest_file(sandbox_id, "guest-file-read", {
                    "handle": handle, "count": 48 * 1024,
                })
                if not isinstance(response, dict):
                    raise SandboxError("VM 文件读取状态无效。")
                chunk = base64.b64decode(response.get("buf-b64", ""))
                received += len(chunk)
                if limit and received > limit:
                    raise SandboxError("文件读取超过上限。")
                chunks.append(chunk)
                if response.get("eof") or not chunk:
                    break
        finally:
            await self._guest_file(sandbox_id, "guest-file-close", {"handle": handle})
        if received != size:
            raise SandboxError("文件在读取期间变化，请重试。")
        return b"".join(chunks)

    async def read_file(
        self, owner: str, sandbox_id: str, path: str, max_bytes: int | None = 64 * 1024,
    ) -> bytes:
        await self._owned_running(owner, sandbox_id)
        target = self._workspace_path(path)
        return await self._read_guest_file(sandbox_id, target, max_bytes)

    async def install_readonly_file(self, owner: str, sandbox_id: str, path: str, content: bytes) -> None:
        target = self._workspace_path(path)
        if not target.startswith("/workspace/upstream/"):
            raise SandboxError("只读交接只能写入 upstream 目录。")
        await self._write_content(owner, sandbox_id, path, content,
                                  allow_large=True, readonly=True)

    async def export_artifact(self, owner: str, sandbox_id: str, path: str) -> tuple[bytes, bool]:
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
            return await self.read_file(owner, sandbox_id, path, max_bytes=None), False
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
        raise SandboxError("VM 使用 Debian 软件包；请用 sandbox_exec 的 packages 参数安装具体包名。")

    async def start_owned(self, owner: str, sandbox_id: str) -> None:
        async with self._create_lock:
            metadata = await self._metadata(owner, sandbox_id)
            if await self._running(sandbox_id):
                return
            existing = await self._all_metadata()
            running = [item for item in existing if await self._running(str(item["id"]))]
            if len(running) >= self.max_total:
                raise SandboxError("VM 沙盒总数已达到上限。")
            if sum(item.get("owner_hash") == self._owner_hash(owner) for item in running) >= self.max_per_owner:
                raise SandboxError("你正在运行的 VM 沙盒已达到上限。")
            if not await self._defined(sandbox_id):
                await self._virsh("define", str(self._directory(sandbox_id) / "domain.xml"))
            await self._virsh("start", self._name(sandbox_id))
            await self._wait_guest(sandbox_id)
            metadata["last_started"] = int(time.time())
            self._save_metadata(sandbox_id, metadata)

    async def stop_owned(self, owner: str, sandbox_id: str) -> None:
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            await self._metadata(owner, sandbox_id)
            if any(item.sandbox_id == sandbox_id for item in self._active_execs.values()):
                raise SandboxError("VM 正在执行命令，不能停止。")
            if not await self._running(sandbox_id):
                return
            code, _, err = await self._guest_exec(sandbox_id, "/usr/bin/sync", [], timeout=20)
            if code:
                raise SandboxError(f"VM 文件同步失败：{err.decode(errors='replace')[-500:]}")
            frozen = await self._agent(sandbox_id, {"execute": "guest-fsfreeze-freeze"}, timeout=20)
            if not isinstance(frozen, int) or frozen < 1:
                raise SandboxError("VM 文件系统冻结失败，保留运行状态。")
            try:
                await self._virsh("destroy", self._name(sandbox_id))
            except Exception:
                await self._agent(sandbox_id, {"execute": "guest-fsfreeze-thaw"}, timeout=20)
                raise

    async def destroy(self, owner: str, sandbox_id: str) -> None:
        await self._metadata(owner, sandbox_id)
        if any(item.sandbox_id == sandbox_id for item in self._active_execs.values()):
            raise SandboxError("VM 正在执行命令，不能销毁。")
        lock = self._exec_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            if await self._running(sandbox_id):
                await self._virsh("destroy", self._name(sandbox_id))
            if await self._defined(sandbox_id):
                await self._virsh("undefine", self._name(sandbox_id))
            shutil.rmtree(self._directory(sandbox_id))
        self._exec_locks.pop(sandbox_id, None)

    async def reclaim_stopped(
        self, owner: str, sandbox_id: str, *, eligible: Callable[[], bool],
        not_used_since: int | None = None,
    ) -> None:
        metadata = await self._metadata(owner, sandbox_id)
        if metadata.get("purpose") != "task" or await self._running(sandbox_id):
            raise SandboxError("VM 仍在使用，不能自动回收。")
        if not_used_since is not None and int(metadata.get("last_started", 0)) > not_used_since:
            raise SandboxError("VM 已在交付后重新启动，不能回收。")
        if not eligible():
            raise SandboxError("任务状态已变化，已取消回收。")
        await self.destroy(owner, sandbox_id)

    async def admin_snapshot(self) -> dict[str, object]:
        items: list[dict[str, object]] = []
        for metadata in await self._all_metadata():
            sandbox_id = str(metadata["id"])
            running = await self._running(sandbox_id)
            directory = self._directory(sandbox_id)
            disk_allocated_bytes = 0
            for filename in ("disk.qcow2", "seed.iso"):
                try:
                    disk_allocated_bytes += (directory / filename).stat().st_blocks * 512
                except OSError:
                    pass
            items.append({
                "sandbox_id": sandbox_id, "name": self._name(sandbox_id),
                "runtime": metadata.get("runtime", "debian"),
                "purpose": metadata.get("purpose", "task"),
                "owner": metadata.get("owner_ref", ""),
                "owner_hash": metadata.get("owner_hash", ""),
                "status": "Up (VM)" if running else "Exited",
                "running": running, "cpu_percent": "-", "memory_usage": "-",
                "memory_percent": "-", "disk_allocated_bytes": disk_allocated_bytes,
                "workspace_files": [],
                "workspace_error": "", "activities": [
                    {"command": item.command, "started_at": item.started_at, "status": item.status}
                    for item in self._active_execs.values() if item.sandbox_id == sandbox_id
                ], "last_activity": None,
            })
        return {"items": items, "active_commands": len(self._active_execs), "backend": self.backend}
