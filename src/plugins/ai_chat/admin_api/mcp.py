"""Versioned external-tool administration; never export stored bearer secrets."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..admin_security import require_admin
from ..mcp_bridge.client import McpCallError
from ..mcp_bridge.contracts import server_config
from ..mcp_bridge.store import McpConflict, McpDenied
from .context import mutation_context, mutation_payload
from .contracts import AdminMutationContext


class Versioned(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True, ge=0)


class ServerUpdate(Versioned):
    config: dict
    scopes: list[str] = Field(min_length=1, max_length=50)
    enabled: StrictBool


class ToolUpdate(Versioned):
    enabled: StrictBool
    trust: Literal["approval", "safe"]
    server_version: int = Field(strict=True, ge=1)
    tool_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class TokenIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_key: str = Field(pattern=r"^onebot-v11:group:[1-9][0-9]{0,19}$")
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    label: str = Field(min_length=1, max_length=80)
    lifetime_seconds: int = Field(default=86400, strict=True, ge=60, le=30 * 86400)


def create_router(context):
    router = context.new_router()
    runtime = context.services.mcp_runtime
    store = runtime.store if runtime else None

    def access(mutation=None):
        account = require_admin()
        if store is None:
            raise HTTPException(status_code=503, detail="MCP 未启用")
        if mutation is not None and mutation.expected_version is None:
            raise HTTPException(status_code=428, detail="MCP 变更必须携带 If-Match 资源版本")
        return account

    async def write(mutation, action, target, operation, before=None):
        try:
            result = await asyncio.to_thread(context.mutate, mutation, "mcp", action=action,
                target=target, before=before, operation=lambda _version: operation())
            return result
        except McpConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except McpDenied:
            raise HTTPException(status_code=403, detail="MCP 权限已经失效") from None
        except McpCallError as exc:
            raise HTTPException(status_code=502, detail={"code": exc.code, "message": "无法读取外部 MCP 工具目录"}) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        finally:
            await asyncio.to_thread(context.event_broker.publish, "mcp", "audit")

    @router.get("/api/mcp")
    async def overview():
        require_admin()
        def read():
            return context.versioned("mcp", {"configured": runtime is not None,
                "servers": store.servers() if store else [], "tokens": store.tokens() if store else [],
                "stdio_profiles": sorted(runtime.config.profiles) if runtime else [],
                "credential_refs": sorted(runtime.sessions.http.policy.secrets) if runtime else [],
                "read_path": runtime.config.read_path if runtime else "",
                "read_ready": runtime.ready.is_set() if runtime else False})
        return await asyncio.to_thread(read)

    @router.get("/api/mcp/servers/{name}")
    async def server_detail(name: str):
        access()
        def read():
            server = store.server(name)
            if server is None:
                raise HTTPException(status_code=404, detail="MCP 服务器不存在")
            return context.versioned("mcp", {"server": server, "tools": store.tools(name)})
        return await asyncio.to_thread(read)

    @router.put("/api/mcp/servers/{name}")
    async def configure(name: str, body: ServerUpdate, mutation: AdminMutationContext = Depends(mutation_context)):
        account = access(mutation)
        def apply():
            config = server_config(body.config)
            if config["transport"] == "stdio" and config["profile"] not in runtime.config.profiles:
                raise ValueError("该隔离 stdio 配置尚未由运营者安装")
            return store.configure(account, name, **{**body.model_dump(), "config": config})
        result = await write(mutation, "mcp.configure", name, apply, await asyncio.to_thread(store.server, name))
        return mutation_payload(result, server=result.value)

    @router.post("/api/mcp/servers/{name}/refresh")
    async def refresh(name: str, body: Versioned, mutation: AdminMutationContext = Depends(mutation_context)):
        account = access(mutation)
        snapshot = await asyncio.to_thread(store.server, name)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="MCP 服务器不存在")
        if snapshot["version"] != body.expected_version:
            raise HTTPException(status_code=409, detail="MCP 服务器配置已更改")
        # Fail stale requests before network I/O as well as before committing.
        current = await asyncio.to_thread(context.control_store.version, "mcp")
        if current != mutation.expected_version:
            raise HTTPException(status_code=409, detail="MCP 控制台版本已更改")
        failure = None
        try:
            definitions = await runtime.service.wire.discover(snapshot["config"])
        except McpCallError as exc:
            failure = exc
            definitions = []
        def apply():
            if failure is not None:
                raise failure
            tools = store.replace_catalog(account, name, definitions, expected_version=body.expected_version)
            return {"server": store.server(name), "tools": tools}
        result = await write(mutation, "mcp.refresh", name, apply, snapshot)
        return mutation_payload(result, **result.value)

    @router.put("/api/mcp/servers/{name}/tools/{remote}")
    async def configure_tool(name: str, remote: str, body: ToolUpdate,
                             mutation: AdminMutationContext = Depends(mutation_context)):
        account = access(mutation)
        result = await write(mutation, "mcp.tool.configure", name + "/" + remote,
            lambda: store.configure_tool(account, name, remote, **body.model_dump()))
        return mutation_payload(result, tool=result.value)

    @router.post("/api/mcp/tokens")
    async def issue_token(body: TokenIssue, mutation: AdminMutationContext = Depends(mutation_context)):
        account = access(mutation)
        if runtime.endpoint is None:
            raise HTTPException(status_code=503, detail="MCP 只读入口未配置")
        if not await runtime.scope_allowed(body.scope_key, body.bot_id):
            raise HTTPException(status_code=409, detail="无法核实机器人在已启用的目标群内")
        issued = {}
        def apply():
            issued.update(store.issue_token(account, **body.model_dump()))
            return {key: value for key, value in issued.items() if key != "token"}
        result = await write(mutation, "mcp.token.issue", body.scope_key, apply)
        # Only the response contains plaintext, never MutationResult/audit/SSE.
        return mutation_payload(result, grant=issued)

    @router.post("/api/mcp/tokens/{token_id}/revoke")
    async def revoke(token_id: str, body: Versioned, mutation: AdminMutationContext = Depends(mutation_context)):
        account = access(mutation)
        def apply():
            store.revoke_token(account, token_id, expected_version=body.expected_version)
            return {"token_id": token_id, "revoked": True}
        result = await write(mutation, "mcp.token.revoke", token_id, apply)
        return mutation_payload(result, grant=result.value)

    @router.get("/api/mcp/calls")
    async def calls(offset: int = Query(default=0, ge=0), limit: int = Query(default=5, ge=1, le=50)):
        access()
        return await asyncio.to_thread(store.admin_calls, offset=offset, limit=limit)

    return router
