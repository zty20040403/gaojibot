from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
import nonebot
from fastapi import FastAPI

nonebot.init()

from src.bot_security.passwords import hash_password
from src.bot_security.service import MobileAuthorization
from src.plugins.ai_chat.admin import AdminServices, _changed_database_resources, register_admin
from src.plugins.ai_chat.mcp_bridge.client import McpCallError
from src.plugins.ai_chat.mcp_bridge.store import McpDenied
from tests import test_mcp_calls as call_fixtures
from tests.test_account_security import BOT, PASSWORD
from tests.test_mcp_boundaries import CONFIG, GROUP, TOOL

API = "/bot-admin/api/v1/mcp"


class McpAdminTests(unittest.IsolatedAsyncioTestCase):
    setUp = call_fixtures.McpCallTests.setUp
    policy = call_fixtures.McpCallTests.policy
    prepare = call_fixtures.McpCallTests.prepare

    async def asyncSetUp(self):
        self.mobile = MobileAuthorization(self.security, sender=AsyncMock(), bot_selector=lambda: BOT)
        self.wire = SimpleNamespace(discover=AsyncMock(return_value=[TOOL]))
        self.runtime = SimpleNamespace(store=self.store, service=SimpleNamespace(wire=self.wire),
            config=SimpleNamespace(profiles={}, read_path="/mcp"), endpoint=object(),
            ready=asyncio.Event(), scope_allowed=AsyncMock(return_value=True),
            sessions=SimpleNamespace(http=SimpleNamespace(policy=SimpleNamespace(secrets={"GAOJI_MCP_SECRET_TEST": "not-for-audit"}))))
        self.app = FastAPI()
        register_admin(self.app, AdminServices(version="test", started_at=1, database=self.database,
            mobile_authorization=self.mobile, mcp_runtime=self.runtime, settings=SimpleNamespace(admin_origin="https://test")))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url="https://test", headers={"Origin": "https://test"})
        self.addAsyncCleanup(self.client.aclose)

    async def login(self, username="owner"):
        result = await self.client.post("/bot-admin/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)
        self.client.headers["X-CSRF-Token"] = self.client.cookies["gaoji_csrf"]

    async def test_anonymous_and_member_access_is_denied(self):
        for prefix in (API, "/bot-admin/api/mcp"):
            self.assertEqual((await self.client.get(prefix)).status_code, 401)
        self.security.apply_account_change(self.admin, {"action": "create", "username": "viewer",
            "password_hash": hash_password(PASSWORD), "role": "member", "qq_id": None})
        await self.login("viewer")
        for method, path in (("GET", ""), ("GET", "/calls"), ("POST", "/tokens"), ("PUT", "/servers/example")):
            self.assertEqual((await self.client.request(method, API + path, json={})).status_code, 403)

    async def test_server_mutations_require_session_csrf_and_both_versions(self):
        await self.login()
        body = {"config": CONFIG, "scopes": [GROUP], "enabled": True, "expected_version": 3}
        path = API + "/servers/example"
        self.assertEqual((await self.client.put(path, json=body)).status_code, 428)
        self.assertEqual((await self.client.put(path, json=body, headers={"If-Match": '"0"', "X-CSRF-Token": "wrong"})).status_code, 403)
        result = await self.client.put(path, json=body, headers={"If-Match": '"0"'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["server"]["version"], 4)
        self.assertEqual((await self.client.put(path, json=body, headers={"If-Match": '"0"'})).status_code, 409)
        self.assertEqual((await self.client.put(path, json=body, headers={"If-Match": '"1"'})).status_code, 409)
        self.mobile.sender.assert_not_awaited()

    async def test_catalog_refresh_revokes_changed_tools_and_requires_explicit_enable(self):
        await self.login()
        self.wire.discover.return_value = [{**TOOL, "description": "Changed tool"}]
        response = await self.client.post(API + "/servers/example/refresh", json={"expected_version": 3}, headers={"If-Match": '"0"'})
        self.assertEqual(response.status_code, 200, response.text)
        tool = response.json()["tools"][0]
        self.assertFalse(tool["enabled"])
        self.assertEqual(tool["trust"], "approval")
        body = {"enabled": True, "trust": "safe", "expected_version": tool["version"],
            "server_version": 4, "tool_fingerprint": tool["fingerprint"]}
        response = await self.client.put(API + "/servers/example/tools/lookup", json=body, headers={"If-Match": '"1"'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["tool"]["enabled"])
        self.assertEqual(self.store.resolve("example", "lookup", GROUP)[1]["trust"], "safe")

    async def test_endpoint_change_during_discovery_cannot_commit_stale_catalog(self):
        await self.login()
        async def discover(config):
            self.store.configure(self.admin, "example", config={**CONFIG, "url": "https://changed.example/mcp"},
                scopes=[GROUP], enabled=True, expected_version=3)
            return [TOOL]
        self.wire.discover.side_effect = discover
        response = await self.client.post(API + "/servers/example/refresh", json={"expected_version": 3}, headers={"If-Match": '"0"'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.store.tools("example")[0]["present"])

    async def test_tokens_are_one_time_only_never_in_audit_or_listings_and_revocable(self):
        await self.login()
        response = await self.client.post(API + "/tokens", json={"scope_key": GROUP, "bot_id": BOT, "label": "Personal MCP reader"},
            headers={"If-Match": '"0"'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        grant = response.json()["grant"]
        self.assertEqual(self.store.authenticate(grant["token"])["scope"].key, GROUP)
        overview = await self.client.get(API)
        self.assertEqual(overview.json()["credential_refs"], ["GAOJI_MCP_SECRET_TEST"])
        audit = await self.client.get("/bot-admin/api/v1/audit")
        self.assertNotIn(grant["token"], overview.text + audit.text)
        self.assertNotIn("not-for-audit", overview.text + audit.text)
        response = await self.client.post(API + f"/tokens/{grant['token_id']}/revoke", json={"expected_version": 1}, headers={"If-Match": '"1"'})
        self.assertEqual(response.status_code, 200, response.text)
        with self.assertRaises(McpDenied):
            self.store.authenticate(grant["token"])

    async def test_token_requires_enabled_reader_and_current_group_membership(self):
        await self.login()
        body = {"scope_key": GROUP, "bot_id": BOT, "label": "reader"}
        self.runtime.scope_allowed.return_value = False
        response = await self.client.post(API + "/tokens", json=body, headers={"If-Match": '"0"'})
        self.assertEqual(response.status_code, 409)
        self.runtime.endpoint = None
        self.assertEqual((await self.client.post(API + "/tokens", json=body, headers={"If-Match": '"0"'})).status_code, 503)
        self.assertFalse(self.store.tokens())

    async def test_only_operator_installed_profiles_and_credential_references_can_be_registered(self):
        await self.login()
        for config in ({"transport": "stdio", "profile": "shell"}, {**CONFIG, "command": "sh"},
                       {**CONFIG, "url": "https://user:secret@example.test"}):
            response = await self.client.put(API + "/servers/new", json={"expected_version": 0,
                "config": config, "scopes": [GROUP], "enabled": True}, headers={"If-Match": '"0"'})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertIsNone(self.store.server("new"))

    async def test_discovery_failure_is_audited_without_remote_error_body(self):
        await self.login()
        self.wire.discover.side_effect = McpCallError("mcp_unavailable")
        response = await self.client.post(API + "/servers/example/refresh", json={"expected_version": 3}, headers={"If-Match": '"0"'})
        self.assertEqual(response.status_code, 502, response.text)
        audit = (await self.client.get("/bot-admin/api/v1/audit")).json()["items"]
        self.assertEqual(audit[0]["action"], "mcp.refresh")
        self.assertEqual(audit[0]["status"], "failed")
        self.assertEqual(self.store.server("example")["version"], 3)

    async def test_default_call_page_is_five_with_no_bound_credentials_or_lease_owner(self):
        await self.login()
        for index in range(7):
            self.prepare(invocation=f"case:{index}")
        page = (await self.client.get(API + "/calls")).json()
        self.assertEqual((len(page["items"]), page["total"]), (5, 7))
        self.assertEqual(len((await self.client.get(API + "/calls?offset=5")).json()["items"]), 2)
        self.assertFalse(set(page["items"][0]) & {"owner", "binding", "invocation_hash", "binding_json", "arguments"})

    def test_catalog_tokens_and_receipts_invalidate_realtime_mcp_resource(self):
        for table in ("mcp_servers", "mcp_tools", "mcp_group_tokens", "mcp_calls"):
            self.assertIn("mcp", _changed_database_resources({table: (1, 0, 0)}, {table: (1, 1, 0)}))


if __name__ == "__main__":
    unittest.main()
