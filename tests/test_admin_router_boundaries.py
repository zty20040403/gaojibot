from __future__ import annotations

import re
import unittest
from types import SimpleNamespace

import httpx
import nonebot
from fastapi import FastAPI

nonebot.init()

from src.plugins.ai_chat.admin import AdminServices
from tests.admin_session_fixture import PASSWORD, register_admin


# The legacy page surface is intentionally pinned across the router extraction.
PAGE_ROUTES = (
    ("GET", "/api/resource-versions"),
    ("GET", "/api/local-model"),
    ("POST", "/api/local-model/control"),
    ("GET", "/api/overview"),
    ("GET", "/api/events"),
    ("GET", "/api/databases"),
    ("GET", "/api/platforms"),
    ("GET", "/api/deliveries"),
    ("POST", "/api/deliveries/{delivery_id}/retry"),
    ("POST", "/api/deliveries/{delivery_id}/cancel"),
    ("GET", "/api/usage"),
    ("GET", "/api/tasks"),
    ("POST", "/api/tasks/{task_id}/cancel"),
    ("GET", "/api/subagents"),
    ("GET", "/api/subagents/{task_id}"),
    ("GET", "/api/subagents/{task_id}/evidence/{evidence_id}"),
    ("PUT", "/api/subagents/{task_id}/models"),
    ("POST", "/api/subagents/{task_id}/revise"),
    ("POST", "/api/subagents/{task_id}/cancel"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs/{job_id}/cancel"),
    ("POST", "/api/jobs/{job_id}/retry"),
    ("GET", "/api/sandboxes"),
    ("POST", "/api/sandboxes/{sandbox_id}/action"),
    ("GET", "/api/stickers"),
    ("GET", "/api/tools"),
    ("PUT", "/api/tools/{tool_name}/enabled"),
    ("GET", "/api/group-models"),
    ("PUT", "/api/group-models/{group_id}/default"),
    ("PUT", "/api/group-models/{group_id}/enabled"),
    ("PUT", "/api/group-models/{group_id}/vision-auto-describe"),
    ("PUT", "/api/group-models/{group_id}/users/{user_id}"),
    ("PUT", "/api/group-models/{group_id}/reasoning-effort"),
    ("PUT", "/api/group-models/{group_id}/users/{user_id}/reasoning-effort"),
    ("GET", "/api/media"),
    ("PUT", "/api/media/{media_id}/review"),
    ("GET", "/api/sources"),
    ("DELETE", "/api/media/legacy-images"),
    ("GET", "/api/context-plans"),
    ("GET", "/api/context-debug"),
    ("GET", "/api/wiki"),
    ("GET", "/api/wiki/{group_id}"),
    ("PUT", "/api/wiki/{group_id}/settings"),
    ("GET", "/api/context-debug/{turn_id}"),
    ("PUT", "/api/context-debug/{turn_id}/feedback"),
    ("GET", "/api/traces"),
    ("GET", "/api/audit"),
    ("GET", "/api/turn-replay/{scope_kind}/{scope_native_id}/{turn_ordinal}"),
    ("GET", "/api/observability"),
    ("GET", "/api/alerts"),
    ("PUT", "/api/alert-notifications/control"),
)


class AdminRouterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.preferences = SimpleNamespace(value=None)
        self.preferences.enabled_override = lambda: self.preferences.value
        self.preferences.effective_enabled = lambda default: default if self.preferences.value is None else self.preferences.value
        self.preferences.set_enabled = lambda value: setattr(self.preferences, "value", value)
        self.app = self.make_app("/bot-admin")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://test",
            headers={"Origin": "https://test"},
        )
        self.addAsyncCleanup(self.client.aclose)

    def make_app(self, prefix):
        app = FastAPI()
        register_admin(
            app,
            AdminServices(
                version="router-test",
                started_at=1,
                settings=SimpleNamespace(
                    admin_origin="https://test", alert_notify_enabled=False
                ),
                alert_preferences=self.preferences,
            ),
            test_case=self,
            path=prefix,
        )
        self.addCleanup(app.state.mobile.store.close)
        return app

    async def login(self, client=None, prefix="/bot-admin"):
        client = client or self.client
        response = await client.post(prefix + "/api/v1/auth/login", json={
            "username": "kenneth", "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = client.cookies["gaoji_csrf"]

    def api_operations(self, app, prefix="/bot-admin"):
        for path, operations in app.openapi()["paths"].items():
            if not path.startswith(prefix + "/api/") or path.endswith("/auth/login"):
                continue
            for method in operations.keys() & {"get", "post", "put", "delete", "patch"}:
                yield method.upper(), re.sub(r"\{[^}]+\}", "1", path)

    async def test_all_page_routes_keep_legacy_and_v1_paths(self):
        paths = self.app.openapi()["paths"]
        for method, path in PAGE_ROUTES:
            for mounted in ("/bot-admin" + path, "/bot-admin/api/v1" + path.removeprefix("/api")):
                with self.subTest(method=method, path=mounted):
                    self.assertIn(mounted, paths)
                    self.assertIn(method.lower(), paths[mounted])
        self.assertFalse(any("/v1/v1/" in path for path in paths))

    async def test_every_api_route_rejects_anonymous_requests_before_validation(self):
        operations = list(self.api_operations(self.app))
        self.assertGreater(len(operations), 100)
        for method, path in operations:
            with self.subTest(method=method, path=path):
                response = await self.client.request(method, path, json={})
                self.assertEqual(response.status_code, 401, response.text)

    async def test_every_mutation_preserves_csrf_and_origin_checks(self):
        await self.login()
        for method, path in self.api_operations(self.app):
            if method == "GET":
                continue
            for headers in ({"X-CSRF-Token": "incorrect"}, {"Origin": "https://evil.test"}):
                with self.subTest(method=method, path=path, headers=headers):
                    self.app.state.auth_clock += 61
                    response = await self.client.request(method, path, json={}, headers=headers)
                    self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNone(self.preferences.value)

    async def test_aliases_share_versions_audit_and_mutation_state(self):
        await self.login()
        changed = await self.client.put("/bot-admin/api/alert-notifications/control",
            json={"enabled": True}, headers={"If-Match": "0", "X-Admin-Actor": "spoofed"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["resource_version"], 1)
        current = await self.client.get("/bot-admin/api/v1/resource-versions")
        self.assertEqual(current.json()["versions"]["alerts"], 1)
        stale = await self.client.put("/bot-admin/api/v1/alert-notifications/control",
            json={"enabled": False}, headers={"If-Match": "0"})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIs(self.preferences.value, True)
        old_audit = await self.client.get("/bot-admin/api/audit")
        v1_audit = await self.client.get("/bot-admin/api/v1/audit")
        self.assertEqual(old_audit.json(), v1_audit.json())
        self.assertNotIn("spoofed", v1_audit.text)

    async def test_custom_prefix_preserves_dashboard_login_and_protected_aliases(self):
        prefix = "/nested/gaoji"
        app = self.make_app(prefix)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="https://test", headers={"Origin": "https://test"}) as client:
            self.assertEqual((await client.get(prefix)).status_code, 200)
            self.assertEqual((await client.get(prefix + "/favicon.svg")).status_code, 200)
            for suffix in ("/api/overview", "/api/v1/overview"):
                self.assertEqual((await client.get(prefix + suffix)).status_code, 401)
            await self.login(client, prefix)
            for suffix in ("/api/overview", "/api/v1/overview"):
                response = await client.get(prefix + suffix)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["version"], "router-test")
            self.assertEqual((await client.get("/bot-admin/api/v1/overview")).status_code, 404)
