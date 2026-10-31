from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import nonebot
from fastapi import FastAPI
from nonebot.adapters.onebot.v11 import Bot

nonebot.init()

from src.bot_security.passwords import hash_password
from src.bot_security.service import MobileAuthorization
from src.bot_security.store import SecurityStore
from src.plugins.ai_chat.admin import AdminServices, register_admin
from tests.test_account_security import PASSWORD, QQ, BOT


class Preferences:
    def __init__(self):
        self.value = None
        self.writes = 0

    def enabled_override(self):
        return self.value

    def effective_enabled(self, default):
        return default if self.value is None else self.value

    def set_enabled(self, value):
        self.value = value
        self.writes += 1


class AdminAccountApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SecurityStore(postgres_for_test(self), b"t" * 32)
        self.addCleanup(self.store.close)
        self.account = self.store.bootstrap("kenneth", PASSWORD, QQ)
        self.store.apply_account_change(self.account, {"action": "create", "username": "viewer", "password_hash": hash_password(PASSWORD), "role": "member", "qq_id": None})
        self.sent = []
        async def send(bot, qq, message):
            self.sent.append((bot, qq, message))
        self.mobile = MobileAuthorization(self.store, sender=send, bot_selector=lambda: BOT)
        self.preferences = Preferences()
        self.sandbox = SimpleNamespace(
            admin_snapshot=AsyncMock(return_value={"items": [{"sandbox_id": "s123abc",
                "owner": "group:another:user:7", "activities": [], "workspace_file_count": 3}]}),
            destroy=AsyncMock(), start_owned=AsyncMock(), stop_owned=AsyncMock(),
        )
        self.app = FastAPI()
        register_admin(self.app, AdminServices(version="test", started_at=1,
            settings=SimpleNamespace(admin_origin="https://test", alert_notify_enabled=False),
            mobile_authorization=self.mobile,
            sandbox_manager=self.sandbox), token="obsolete-token")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://test", headers={"Origin": "https://test"})
        self.addAsyncCleanup(self.client.aclose)

    async def login(self, username="kenneth"):
        response = await self.client.post("/bot-admin/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.headers["X-CSRF-Token"] = self.client.cookies.get("gaoji_csrf")
        return response

    async def approve(self, response):
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["approval_id"]
        code = re.search(r"一次性口令：([0-9]{6})", self.sent[-1][2])[1]
        self.store.confirm(identifier, QQ, BOT, code)
        self.assertTrue(await self.mobile.run_once())
        return await self.client.get(f"/bot-admin/api/v1/approvals/{identifier}")

    async def test_old_bearer_and_empty_configuration_never_grant_access(self):
        for prefix in ("/bot-admin/api", "/bot-admin/api/v1"):
            response = await self.client.get(prefix + "/overview", headers={"Authorization": "Bearer obsolete-token"})
            self.assertEqual(response.status_code, 401)
        locked = FastAPI()
        register_admin(locked, AdminServices(version="test", started_at=1))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=locked), base_url="https://test") as client:
            self.assertEqual((await client.get("/bot-admin/api/v1/overview")).status_code, 503)

    async def test_member_only_gets_basic_status_and_own_session(self):
        response = await self.login("viewer")
        self.assertIn("HttpOnly", response.headers.get_list("set-cookie")[0])
        self.assertIn("Secure", response.headers.get_list("set-cookie")[0])
        for prefix in ("/bot-admin/api", "/bot-admin/api/v1"):
            status = await self.client.get(prefix + "/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(set(status.json()), {"version", "uptime_seconds", "process", "qq_connected"})
            for resource in ("overview", "traces", "context-debug", "accounts", "audit", "events", "security-audit", "approvals"):
                self.assertEqual((await self.client.get(prefix + "/" + resource)).status_code, 403, resource)
            response = await self.client.post(prefix + "/sandboxes/s123abc/action", json={"action": "destroy"})
            self.assertEqual(response.status_code, 403)
        self.sandbox.destroy.assert_not_awaited()
        self.assertFalse(self.sent)

    async def test_qq_status_checks_account_not_just_websocket(self):
        await self.login("viewer")
        bot = Mock(spec=Bot)
        bot.call_api = AsyncMock()
        with patch("nonebot.get_bots", return_value={"qq": bot}):
            for state, online in (
                ({"online": False, "good": True}, False),
                ({"online": True, "good": True}, True),
                ({"online": True, "good": False}, False),
                ({"online": "true", "good": True}, False),
                ({"good": True}, False),
                (None, False),
            ):
                with self.subTest(state=state):
                    bot.call_api.reset_mock()
                    bot.call_api.return_value = state
                    response = await self.client.get("/bot-admin/api/v1/status")
                    self.assertEqual(response.status_code, 200)
                    self.assertIs(response.json()["qq_connected"], online)
                    bot.call_api.assert_awaited_once_with("get_status")

    async def test_qq_status_does_not_claim_online_when_probe_fails(self):
        await self.login("viewer")
        bot = Mock(spec=Bot)
        bot.call_api = AsyncMock(side_effect=TimeoutError)
        with patch("nonebot.get_bots", return_value={"qq": bot}):
            response = await self.client.get("/bot-admin/api/v1/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["qq_connected"])

    async def test_qq_status_ignores_other_adapters_and_checks_all_qq_accounts(self):
        await self.login("viewer")
        with patch("nonebot.get_bots", return_value={"other": object()}):
            response = await self.client.get("/bot-admin/api/v1/status")
        self.assertFalse(response.json()["qq_connected"])
        offline, online = Mock(spec=Bot), Mock(spec=Bot)
        offline.call_api = AsyncMock(return_value={"online": False, "good": True})
        online.call_api = AsyncMock(return_value={"online": True, "good": True})
        with patch("nonebot.get_bots", return_value={"one": offline, "two": online}):
            response = await self.client.get("/bot-admin/api/v1/status")
        self.assertTrue(response.json()["qq_connected"])
        offline.call_api.assert_awaited_once_with("get_status")
        online.call_api.assert_awaited_once_with("get_status")

    async def test_all_registered_mutation_routes_deny_member_before_execution(self):
        await self.login("viewer")
        for route, operations in self.app.openapi()["paths"].items():
            if not route.startswith("/bot-admin/api/") or route.endswith(("/auth/login", "/auth/logout")):
                continue
            for method in {key.upper() for key in operations} & {"POST", "PUT", "DELETE", "PATCH"}:
                path = re.sub(r"\{[^}]+\}", "1", route)
                response = await self.client.request(method, path, json={})
                self.assertEqual(response.status_code, 403, f"{method} {path}: {response.text}")

    async def test_admin_sandbox_actions_are_direct_versioned_and_audited(self):
        await self.login()
        path = "/bot-admin/api/v1/sandboxes/s123abc/action"
        for version, action in enumerate(("start", "stop", "destroy")):
            response = await self.client.post(path, json={"action": action}, headers={"If-Match": str(version)})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["resource_version"], version + 1)
        self.sandbox.destroy.assert_awaited_once_with("group:another:user:7", "s123abc")
        self.assertFalse(self.sent)
        audit = (await self.client.get("/bot-admin/api/v1/audit")).json()["items"]
        self.assertEqual({item["action"] for item in audit}, {"sandbox.start", "sandbox.stop", "sandbox.destroy"})
        response = await self.client.post(path, json={"action": "destroy"}, headers={"If-Match": "0"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.sandbox.destroy.await_count, 1)

    async def test_direct_sandbox_actions_still_require_admin_origin_csrf_and_version(self):
        path = "/bot-admin/api/v1/sandboxes/s123abc/action"
        response = await self.client.post(path, json={"action": "destroy"}, headers={"If-Match": "0"})
        self.assertEqual(response.status_code, 401)
        await self.login()
        for headers in ({"Origin": "https://evil.test"}, {"X-CSRF-Token": "wrong"}):
            response = await self.client.post(path, json={"action": "destroy"}, headers={"If-Match": "0", **headers})
            self.assertEqual(response.status_code, 403)
        response = await self.client.post(path, json={"action": "destroy"})
        self.assertEqual(response.status_code, 428)
        self.sandbox.destroy.assert_not_awaited()
        self.assertFalse(self.sent)

    async def test_account_creation_is_direct_without_disclosing_password(self):
        await self.login()
        response = await self.client.post("/bot-admin/api/v1/accounts", json={"username": "newadmin", "password": PASSWORD, "role": "admin", "qq_id": "333333333"})
        self.assertNotIn(PASSWORD, response.text)
        self.assertNotIn("argon2", response.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.sent)
        self.assertIsNotNone(self.store.account_for_qq("333333333"))

    async def test_logout_invalidates_pending_approval(self):
        await self.login()
        await self.client.post("/bot-admin/api/v1/accounts", json={"username": "reviewer", "password": PASSWORD,
            "role": "admin", "qq_id": "333333333"})
        account = self.store.session(self.client.cookies.get("gaoji_session"))
        request = await self.mobile.propose(account, kind="server_task", payload={"id": 1}, summary="test task")
        identifier = request["approval_id"]
        await self.client.post("/bot-admin/api/v1/auth/logout")
        self.assertEqual(self.store.get(identifier, self.account["account_id"])["status"], "cancelled")
        self.assertEqual((await self.client.get("/bot-admin/api/v1/me")).status_code, 401)

    async def test_no_web_code_confirmation_endpoint_exists(self):
        await self.login()
        response = await self.client.post("/bot-admin/api/v1/approvals/AP-123456789ABC/confirm", json={"code": "123456"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
