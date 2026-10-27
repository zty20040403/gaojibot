"""Opt-in application wiring; endpoint and container ownership stay with the host."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot
from starlette.responses import JSONResponse

from .continuations import McpContinuationStore
from .continuation_runtime import McpContinuationDispatcher
from .client import McpWireClient
from .contracts import SERVER_NAME, group_scope, server_config
from .http_transport import HttpPolicy, HttpSessions
from .server import GroupMcpEndpoint
from .service import McpService
from .stdio_transport import DockerLauncher, McpSessions, StdioProfile, StdioSessions


@dataclass(frozen=True)
class McpRuntimeConfig:
    private_endpoints: frozenset[str] = frozenset()
    profiles: dict[str, StdioProfile] = field(default_factory=dict)
    docker_binary: str = ""
    docker_socket: str = ""
    installation_id: str = ""
    read_path: str = ""
    allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def load(cls, filename: str):
        path = Path(filename)
        if not filename or not path.is_absolute():
            raise ValueError("AI_MCP_CONFIG_FILE must be an absolute operator-owned JSON file")
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("MCP operator configuration exceeds 64 KiB")
        return cls.parse(json.loads(raw))

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - {
            "private_endpoints", "stdio_profiles", "docker_binary", "docker_socket", "installation_id", "read_server",
        }:
            raise ValueError("Invalid MCP operator configuration keys")
        endpoints = value.get("private_endpoints", [])
        if not isinstance(endpoints, list) or len(endpoints) > 32:
            raise ValueError("MCP allows at most 32 exact private endpoints")
        endpoints = frozenset(server_config({"transport": "http", "url": url})["url"] for url in endpoints)
        profiles = value.get("stdio_profiles", {})
        if not isinstance(profiles, dict) or len(profiles) > 32:
            raise ValueError("Invalid MCP stdio profile inventory")
        parsed = {}
        for name, profile in profiles.items():
            if (not isinstance(name, str) or not SERVER_NAME.fullmatch(name) or not isinstance(profile, dict)
                    or set(profile) != {"image", "command"} or not isinstance(profile["command"], list)):
                raise ValueError("Invalid MCP stdio profile")
            parsed[name] = StdioProfile(profile["image"], tuple(profile["command"]))
        binary, socket, owner = (value.get(key, "") for key in ("docker_binary", "docker_socket", "installation_id"))
        if any(not isinstance(item, str) for item in (binary, socket, owner)):
            raise ValueError("Invalid MCP launcher configuration")
        if parsed:
            DockerLauncher(binary, socket, owner)
        read = value.get("read_server", {})
        if not isinstance(read, dict) or set(read) - {"path", "allowed_hosts"}:
            raise ValueError("Invalid MCP read server configuration")
        path, hosts = read.get("path", ""), read.get("allowed_hosts", [])
        if (not isinstance(path, str) or path and not re.fullmatch(r"/mcp(?:/[A-Za-z0-9_-]+)*", path)
                or not isinstance(hosts, list) or len(hosts) > 16 or bool(path) != bool(hosts)):
            raise ValueError("MCP reader requires its own /mcp path and explicit hostnames")
        for host in hosts:
            if not isinstance(host, str) or not host or len(host) > 255 or any(c in host for c in "*@/?#\\ \t\r\n"):
                raise ValueError("Invalid MCP served hostname")
            url = urlsplit("https://" + host)
            if not url.hostname or url.username or url.password or (url.port is not None and not 1 <= url.port <= 65535):
                raise ValueError("Invalid MCP served hostname")
        return cls(endpoints, parsed, binary, socket, owner, path, tuple(hosts))


class McpRuntime:
    def __init__(self, handlers, config: McpRuntimeConfig, *, secrets=None):
        self.handlers, self.app, self.config = handlers, handlers.context, config
        if self.app.mobile_authorization is None:
            raise RuntimeError("MCP requires configured administrator accounts and mobile approval")
        if config.read_path and self.app.message_ledger is None:
            raise RuntimeError("MCP history requires the message ledger")
        if any(getattr(self.app, name, None) is None for name in ("job_store", "job_worker", "delivery_store")):
            raise RuntimeError("MCP requires durable jobs and Outbox for approval continuation")
        self.launcher = DockerLauncher(config.docker_binary, config.docker_socket, config.installation_id) if config.profiles else None
        credentials = {key: value for key, value in (os.environ if secrets is None else secrets).items()
                       if re.fullmatch(r"GAOJI_MCP_SECRET_[A-Z0-9_]+", key)}
        self.sessions = McpSessions(HttpSessions(HttpPolicy(config.private_endpoints, credentials)),
            StdioSessions(config.profiles, self.launcher) if self.launcher else None)
        self.store = McpContinuationStore(self.app.database, jobs=self.app.job_store, outbox=self.app.delivery_store)
        try:
            self.service = McpService(self.store, McpWireClient(self.sessions),
                scope_allowed=self.scope_allowed, mobile=self.app.mobile_authorization, db_call=self.app.db_call)
            self.endpoint = GroupMcpEndpoint(self.store, self.app.message_ledger, self.app.context_store,
                scope_allowed=self.read_allowed, allowed_hosts=config.allowed_hosts) if config.read_path else None
            self.dispatcher = McpContinuationDispatcher(self)
        except BaseException:
            self.store.close()
            raise
        self.ready = asyncio.Event()
        self.app.mobile_authorization.executors["mcp_tool"] = self.service.execute_approved

    async def scope_allowed(self, scope_key, bot_id, user_id=""):
        scope = group_scope(scope_key)
        group_id = int(scope.native_conversation_id)
        if not await self.app.db_call(self.handlers.group_enabled, group_id):
            return False
        bot = get_bots().get(bot_id)
        if not isinstance(bot, Bot) or str(bot.self_id) != bot_id:
            return False
        try:
            async with asyncio.timeout(4):
                for member_id in {bot_id, user_id} - {""}:
                    member = await bot.get_group_member_info(group_id=group_id, user_id=int(member_id), no_cache=True)
                    if (not isinstance(member, dict) or str(member.get("user_id")) != member_id
                            or str(member.get("group_id")) != str(group_id)):
                        return False
        except Exception:
            return False
        return True

    async def read_allowed(self, grant):
        return await self.scope_allowed(grant["scope"].key, grant["bot_id"])

    async def serve(self):
        # AnyIO's manager must enter and exit in this same supervised task, not
        # in NoneBot's separate startup/shutdown hook tasks.
        async with self.endpoint.mcp.session_manager.run():
            self.ready.set()
            try:
                await asyncio.Future()
            finally:
                self.ready.clear()

    async def http(self, scope, receive, send):
        if not self.ready.is_set():
            return await JSONResponse({"error": "MCP reader is not ready"}, status_code=503)(scope, receive, send)
        return await self.endpoint.app(scope, receive, send)

    async def maintain(self):
        while True:
            await self.app.db_call(self.store.recover_calls)
            await self.app.db_call(self.store.materialize_continuations)
            await self.app.db_call(self.store.reconcile_terminal_jobs)
            if self.launcher is not None:
                await self.launcher.reap()
            await asyncio.sleep(30)

    async def start(self):
        self.app.background_tasks.start("mcp-maintenance", self.maintain)
        if self.endpoint is not None:
            self.app.background_tasks.start("mcp-reader", self.serve)
            await asyncio.wait_for(self.ready.wait(), timeout=5)


def install_mcp(handlers):
    app = handlers.context
    if not getattr(app.settings, "mcp_enabled", False):
        return
    config = McpRuntimeConfig.load(app.settings.mcp_config_file)
    runtime = McpRuntime(handlers, config)
    app.mcp_runtime, app.mcp_service = runtime, runtime.service
