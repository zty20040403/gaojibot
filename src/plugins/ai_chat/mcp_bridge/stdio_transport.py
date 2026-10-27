"""Bounded SDK transport for operator-pinned, ephemeral OCI MCP processes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import json
from pathlib import Path
import re
import secrets
import subprocess
import time

import anyio
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from .contracts import SERVER_NAME, server_config
from .http_transport import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_SESSION_BYTES, McpTransportDenied

OWNER_LABEL = "io.gaoji.mcp.owner"
EXPIRY_LABEL = "io.gaoji.mcp.expires"
CONTAINER_PREFIX = "gaoji-mcp-"
MAX_LIFETIME = 150


@dataclass(frozen=True)
class StdioProfile:
    image: str
    command: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.image, str) or not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[a-f0-9]{64}", self.image):
            raise ValueError("MCP stdio requires an immutable, preloaded image digest")
        if (not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 32
                or any(not isinstance(value, str) or not 1 <= len(value) <= 2048 or "\x00" in value for value in self.command)):
            raise ValueError("MCP stdio requires an operator-defined executable and arguments")


class DockerLauncher:
    def __init__(self, binary: str, socket_path: str, owner: str):
        if not Path(binary).is_absolute() or not Path(socket_path).is_absolute():
            raise ValueError("MCP Docker binary and Unix socket must be explicit absolute paths")
        if not re.fullmatch(r"[a-f0-9]{16,64}", owner):
            raise ValueError("Invalid MCP installation owner")
        self.binary, self.socket_path, self.owner = binary, socket_path, owner

    @property
    def prefix(self):
        return [self.binary, "--host", "unix://" + self.socket_path]

    @staticmethod
    def environment():
        # No Bot credentials, Docker auth/config, proxy settings or user home.
        return {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "DOCKER_CONFIG": "/nonexistent", "LANG": "C.UTF-8"}

    async def command(self, args: list[str], *, timeout: float = 5, limit: int = 65536) -> bytes:
        process = await asyncio.create_subprocess_exec(*self.prefix, *args, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.environment(), limit=8192)
        try:
            async with asyncio.timeout(timeout):
                output = bytearray()
                while chunk := await process.stdout.read(8192):
                    if len(output) + len(chunk) > limit:
                        raise McpTransportDenied("MCP Docker response exceeds its limit")
                    output.extend(chunk)
                if await process.wait() != 0:
                    raise McpTransportDenied("MCP isolated launcher is unavailable")
                return bytes(output)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            with anyio.CancelScope(shield=True), anyio.move_on_after(3):
                await process.wait()

    async def verify(self, profile: StdioProfile):
        raw = await self.command(["image", "inspect", "--format", "{{json .Config.Volumes}}", profile.image])
        # An image-declared VOLUME would create persistent anonymous writable storage.
        if json.loads(raw) not in (None, {}):
            raise McpTransportDenied("MCP images cannot declare persistent volumes")

    def arguments(self, profile: StdioProfile, name: str, expires_at: int) -> list[str]:
        return [*self.prefix, "run", "--rm", "-i", "--pull=never", "--name", name,
            "--label", OWNER_LABEL + "=" + self.owner, "--label", EXPIRY_LABEL + "=" + str(expires_at),
            "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--cpus=1", "--memory=512m", "--memory-swap=512m",
            "--pids-limit=64", "--ulimit=nofile=128:128", "--log-driver=none", "--init",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777", "--env=HOME=/tmp",
            "--entrypoint", profile.command[0], profile.image, *profile.command[1:]]

    async def remove(self, name: str):
        if not re.fullmatch(CONTAINER_PREFIX + r"[a-f0-9]{32}", name):
            raise ValueError("Invalid MCP container name")
        try:
            raw = await self.command(["inspect", "--format", "{{json .Config.Labels}}", name])
        except McpTransportDenied:
            # Already removed (--rm), or daemon unavailable. Reaper retries after expiry.
            return
        if json.loads(raw).get(OWNER_LABEL) != self.owner:
            raise McpTransportDenied("MCP cannot remove another installation's container")
        await self.command(["rm", "--force", name])

    async def reap(self, *, now: int | None = None):
        raw = await self.command(["ps", "--all", "--filter", "label=" + OWNER_LABEL + "=" + self.owner,
                                  "--format", "{{.Names}}"])
        names = raw.decode("ascii").splitlines()
        if len(names) > 64:
            raise McpTransportDenied("MCP container cleanup inventory requires operator attention")
        timestamp = int(time.time()) if now is None else now
        for name in names:
            if not re.fullmatch(CONTAINER_PREFIX + r"[a-f0-9]{32}", name):
                continue
            try:
                labels = json.loads(await self.command(["inspect", "--format", "{{json .Config.Labels}}", name]))
                expiry = labels.get(EXPIRY_LABEL, "")
                if labels.get(OWNER_LABEL) == self.owner and str(expiry).isdecimal() and int(expiry) <= timestamp:
                    await self.remove(name)
            except McpTransportDenied:
                continue


@asynccontextmanager
async def bounded_stdio(process):
    """Public SDK Transport protocol, with hard limits before JSON decoding.

    SDK stdio_client currently buffers an arbitrarily long unterminated line.
    The framing here retains official SessionMessage parsing and Client dispatch.
    The caller owns process/container termination.
    """
    incoming, reader = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    writer, outgoing = anyio.create_memory_object_stream[SessionMessage](0)

    async def receive():
        total, pending = 0, bytearray()
        async with incoming:
            try:
                while chunk := await process.stdout.read(8192):
                    total += len(chunk)
                    if total > MAX_SESSION_BYTES:
                        raise McpTransportDenied("MCP stdio session exceeds its byte limit")
                    pending.extend(chunk)
                    while (end := pending.find(b"\n")) >= 0:
                        if end > MAX_RESPONSE_BYTES:
                            raise McpTransportDenied("MCP stdio message exceeds its byte limit")
                        line = bytes(pending[:end])
                        del pending[:end + 1]
                        if not line.strip():
                            continue
                        try:
                            message = jsonrpc_message_adapter.validate_json(line, by_name=False)
                        except ValueError:
                            raise McpTransportDenied("MCP stdio returned invalid JSON-RPC") from None
                        await incoming.send(SessionMessage(message))
                    if len(pending) > MAX_RESPONSE_BYTES:
                        raise McpTransportDenied("MCP stdio message exceeds its byte limit")
                if pending.strip():
                    raise McpTransportDenied("MCP stdio ended in an incomplete message")
            except (McpTransportDenied, OSError) as exc:
                await incoming.send(McpTransportDenied(str(exc)) if isinstance(exc, McpTransportDenied)
                                    else McpTransportDenied("MCP stdio connection closed"))

    async def send():
        async with outgoing:
            try:
                async for item in outgoing:
                    data = item.message.model_dump_json(by_alias=True, exclude_unset=True).encode() + b"\n"
                    if len(data) > MAX_REQUEST_BYTES:
                        raise McpTransportDenied("MCP stdio request exceeds its byte limit")
                    process.stdin.write(data)
                    await process.stdin.drain()
            except (OSError, McpTransportDenied):
                await incoming.aclose()

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(receive)
            tasks.start_soon(send)
            try:
                yield reader, writer
            finally:
                tasks.cancel_scope.cancel()
    finally:
        # Synchronous close also runs when an SDK task-group cancellation is active.
        for stream in (incoming, reader, writer, outgoing):
            stream.close()


class StdioSessions:
    def __init__(self, profiles: Mapping[str, StdioProfile], launcher: DockerLauncher):
        if len(profiles) > 32 or any(not SERVER_NAME.fullmatch(name) or not isinstance(profile, StdioProfile)
                                    for name, profile in profiles.items()):
            raise ValueError("Invalid MCP stdio profiles")
        self.profiles, self.launcher = dict(profiles), launcher

    @staticmethod
    def credential(config):
        return ""

    @asynccontextmanager
    async def connect(self, config: dict):
        config = server_config(config)
        profile = self.profiles.get(config.get("profile", ""))
        if config["transport"] != "stdio" or profile is None:
            raise McpTransportDenied("MCP isolated stdio profile is not installed")
        await self.launcher.verify(profile)
        name = CONTAINER_PREFIX + secrets.token_hex(16)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.launcher.arguments(profile, name, int(time.time()) + MAX_LIFETIME),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=self.launcher.environment(), limit=16384)
            async with asyncio.timeout(MAX_LIFETIME), bounded_stdio(process) as streams:
                yield streams
        finally:
            with anyio.CancelScope(shield=True), anyio.move_on_after(12):
                try:
                    await self.launcher.remove(name)
                finally:
                    if process is not None:
                        if process.stdin is not None:
                            process.stdin.close()
                        if process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.kill()
                        await process.wait()


class McpSessions:
    def __init__(self, http, stdio: StdioSessions | None = None):
        self.http, self.stdio = http, stdio

    def _select(self, config):
        config = server_config(config)
        selected = self.http if config["transport"] == "http" else self.stdio
        if selected is None:
            raise McpTransportDenied("MCP stdio runtime is not configured")
        return selected

    def credential(self, config):
        return self._select(config).credential(config)

    def connect(self, config):
        return self._select(config).connect(config)
