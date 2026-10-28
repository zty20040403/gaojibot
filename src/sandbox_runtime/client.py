"""Bot-side client. No Docker socket, credentials or arbitrary host path API."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import os
from pathlib import Path
import time
import uuid

from .docker import DockerSandboxManager, SandboxError, SandboxObservedManifest, SandboxResult
from .protocol import (HEADER_LIMIT, ID_PATTERN, TOKEN_PATTERN, VERSION, ProtocolError,
                       read_header, validate_request, write_frame)


class SandboxClient:
    def __init__(self, socket_path: str, token: str, *, max_file_bytes: int = 20 * 1024 * 1024, image: str = "runner"):
        if not Path(socket_path).is_absolute() or len(os.fsencode(socket_path)) > 100 or not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("Invalid sandbox runner address or credential")
        if not 1024 <= max_file_bytes <= 256 * 1024 * 1024:
            raise ValueError("Invalid sandbox transfer limit")
        self.socket_path, self._token = socket_path, token
        self.max_file_bytes, self.image = max_file_bytes, image

    async def request(self, method: str, args: dict, payload: bytes = b"", *, request_id: str | None = None, eligible=None):
        identifier = request_id or uuid.uuid4().hex
        header = {"schema": VERSION, "id": identifier, "token": self._token, "method": method,
                  "args": args, "payload_size": len(payload)}
        validate_request(header, file_limit=self.max_file_bytes)
        # One retry uses the same durable request ID, never a fresh operation.
        for attempt in range(2):
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.socket_path, limit=HEADER_LIMIT), timeout=5)
                await write_frame(writer, header, payload)
                deadline, callbacks = time.monotonic() + 720, 0
                while True:
                    response = await read_header(reader, timeout=max(0.01, deadline - time.monotonic()))
                    if response.get("schema") != VERSION or response.get("id") != identifier:
                        raise ProtocolError("沙盒通道响应不匹配。")
                    if response.get("callback") == "eligible":
                        callbacks += 1
                        if callbacks > 4:
                            raise ProtocolError("沙盒回收校验次数异常，已停止回收。")
                        nonce = response.get("nonce")
                        if not isinstance(nonce, str) or not ID_PATTERN.fullmatch(nonce) or response.get("payload_size") != 0:
                            raise ProtocolError("沙盒回收校验消息不正确。")
                        allowed = False
                        if method == "reclaim_stopped" and eligible is not None:
                            allowed = eligible()
                            if inspect.isawaitable(allowed):
                                allowed = await allowed
                        await write_frame(writer, {"schema": VERSION, "id": identifier,
                            "nonce": nonce, "eligible": allowed is True, "payload_size": 0})
                        continue
                    size = response.get("payload_size")
                    if type(size) is not int or not 0 <= size <= self.max_file_bytes or type(response.get("ok")) is not bool:
                        raise ProtocolError("沙盒响应字段不正确。")
                    if not response["ok"]:
                        raise SandboxError(str(response.get("error") or "沙盒操作失败。")[:1000])
                    body = await asyncio.wait_for(reader.readexactly(size), timeout=60)
                    return response.get("data"), body
            except asyncio.CancelledError:
                if method != "cancel":
                    with suppress(Exception):
                        await asyncio.wait_for(self.request("cancel", {"request_id": identifier}), timeout=10)
                raise
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                if attempt:
                    raise SandboxError("沙盒通道暂时断开，执行状态尚未确认；请先检查沙盒，不要直接重复操作。") from None
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), timeout=5)
                    except (OSError, asyncio.TimeoutError):
                        pass

    async def health(self):
        return (await self.request("health", {}))[0]

    async def project_browser(self, operation, owner, project_id, *, generation, fence,
                              request_id=None, **parameters):
        """Caller persists launch/session/sequence before dispatch, then result before ack.

        An absent status is not permission to repeat an uncertain launch with a
        fresh ID. Replay open with its original launch_id or reconcile it first.
        """
        if operation not in {"open", "status", "execute", "receive", "ack", "drain"}:
            raise ProtocolError("Unsupported project browser operation")
        if operation == "open" and request_id is None:
            request_id = parameters.get("launch_id")
        return (await self.request("project_browser_" + operation, {
            "owner": owner, "project_id": project_id, "generation": generation,
            "fence": fence, **parameters,
        }, request_id=request_id))[0]

    async def project_rebuild(self, operation, owner, project_id, *, generation,
                              fence, reservation, manifest_sha256, request_id=None):
        if operation not in {"run", "status"}:
            raise ProtocolError("Unsupported project rebuild operation")
        return (await self.request("project_rebuild_" + operation, {
            "owner": owner, "project_id": project_id, "generation": generation,
            "fence": fence, "reservation": reservation, "manifest_sha256": manifest_sha256,
        }, request_id=request_id))[0]

    async def project_volume(self, operation, owner, project_id, *, fence=None, quota_bytes=None, request_id=None, generation=1, reservation=None):
        if operation not in {'create', 'status', 'mount', 'unmount', 'resize', 'fsck', 'trim', 'delete'}:
            raise ProtocolError('Unsupported project-volume operation')
        args = {'owner': owner, 'project_id': project_id, 'generation': generation}
        if fence is not None:
            args['fence'] = fence
        if quota_bytes is not None:
            args['quota_bytes'] = quota_bytes
        if reservation is not None:
            args['reservation'] = reservation
        return (await self.request('project_volume_' + operation, args, request_id=request_id))[0]

    async def create(self, owner, runtime="python", *, purpose="task"):
        return (await self.request("create", {"owner": owner, "runtime": runtime, "purpose": purpose}))[0]

    async def project_usage(self, owner, project_id, *, generation, rebuild_command=""):
        return (await self.request("project_usage", {"owner": owner, "project_id": project_id,
            "generation": generation, "rebuild_command": rebuild_command}))[0]

    async def project_notes(self, owner, project_id, *, generation, fence):
        return (await self.request("project_notes", {
            "owner": owner, "project_id": project_id, "generation": generation, "fence": fence,
        }))[0]

    async def project_workspace(self, operation, owner, project_id, *, generation, fence=None, request_id=None, reservation=None):
        if operation not in {"start", "stop", "remove", "status", "reserve", "release"}:
            raise ProtocolError("Unsupported project-workspace operation")
        args = {"owner": owner, "project_id": project_id, "generation": generation}
        if fence is not None:
            args["fence"] = fence
        if reservation is not None:
            args["reservation"] = reservation
        return (await self.request("project_workspace_" + operation, args, request_id=request_id))[0]

    async def ensure_default(self, owner, runtime="debian"):
        return (await self.request("ensure_default", {"owner": owner, "runtime": runtime}))[0]

    async def project_files(self, operation, owner, project_id, *, generation, fence,
                            reservation, request_id=None, payload=b"", **parameters):
        return await self.request("project_files_" + operation,
            {"owner": owner, "project_id": project_id, "generation": generation,
             "fence": fence, "reservation": reservation, **parameters},
            payload, request_id=request_id)

    async def list(self, owner):
        return (await self.request("list", {"owner": owner}))[0]

    async def admin_snapshot(self):
        return (await self.request("admin_snapshot", {}))[0]

    async def destroy(self, owner, sandbox_id):
        await self.request("destroy", {"owner": owner, "sandbox_id": sandbox_id})

    async def start_owned(self, owner, sandbox_id):
        await self.request("start_owned", {"owner": owner, "sandbox_id": sandbox_id})

    async def stop_owned(self, owner, sandbox_id):
        await self.request("stop_owned", {"owner": owner, "sandbox_id": sandbox_id})

    async def exec(self, owner, sandbox_id, command, timeout_seconds=None, packages=None):
        data, _ = await self.request("exec", {"owner": owner, "sandbox_id": sandbox_id, "command": command,
            "timeout_seconds": timeout_seconds, "packages": list(packages) if packages is not None else None})
        try:
            manifest = data["manifest"]
            if manifest is not None:
                manifest["changed_workspace_paths"] = tuple(manifest["changed_workspace_paths"])
                manifest["container_diff"] = tuple(manifest["container_diff"])
                manifest = SandboxObservedManifest(**manifest)
            return SandboxResult(data["stdout"], data["stderr"], data["returncode"], manifest)
        except (KeyError, TypeError):
            raise ProtocolError("沙盒执行回执格式不正确。") from None

    async def search_nix_packages(self, query):
        return (await self.request("search_nix_packages", {"query": query}))[0]

    async def write_file(self, owner, sandbox_id, path, content, *, allow_large=False):
        return (await self.request("write_file", {"owner": owner, "sandbox_id": sandbox_id,
            "path": path, "allow_large": allow_large}, content))[0]

    async def install_readonly_file(self, owner, sandbox_id, path, content):
        await self.request("install_readonly_file", {"owner": owner, "sandbox_id": sandbox_id, "path": path}, content)

    async def read_file(self, owner, sandbox_id, path, max_bytes=64 * 1024):
        return (await self.request("read_file", {"owner": owner, "sandbox_id": sandbox_id, "path": path, "max_bytes": max_bytes}))[1]

    async def export_artifact(self, owner, sandbox_id, path):
        data, payload = await self.request("export_artifact", {"owner": owner, "sandbox_id": sandbox_id, "path": path})
        return payload, data["directory"]

    async def reclaim_stopped(self, owner, sandbox_id, *, eligible, not_used_since=None):
        if not await DockerSandboxManager._eligible(eligible):
            raise SandboxError("任务状态已变化，已取消自动回收。")
        await self.request("reclaim_stopped", {"owner": owner, "sandbox_id": sandbox_id,
            "not_used_since": not_used_since}, eligible=eligible)
