"""Dedicated, pinned SSH transport. No bot sandbox receives these credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import shlex
import time
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from src.ssh_ops_protocol import HOST, MAX_REQUEST, MAX_RESPONSE, PROTOCOL, catalog, parse_handle
from .ops import OpsError, OpsOperation, OpsResponse


def parse_targets(raw: str) -> dict[str, dict[str, str]]:
    targets = json.loads(raw or "{}")
    if not isinstance(targets, dict):
        raise ValueError("SSH targets must be a host map")
    for host, config in targets.items():
        if (not isinstance(host, str) or not HOST.fullmatch(host) or not isinstance(config, dict)
                or set(config) != {"destination", "helper"}):
            raise ValueError("Invalid SSH target configuration")
        destination, helper = config["destination"], config["helper"]
        if not isinstance(destination, str) or not re.fullmatch(
                r"[a-z_][a-z0-9_-]{0,63}@[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", destination):
            raise ValueError("SSH destination must be a fixed user@hostname")
        if not isinstance(helper, str) or not re.fullmatch(r"/[A-Za-z0-9_/.-]+", helper):
            raise ValueError("SSH helper must be a fixed absolute executable")
    return targets


class SSHOperationsClient:
    backend_name = "ssh"
    catalog_version = 2

    def __init__(self, targets: dict[str, dict[str, str]], *, known_hosts_file: str,
                 identity_file: str = "", ssh_binary: str = "ssh", writable: bool = False,
                 timeout_seconds: float = 25, concurrency: int = 4) -> None:
        self.targets = parse_targets(json.dumps(targets))
        if not known_hosts_file or not Path(known_hosts_file).is_absolute():
            raise ValueError("An absolute, administrator-pinned known_hosts file is required")
        if identity_file and not Path(identity_file).is_absolute():
            raise ValueError("SSH identity must be an absolute credential path")
        self.known_hosts_file = known_hosts_file
        self.identity_file = identity_file
        self.ssh_binary = ssh_binary
        self.writable = writable
        self.timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(max(1, min(concurrency, 16)))

    def authorization_binding(self) -> dict[str, Any]:
        # Identity rotation, host-key changes or target changes invalidate prior
        # approvals. File bytes are only hashed, never exposed to tools or logs.
        files = [self.known_hosts_file, *([self.identity_file] if self.identity_file else [])]
        return {"backend": PROTOCOL, "targets": self.targets, "writable": self.writable,
                "identity": [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in files]}

    async def close(self) -> None:
        pass

    async def catalog(self) -> dict[str, Any]:
        result = catalog()
        if not self.writable:
            result["operations"] = [d for d in result["operations"] if d["read_only"]]
        return result

    async def operations(self) -> tuple[OpsOperation, ...]:
        return tuple(OpsOperation(d["name"], d["params_schema"]) for d in
                     (await self.catalog())["operations"] if d["read_only"])

    def command(self, host: str) -> list[str]:
        target = self.targets.get(host)
        if target is None:
            raise OpsError("forbidden", "Host is outside the configured SSH inventory")
        argv = [self.ssh_binary, "-F", "/dev/null", "-T", "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes", f"-oUserKnownHostsFile={self.known_hosts_file}",
            "-oGlobalKnownHostsFile=/dev/null", "-oConnectTimeout=8", "-oConnectionAttempts=1",
            "-oServerAliveInterval=5", "-oServerAliveCountMax=2", "-oForwardAgent=no",
            "-oClearAllForwardings=yes", "-oPermitLocalCommand=no", "-oIdentityAgent=none",
            "-oIdentitiesOnly=yes"]
        if self.identity_file:
            argv.extend(["-i", self.identity_file])
        else:
            # Tailscale SSH can authorize the dedicated network identity directly.
            argv.append("-oIdentityFile=none")
        return [*argv, "--", target["destination"], "sudo -n -- " + shlex.quote(target["helper"])]

    async def _remote(self, host: str, request: dict[str, Any]) -> Any:
        body = json.dumps({"protocol": PROTOCOL, "host": host, **request}, ensure_ascii=False).encode()
        if len(body) > MAX_REQUEST:
            raise OpsError("request_too_large", "SSH request exceeds 256 KiB")
        async with self._slots:
            try:
                process = await asyncio.create_subprocess_exec(*self.command(host),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            except OSError as exc:
                raise OpsError("transport_unavailable", f"Cannot start SSH: {exc}", retryable=True) from exc

            async def read(stream: asyncio.StreamReader) -> bytes:
                value = bytearray()
                while chunk := await stream.read(16384):
                    value.extend(chunk)
                    if len(value) > MAX_RESPONSE:
                        raise OpsError("response_too_large", "SSH response exceeds its bounded limit")
                return bytes(value)

            readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    process.stdin.write(body)
                    await process.stdin.drain()
                    process.stdin.close()
                    stdout, stderr = await asyncio.gather(*readers)
                    code = await process.wait()
                if code == 255:
                    raise OpsError("transport_unavailable", "SSH failed: " + stderr.decode(errors="replace")[:500], retryable=True)
                try:
                    payload = json.loads(stdout)
                except ValueError:
                    raise OpsError("invalid_response", "SSH helper returned no valid receipt; inspect the original job", retryable=True) from None
                if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL or payload.get("host") != host:
                    raise OpsError("invalid_response", "SSH evidence belongs to a different target or protocol")
                if code or payload.get("ok") is not True:
                    raise OpsError(str(payload.get("code", "target_error")), str(payload.get("error", "Target helper failed"))[:1000])
                if "data" not in payload:
                    raise OpsError("invalid_response", "SSH helper omitted its receipt; inspect the original job")
                return payload["data"]
            except TimeoutError:
                raise OpsError("timeout", "SSH observation timed out; remote work may still be running", retryable=True) from None
            except OSError as exc:
                raise OpsError("transport_unavailable", "SSH connection interrupted; remote work may still be running", retryable=True) from exc
            finally:
                for task in readers:
                    task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                if process.returncode is None:
                    process.kill()
                # Drain the finite remaining pipe buffers after killing SSH. A
                # bare wait can deadlock when a cancelled reader paused its pipe.
                await process.communicate()

    async def execute(self, operation: str, params: dict[str, Any]) -> OpsResponse:
        if operation not in {d.name for d in await self.operations()}:
            raise OpsError("forbidden", "Observation endpoint does not permit mutations")
        return await self.call(operation, params)

    async def call(self, operation: str, params: dict[str, Any], *,
                   idempotency_key: str | None = None) -> OpsResponse:
        definition = next((d for d in (await self.catalog())["operations"] if d["name"] == operation), None)
        if definition is None:
            raise OpsError("unsupported", "This SSH backend does not support the requested operation")
        try:
            Draft202012Validator(definition["params_schema"]).validate(params)
        except ValidationError as exc:
            raise OpsError("invalid_request", exc.message[:500]) from None
        started = time.monotonic()
        if operation in {"fleet.overview", "units.failed"}:
            async def observe(host: str) -> dict[str, Any]:
                try:
                    return await self._remote(host, {"op": operation, "params": {}})
                except (OpsError, OSError) as exc:
                    return {"host": host, "status": "unknown", "error": str(exc)[:500]}
            hosts = await asyncio.gather(*(observe(host) for host in self.targets))
            data = {"hosts": hosts, "observed_at": int(time.time()),
                "partial": any(h.get("status") == "unknown" for h in hosts)}
        else:
            host = params.get("host")
            if operation.startswith("jobs.") and operation != "jobs.list":
                try:
                    host, _ = parse_handle(params["job_id"])
                except ValueError as exc:
                    raise OpsError("legacy_job", str(exc)) from None
            data = await self._remote(host, {"op": operation, "params": params, "idempotency_key": idempotency_key})
        return OpsResponse(data, int((time.monotonic() - started) * 1000))
