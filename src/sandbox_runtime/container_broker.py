"""Root-side broker for gaoji's NixOS containers (stdlib only).

The bot runs unprivileged. This small root service is the only way it can
start, stop or check a conversation container, run a command inside one, or
remove a stopped container's state. Each request names a sandbox id that
must match ``s[0-9a-f]{6}``; nothing else from the caller reaches a command
line except the argv run inside the container. Only the bot's user can reach
the socket (systemd sets its owner and mode).

Wire format, one exchange per connection:
  request:  one JSON line, then ``stdin`` bytes if the request says so
  response: one JSON line {"code", "stdout", "stderr", ...}, then the bytes
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

SANDBOX_ID = re.compile(r"^s[0-9a-f]{6}$")
STATE = Path("/var/lib/gaoji-ct")
MAX_HEADER = 64 * 1024
MAX_STDIN = 300 * 1024 * 1024
MAX_OUTPUT = 300 * 1024 * 1024
MAX_TIMEOUT = 1800


def machine(sandbox_id: str) -> str:
    return "gct-" + sandbox_id


def units(sandbox_id: str) -> tuple[str, str]:
    return f"gaoji-ct@{sandbox_id}.service", f"gaoji-ct-ops@{sandbox_id}.service"


class BrokerError(Exception):
    pass


def parse(header: dict) -> dict:
    op = header.get("op")
    sandbox_id = header.get("sid")
    if op not in {"status", "start", "stop", "remove", "exec"}:
        raise BrokerError("unknown_op")
    if type(sandbox_id) is not str or not SANDBOX_ID.fullmatch(sandbox_id):
        raise BrokerError("invalid_sandbox_id")
    request = {"op": op, "sid": sandbox_id}
    if op == "start":
        if type(header.get("ops", False)) is not bool:
            raise BrokerError("invalid_ops")
        request["ops"] = header.get("ops", False)
    if op == "exec":
        argv = header.get("argv")
        if (type(argv) is not list or not argv or len(argv) > 4096
                or any(type(item) is not str or "\0" in item for item in argv)
                or not argv[0].startswith("/")):
            raise BrokerError("invalid_argv")
        timeout = header.get("timeout", 60)
        stdin = header.get("stdin", 0)
        if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
            raise BrokerError("invalid_timeout")
        if type(stdin) is not int or not 0 <= stdin <= MAX_STDIN:
            raise BrokerError("invalid_stdin")
        request.update(argv=argv, timeout=timeout, stdin=stdin)
    return request


class Broker:
    def __init__(self, *, systemctl="systemctl", systemd_run="systemd-run", state=STATE):
        self.systemctl, self.systemd_run, self.state = systemctl, systemd_run, Path(state)

    async def _run(self, *argv, stdin=b"", timeout=60, limit=MAX_OUTPUT):
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        async def drain(stream):
            chunks, size = [], 0
            while chunk := await stream.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    process.kill()
                    raise BrokerError("output_limit")
                chunks.append(chunk)
            return b"".join(chunks)

        async def feed():
            if stdin:
                process.stdin.write(stdin)
                await process.stdin.drain()
            process.stdin.close()

        try:
            async with asyncio.timeout(timeout):
                _, out, err = await asyncio.gather(feed(), drain(process.stdout), drain(process.stderr))
                code = await process.wait()
        except (TimeoutError, BrokerError):
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        return code, out, err

    async def _active(self, unit):
        code, out, _ = await self._run(self.systemctl, "is-active", unit, timeout=15)
        return out.strip() == b"active"

    async def handle(self, request, stdin=b""):
        sandbox_id = request["sid"]
        plain, ops = units(sandbox_id)
        op = request["op"]
        if op == "status":
            plain_on, ops_on = await self._active(plain), await self._active(ops)
            return {"code": 0, "active": plain_on or ops_on, "ops": ops_on}, b"", b""
        if op == "start":
            want, other = (ops, plain) if request["ops"] else (plain, ops)
            if await self._active(other):
                await self._run(self.systemctl, "stop", other, timeout=120)
            code, out, err = await self._run(self.systemctl, "start", want, timeout=300)
            return {"code": code}, out, err
        if op == "stop":
            code, out, err = await self._run(self.systemctl, "stop", plain, ops, timeout=120)
            return {"code": code}, out, err
        if op == "remove":
            if await self._active(plain) or await self._active(ops):
                raise BrokerError("container_running")
            shutil.rmtree(self.state / sandbox_id, ignore_errors=True)
            return {"code": 0}, b"", b""
        # exec: a transient unit inside the container, killed by its own deadline.
        if not (await self._active(plain) or await self._active(ops)):
            raise BrokerError("container_not_running")
        code, out, err = await self._run(
            self.systemd_run, "--machine=" + machine(sandbox_id), "--quiet", "--pipe", "--wait",
            "--collect", "--service-type=exec", f"--property=RuntimeMaxSec={request['timeout']}",
            "--", *request["argv"], stdin=stdin, timeout=request["timeout"] + 30)
        return {"code": code}, out, err

    async def serve_connection(self, reader, writer):
        try:
            try:
                line = await reader.readuntil(b"\n")
                if len(line) > MAX_HEADER:
                    raise BrokerError("header_too_large")
                request = parse(json.loads(line))
                stdin = await reader.readexactly(request.get("stdin", 0)) if request.get("stdin") else b""
                header, out, err = await self.handle(request, stdin)
            except (BrokerError, ValueError, TypeError, asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError) as error:
                header, out, err = {"code": -1, "error": str(error) or type(error).__name__}, b"", b""
            except TimeoutError:
                header, out, err = {"code": -1, "error": "timeout"}, b"", b""
            header.update(stdout=len(out), stderr=len(err))
            writer.write(json.dumps(header).encode() + b"\n" + out + err)
            await writer.drain()
        finally:
            writer.close()


async def main(path=None):
    broker = Broker()
    if os.environ.get("LISTEN_FDS") == "1":
        server = await asyncio.start_unix_server(
            broker.serve_connection, sock=socket.socket(fileno=3), limit=MAX_HEADER + 1)
    else:
        server = await asyncio.start_unix_server(broker.serve_connection, path=path, limit=MAX_HEADER + 1)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
