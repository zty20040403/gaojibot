from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx2
import nonebot
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from nonebot.adapters.onebot.v11 import Bot
from starlette.applications import Starlette

nonebot.init()

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.config import Settings
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.lifecycle import BackgroundTaskSupervisor
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.plugins.ai_chat.mcp_bridge.runtime import McpRuntime, McpRuntimeConfig, install_mcp
from tests import test_mcp_calls as call_fixtures
from tests.test_account_security import BOT, QQ
from tests.test_mcp_boundaries import GROUP


class McpRuntimeConfigTests(unittest.TestCase):
    def test_disabled_by_default_and_no_ambient_configuration_read(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.mcp_enabled)
        self.assertEqual(settings.mcp_config_file, "")
        with patch("src.plugins.ai_chat.mcp_bridge.runtime.McpRuntimeConfig.load") as load:
            install_mcp(SimpleNamespace(context=SimpleNamespace(settings=settings)))
        load.assert_not_called()

    def test_operator_file_is_bounded_and_unknown_fields_rejected(self):
        for value in ([], {"credentials": {"secret": "no"}}, {"read_server": {"enabled": True}},
                      {"private_endpoints": ["https://user:password@example.test"]}, {"stdio_profiles": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                McpRuntimeConfig.parse(value)
        with self.assertRaises(ValueError):
            McpRuntimeConfig.load("relative.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_bytes(b" " * 65537)
            with self.assertRaises(ValueError):
                McpRuntimeConfig.load(str(path))
            path.write_text('{}')
            self.assertEqual(McpRuntimeConfig.load(str(path)), McpRuntimeConfig())

    def test_stdio_requires_pinned_image_and_explicit_launcher(self):
        valid = {"stdio_profiles": {"calc": {"image": "sha256:" + "a" * 64, "command": ["/bin/calc"]}},
            "docker_binary": "/usr/bin/docker", "docker_socket": "/run/docker.sock", "installation_id": "a" * 32}
        self.assertEqual(McpRuntimeConfig.parse(valid).profiles["calc"].command, ("/bin/calc",))
        for field, value in (("docker_binary", "docker"), ("installation_id", ""), ("docker_socket", "tcp://localhost:2375")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                McpRuntimeConfig.parse({**valid, field: value})
        with self.assertRaises(ValueError):
            McpRuntimeConfig.parse({**valid, "stdio_profiles": {"calc": {"image": "latest", "command": ["/bin/calc"]}}})

    def test_reader_has_dedicated_path_and_exact_host_allowlist(self):
        config = McpRuntimeConfig.parse({"read_server": {"path": "/mcp/history", "allowed_hosts": ["testserver", "[::1]:8000"]}})
        self.assertEqual(config.read_path, "/mcp/history")
        for path, hosts in (("/bot-admin", ["testserver"]), ("/mcp", []), ("", ["testserver"]),
                            ("/mcp", ["*"]), ("/mcp", ["*.example.test"]), ("/mcp", ["https://example.test"]),
                            ("/mcp", ["testserver:99999"]), ("/mcp", ["user@testserver"])):
            with self.subTest(path=path, hosts=hosts), self.assertRaises(ValueError):
                McpRuntimeConfig.parse({"read_server": {"path": path, "allowed_hosts": hosts}})


class McpRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = call_fixtures.McpCallTests.setUp
    policy = call_fixtures.McpCallTests.policy

    async def asyncSetUp(self):
        self.ledger = MessageLedger(self.database)
        self.addCleanup(self.ledger.close)
        self.logger = Mock()
        jobs = DurableJobStore(self.database)
        outbox = DeliveryStore(self.database)
        self.addCleanup(jobs.close)
        self.addCleanup(outbox.close)
        self.app = SimpleNamespace(database=self.database, db_call=DatabaseCalls(),
            job_store=jobs, job_worker=DurableJobWorker(jobs, logger=self.logger), delivery_store=outbox,
            mobile_authorization=SimpleNamespace(executors={}, bot_selector=lambda: BOT),
            message_ledger=self.ledger, context_store=None,
            background_tasks=BackgroundTaskSupervisor(self.logger))
        self.addAsyncCleanup(self.app.db_call.close)
        self.handlers = SimpleNamespace(context=self.app, group_enabled=Mock(return_value=True))
        self.config = McpRuntimeConfig.parse({"read_server": {"path": "/mcp", "allowed_hosts": ["testserver"]}})
        self.runtime = McpRuntime(self.handlers, self.config,
            secrets={"AI_POSTGRES_DSN": "private", "GAOJI_MCP_SECRET_EXAMPLE": "mcp-only"})
        self.addCleanup(self.runtime.store.close)
        self.addAsyncCleanup(self.app.background_tasks.stop_all)
        self.bot = Mock(spec=Bot, self_id=BOT)
        async def member(*, group_id, user_id, no_cache):
            self.assertTrue(no_cache)
            return {"group_id": group_id, "user_id": user_id}
        self.bot.get_group_member_info = AsyncMock(side_effect=member)
        bots = patch("src.plugins.ai_chat.mcp_bridge.runtime.get_bots", return_value={BOT: self.bot})
        bots.start()
        self.addCleanup(bots.stop)

    async def test_fresh_membership_requires_both_actor_and_bot_in_enabled_group(self):
        self.assertTrue(await self.runtime.scope_allowed(GROUP, BOT, QQ))
        self.assertEqual(self.bot.get_group_member_info.await_count, 2)
        self.handlers.group_enabled.return_value = False
        self.assertFalse(await self.runtime.scope_allowed(GROUP, BOT, QQ))
        self.assertEqual(self.bot.get_group_member_info.await_count, 2)
        self.handlers.group_enabled.return_value = True
        self.bot.get_group_member_info.side_effect = None
        self.bot.get_group_member_info.return_value = {"group_id": 200, "user_id": QQ}
        self.assertFalse(await self.runtime.scope_allowed(GROUP, BOT, QQ))

    async def test_disconnected_or_unverifiable_bot_fails_closed(self):
        with patch("src.plugins.ai_chat.mcp_bridge.runtime.get_bots", return_value={}):
            self.assertFalse(await self.runtime.scope_allowed(GROUP, BOT, QQ))
        self.bot.get_group_member_info.side_effect = TimeoutError()
        self.assertFalse(await self.runtime.scope_allowed(GROUP, BOT, QQ))

    async def test_real_sdk_mounted_endpoint_starts_stops_in_its_own_lifespan_task(self):
        token = self.runtime.store.issue_token(self.admin, scope_key=GROUP, bot_id=BOT, label="runtime test")["token"]
        app = Starlette()
        app.mount(self.config.read_path, self.runtime.http)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + token}, timeout=10) as http:
            response = await http.post("http://testserver/mcp/", json={})
            self.assertEqual(response.status_code, 503)
            await self.runtime.start()
            async with Client(streamable_http_client("http://testserver/mcp/", http_client=http)) as client:
                listing = await client.list_tools()
                self.assertEqual({item.name for item in listing.tools}, {"context_search", "ledger_search"})
                result = await client.call_tool("ledger_search", {})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["scope"], GROUP)
            await self.app.background_tasks.stop_all()
            self.assertFalse(self.runtime.ready.is_set())
            self.assertEqual((await http.post("http://testserver/mcp/", json={})).status_code, 503)
        self.logger.error.assert_not_called()

    async def test_only_explicit_mcp_credentials_reach_transport(self):
        policy = self.runtime.sessions.http.policy
        self.assertEqual(dict(policy.secrets), {"GAOJI_MCP_SECRET_EXAMPLE": "mcp-only"})
        self.assertNotIn("mcp-only", repr(policy))
        self.assertIs(self.app.mobile_authorization.executors["mcp_tool"].__self__, self.runtime.service)
        self.assertIn("mcp_continuation", self.app.job_worker.registered_kinds)


if __name__ == "__main__":
    unittest.main()
