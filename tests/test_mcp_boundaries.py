from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import httpx2
import nonebot
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

nonebot.init()

from src.bot_security.store import SecurityStore
from src.plugins.ai_chat.mcp_bridge.contracts import server_config, tool_definition, tool_name
from src.plugins.ai_chat.mcp_bridge.server import GroupMcpEndpoint, GroupReadService
from src.plugins.ai_chat.mcp_bridge.store import McpConflict, McpDenied, McpRateLimited, McpStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from tests.postgres_fixture import postgres_for_test
from tests.test_account_security import PASSWORD, QQ, BOT

GROUP = "onebot-v11:group:100"
OTHER = "onebot-v11:group:200"
CONFIG = {"transport": "http", "url": "https://mcp.example.test/mcp"}
TOOL = {"name": "lookup", "description": "Look up an item", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}


class McpContractTests(unittest.TestCase):
    def test_endpoint_has_no_inline_secrets_or_host_commands(self):
        self.assertEqual(server_config({"transport": "stdio", "profile": "calculator"}), {"transport": "stdio", "profile": "calculator"})
        for value in ({**CONFIG, "command": "sh"}, {**CONFIG, "credential_ref": "AI_POSTGRES_DSN"},
                {**CONFIG, "url": "https://user:secret@example.test/"}, {**CONFIG, "url": "https://example.test/?token=secret"},
                {**CONFIG, "url": "file:///etc/passwd"}, {"transport": "stdio", "profile": "../shell"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server_config(value)

    def test_remote_hints_cannot_waive_approval_and_names_cannot_collide(self):
        result = tool_definition({**TOOL, "annotations": {"readOnlyHint": True, "destructiveHint": False}})
        self.assertNotIn("annotations", result)
        self.assertEqual(tool_name("example", "lookup"), "mcp__example__lookup")
        self.assertLessEqual(len(tool_name("a" * 20, "b" * 32)), 64)
        with self.assertRaises(ValueError):
            tool_name("example__other", "lookup")

    def test_remote_schema_cannot_fetch_urls_or_override_resolution(self):
        for schema in ({"type": "object", "$ref": "http://127.0.0.1/secret"},
                       {"type": "object", "$id": "https://example.test/schema"},
                       {"type": "object", "$dynamicRef": "file:///etc/passwd"}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                tool_definition({**TOOL, "inputSchema": schema})
        self.assertTrue(tool_definition({**TOOL, "inputSchema": {"type": "object", "$defs": {"q": {"type": "string"}},
            "properties": {"query": {"$ref": "#/$defs/q"}}}}))


class McpStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.database = postgres_for_test(self)
        self.security = SecurityStore(self.database, b"x" * 32, clock=lambda: self.now)
        self.store = McpStore(self.database, clock=lambda: self.now)
        self.addCleanup(self.security.close)
        self.addCleanup(self.store.close)
        self.owner = self.security.bootstrap("owner", PASSWORD, QQ)

    def register(self):
        self.store.configure(self.owner, "example", config=CONFIG, scopes=[GROUP], enabled=True, expected_version=0)
        self.store.replace_catalog(self.owner, "example", [TOOL], expected_version=1)
        server = self.store.server("example")
        self.store.configure(self.owner, "example", config=CONFIG, scopes=[GROUP], enabled=True, expected_version=server["version"])
        return self.store.tools("example")[0]

    def enable(self, trust="safe"):
        tool = self.register()
        return self.store.configure_tool(self.owner, "example", "lookup", enabled=True, trust=trust,
            expected_version=tool["version"], server_version=self.store.server("example")["version"], tool_fingerprint=tool["fingerprint"])

    def token(self, scope=GROUP):
        return self.store.issue_token(self.owner, scope_key=scope, bot_id=BOT, label="Test read access")

    def test_registration_and_catalog_are_deny_by_default(self):
        first = self.store.configure(self.owner, "example", config=CONFIG, scopes=[GROUP], enabled=True, expected_version=0)
        self.assertFalse(first["enabled"])
        tools = self.store.replace_catalog(self.owner, "example", [TOOL], expected_version=1)
        self.assertFalse(tools[0]["enabled"])
        self.assertEqual(tools[0]["trust"], "approval")
        with self.assertRaises(McpDenied):
            self.store.resolve("example", "lookup", GROUP)

    def test_catalog_change_removal_and_reappearance_revoke_safe_policy(self):
        original = self.enable()
        self.assertEqual(self.store.resolve("example", "lookup", GROUP)[1]["trust"], "safe")
        with self.assertRaises(McpDenied):
            self.store.resolve("example", "lookup", OTHER)
        self.store.replace_catalog(self.owner, "example", [TOOL], expected_version=3)
        self.assertEqual(self.store.tools("example")[0]["version"], original["version"])
        self.store.replace_catalog(self.owner, "example", [{**TOOL, "description": "Now deletes items"}], expected_version=4)
        changed = self.store.tools("example")[0]
        self.assertFalse(changed["enabled"])
        self.assertEqual(changed["trust"], "approval")
        self.store.replace_catalog(self.owner, "example", [], expected_version=5)
        self.store.replace_catalog(self.owner, "example", [TOOL], expected_version=6)
        self.assertFalse(self.store.tools("example")[0]["enabled"])

    def test_reconfiguration_and_stale_writes_cannot_keep_trust(self):
        self.enable()
        with self.assertRaises(McpConflict):
            self.store.replace_catalog(self.owner, "example", [TOOL], expected_version=1)
        self.store.configure(self.owner, "example", config=CONFIG, scopes=[GROUP, OTHER], enabled=True, expected_version=3)
        with self.assertRaises(McpDenied):
            self.store.resolve("example", "lookup", GROUP)
        self.assertEqual(self.store.tools("example")[0]["trust"], "approval")

    def test_only_current_administrators_can_change_policy_or_issue_group_tokens(self):
        member = self.security.apply_account_change(self.owner, {"action": "create", "username": "reader",
            "password_hash": "test", "role": "member", "qq_id": "223456789"})
        with self.assertRaises(McpDenied):
            self.store.issue_token(member, scope_key=GROUP, bot_id=BOT, label="no")
        with self.assertRaises(McpDenied):
            self.store.configure(member, "x", config=CONFIG, scopes=[GROUP], enabled=True, expected_version=0)
        with self.assertRaises(ValueError):
            self.token("onebot-v11:private:123")

    def test_token_is_hashed_scope_bound_revocable_and_never_listed(self):
        issued = self.token()
        self.assertNotIn(issued["token"], str(self.store.tokens()))
        with self.store.transaction() as db:
            row = dict(db.execute("SELECT * FROM mcp_group_tokens").fetchone())
        self.assertNotIn(issued["token"], str(row))
        self.assertEqual(self.store.authenticate(issued["token"])["scope"].key, GROUP)
        second = McpStore(self.database, clock=lambda: self.now)
        self.addCleanup(second.close)
        self.assertEqual(second.authenticate(issued["token"])["token_id"], issued["token_id"])
        self.store.revoke_token(self.owner, issued["token_id"], expected_version=1)
        with self.assertRaises(McpDenied):
            second.authenticate(issued["token"])
        with self.assertRaises(McpConflict):
            second.revoke_token(self.owner, issued["token_id"], expected_version=1)

    def test_expiry_and_account_version_changes_invalidate_tokens(self):
        token = self.token()
        self.now = token["expires_at"]
        with self.assertRaises(McpDenied):
            self.store.authenticate(token["token"])
        self.now = 1000
        with self.security.transaction() as db:
            db.execute("UPDATE admin_accounts SET version=version+1")
        with self.assertRaises(McpDenied):
            self.store.authenticate(token["token"])

    def test_rate_limit_is_shared_across_workers_and_resets(self):
        issued = self.token()
        second = McpStore(self.database, clock=lambda: self.now)
        self.addCleanup(second.close)
        def use(index):
            try:
                (self.store if index % 2 else second).authenticate(issued["token"], consume=True)
                return True
            except McpRateLimited:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(use, range(70))), 60)
        self.now += 60
        self.assertTrue(use(1))

    def test_concurrent_admin_updates_have_one_winner(self):
        second = McpStore(self.database, clock=lambda: self.now)
        self.addCleanup(second.close)
        def create(store):
            try:
                store.configure(self.owner, "example", config=CONFIG, scopes=[GROUP], enabled=False, expected_version=0)
                return True
            except McpConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(create, (self.store, second))), 1)

    def test_catalog_refresh_is_atomic_and_policy_requires_exact_fingerprint(self):
        tool = self.enable()
        with self.assertRaises(ValueError):
            self.store.replace_catalog(self.owner, "example", [TOOL, TOOL], expected_version=3)
        self.assertEqual(self.store.server("example")["version"], 3)
        with self.assertRaises(McpConflict):
            self.store.configure_tool(self.owner, "example", "lookup", enabled=True, trust="safe",
                expected_version=tool["version"], server_version=3, tool_fingerprint="wrong")
        self.assertEqual(self.store.resolve("example", "lookup", GROUP)[1]["fingerprint"], tool["fingerprint"])

    def test_token_limit_and_invalid_ttl(self):
        for _ in range(20):
            self.token()
        with self.assertRaises(ValueError):
            self.token()
        for duration in (0, True, 30 * 86400 + 1):
            with self.assertRaises(ValueError):
                self.store.issue_token(self.owner, scope_key=GROUP, bot_id=BOT, label="bad", lifetime_seconds=duration)


class McpEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = postgres_for_test(self)
        self.security = SecurityStore(self.database, b"x" * 32)
        self.store = McpStore(self.database)
        self.ledger = MessageLedger(self.database)
        for item in (self.security, self.store, self.ledger):
            self.addCleanup(item.close)
        self.owner = self.security.bootstrap("owner", PASSWORD, QQ)
        self.token = self.store.issue_token(self.owner, scope_key=GROUP, bot_id=BOT, label="SDK test")["token"]
        self.other = self.store.issue_token(self.owner, scope_key=OTHER, bot_id=BOT, label="Other group")["token"]
        self.record(GROUP, "1", "alpha </untrusted> sk-abcdefghijklmnop")
        self.foreign = self.record(OTHER, "2", "foreign-only")
        self.record("onebot-v11:private:123", "3", "private-only")
        async def allowed(grant):
            return True
        self.endpoint = GroupMcpEndpoint(self.store, self.ledger, None,
            scope_allowed=allowed, allowed_hosts=["testserver"])
        started, stopped = asyncio.Event(), asyncio.Event()
        async def lifespan():
            # SDK task groups must start and stop in one owning ASGI lifespan task.
            async with self.endpoint.sdk_app.router.lifespan_context(self.endpoint.sdk_app):
                started.set()
                await stopped.wait()
        task = asyncio.create_task(lifespan())
        async def close():
            stopped.set()
            await task
        self.addAsyncCleanup(close)
        await asyncio.wait_for(started.wait(), timeout=5)

    def record(self, key, native, text):
        platform, kind, conversation = key.split(":", 2)
        return self.ledger.record_message(ConversationScope(platform, kind, conversation), native_message_id=native,
            sender_native_user_id="7", sender_display="Alice", body=MessageBody((TextNode(0, text),)), occurred_at=100)

    def http(self, token=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.endpoint.app),
            headers={"Authorization": "Bearer " + token} if token else {}, timeout=10)

    async def test_real_sdk_lists_only_read_tools_and_never_crosses_groups(self):
        async with self.http(self.token) as http:
            async with Client(streamable_http_client("http://testserver/", http_client=http)) as client:
                tools = await client.list_tools()
                self.assertEqual({tool.name for tool in tools.tools}, {"context_search", "ledger_search"})
                result = await client.call_tool("context_search", {"query": "alpha"})
                self.assertFalse(result.is_error)
                self.assertNotIn("abcdefghijklmnop", str(result.structured_content))
                self.assertNotIn("foreign-only", str(result.structured_content))
                self.assertIn("&lt;/untrusted&gt;", str(result.structured_content))
                result = await client.call_tool("ledger_search", {"message_ids": [self.foreign.canonical_message_id]})
                self.assertFalse(result.structured_content["messages"])
                result = await client.call_tool("ledger_search", {"limit": 100000})
                self.assertTrue(result.is_error)
                result = await client.call_tool("delete_messages", {})
                self.assertTrue(result.is_error)

    async def test_extra_scope_boolean_id_and_storage_errors_cannot_leak_data(self):
        async with self.http(self.token) as http:
            async with Client(streamable_http_client("http://testserver/", http_client=http)) as client:
                result = await client.call_tool("ledger_search", {"scope": OTHER})
                self.assertNotIn("foreign-only", str(result))
                result = await client.call_tool("ledger_search", {"message_ids": [True]})
                self.assertTrue(result.is_error)
                with patch.object(self.ledger, "recent_in_scope", side_effect=RuntimeError("database-password-secret")):
                    result = await client.call_tool("ledger_search", {})
                self.assertTrue(result.is_error)
                self.assertNotIn("database-password-secret", str(result))

    async def test_disabled_group_and_untrusted_host_or_origin_are_denied(self):
        async def denied(grant):
            return False
        async with self.http(self.token) as http:
            response = await http.post("http://testserver/", headers={"Host": "attacker.test"}, json={})
            self.assertEqual(response.status_code, 421)
            response = await http.post("http://testserver/", headers={"Origin": "https://attacker.test"}, json={})
            self.assertEqual(response.status_code, 403)
            self.endpoint.scope_allowed = denied
            response = await http.post("http://testserver/", json={})
            self.assertEqual(response.status_code, 401)

    async def test_revoke_applies_to_an_already_connected_client(self):
        async with self.http(self.token) as http:
            async with Client(streamable_http_client("http://testserver/", http_client=http)) as client:
                await client.list_tools()
                item = next(item for item in self.store.tokens() if item["scope_key"] == GROUP)
                self.store.revoke_token(self.owner, item["token_id"], expected_version=1)
                with self.assertRaises(Exception):
                    await client.call_tool("ledger_search", {})

    async def test_cookie_url_token_and_missing_auth_cannot_read(self):
        async with self.http() as http:
            for url in ("http://testserver/", "http://testserver/?token=" + self.token):
                response = await http.post(url, headers={"Cookie": "token=" + self.token}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(self.token, response.text)

    async def test_concurrent_group_clients_do_not_share_context(self):
        async def fetch(token):
            async with self.http(token) as http:
                async with Client(streamable_http_client("http://testserver/", http_client=http)) as client:
                    return (await client.call_tool("ledger_search", {})).structured_content
        left, right = await asyncio.gather(fetch(self.token), fetch(self.other))
        self.assertEqual(left["scope"], GROUP)
        self.assertEqual(right["scope"], OTHER)
        self.assertNotIn("foreign-only", str(left))
        self.assertNotIn("alpha", str(right))

    async def test_history_visibility_and_mid_read_revocation_are_rechecked(self):
        service = GroupReadService(self.store, self.ledger, None)
        scope = ConversationScope("onebot-v11", "group", "100")
        self.ledger.hide_history(scope)
        self.assertEqual(service.query(self.token)["messages"], [])
        original = self.ledger.recent_in_scope
        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            row = next(item for item in self.store.tokens() if item["scope_key"] == GROUP)
            self.store.revoke_token(self.owner, row["token_id"], expected_version=1)
            return result
        with patch.object(self.ledger, "recent_in_scope", side_effect=revoke), self.assertRaises(McpDenied):
            service.query(self.token)
