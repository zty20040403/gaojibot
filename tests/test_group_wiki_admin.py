from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import nonebot
from fastapi import FastAPI

nonebot.init()

from src.bot_security.passwords import hash_password
from src.plugins.ai_chat.admin import AdminServices, _changed_database_resources
from tests.admin_session_fixture import PASSWORD, register_admin
from tests.test_group_wiki import GroupWikiFixture


class GroupWikiAdminTests(GroupWikiFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        register_admin(self.app, AdminServices(version="test", started_at=1, database=self.db,
            group_wiki_store=self.store, settings=SimpleNamespace(admin_origin="https://test")), test_case=self)
        security = self.app.state.mobile.store
        account = security.account_for_qq("123456789")
        security.apply_account_change(account, {"action": "create", "username": "viewer",
            "password_hash": hash_password(PASSWORD), "role": "member", "qq_id": None})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url="https://test", headers={"Origin": "https://test"})
        self.addAsyncCleanup(self.client.aclose)

    async def login(self, username="kenneth"):
        result = await self.client.post("/bot-admin/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)
        self.client.headers["X-CSRF-Token"] = self.client.cookies["gaoji_csrf"]

    async def test_anonymous_and_member_cannot_read_or_edit_wiki(self):
        for prefix in ("/bot-admin/api", "/bot-admin/api/v1"):
            self.assertEqual((await self.client.get(prefix + "/wiki/100")).status_code, 401)
        await self.login("viewer")
        for method, path in (("GET", "/wiki"), ("GET", "/wiki/100"), ("PUT", "/wiki/100/settings")):
            self.assertEqual((await self.client.request(method, "/bot-admin/api/v1" + path, json={})).status_code, 403)

    async def test_settings_require_two_versions_and_record_audit(self):
        await self.login()
        body = dict(bot_id="301", enabled=True, newcomer_enabled=False, expected_version=0)
        path = "/bot-admin/api/v1/wiki/100/settings"
        self.assertEqual((await self.client.put(path, json=body)).status_code, 428)
        self.assertEqual((await self.client.put(path, json=body, headers={"If-Match": '"0"', "X-CSRF-Token": "wrong"})).status_code, 403)
        response = await self.client.put(path, json=body, headers={"If-Match": '"0"'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["settings"]["version"], 1)
        self.assertEqual(response.json()["resource_version"], 1)
        self.assertEqual((await self.client.put(path, json=body, headers={"If-Match": '"0"'})).status_code, 409)
        self.enable(expected_version=1, newcomer_enabled=True)
        body["expected_version"] = 1
        response = await self.client.put(path, json=body, headers={"If-Match": '"1"'})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.store.settings(self.scope)["newcomer_enabled"])
        audit = (await self.client.get("/bot-admin/api/v1/audit")).json()["items"]
        wiki_audit = [item for item in audit if item["action"] == "wiki.configure"]
        self.assertEqual({item["status"] for item in wiki_audit}, {"succeeded", "failed"})

    async def test_scoped_search_paging_and_hidden_history(self):
        message, _ = self.chapter()
        job = self.job()
        self.store.publish(job, [self.entry(message, title=f"Decision {index}") for index in range(9)], now=100)
        self.jobs.mark_succeeded(job.job_id, job.lease_owner, now=100)
        await self.login()
        overview = (await self.client.get("/bot-admin/api/v1/wiki")).json()
        self.assertEqual(len(overview["groups"]), 1)
        result = (await self.client.get("/bot-admin/api/v1/wiki/100?offset=5&limit=5")).json()
        self.assertEqual((result["total"], len(result["items"])), (9, 4))
        self.assertEqual(result["jobs"]["counts"]["succeeded"], 1)
        self.assertNotIn("payload", result["jobs"]["items"][0])
        self.assertEqual((await self.client.get("/bot-admin/api/wiki/100?query=Decision%207")).json()["total"], 1)
        self.assertEqual((await self.client.get("/bot-admin/api/v1/wiki/200")).json()["items"], [])
        self.ledger.hide_history(self.scope)
        result = (await self.client.get("/bot-admin/api/v1/wiki/100")).json()
        self.assertEqual((result["total"], result["stale_entries"]), (0, 9))

    async def test_invalid_inputs_and_no_model_or_qr_required(self):
        await self.login()
        for path in ("/wiki/-1", "/wiki/0", "/wiki/100?offset=-1", "/wiki/100?limit=1000"):
            self.assertEqual((await self.client.get("/bot-admin/api/v1" + path)).status_code, 422)
        bad = dict(bot_id="-1", enabled="false", newcomer_enabled=False, expected_version=0)
        self.assertEqual((await self.client.put("/bot-admin/api/v1/wiki/100/settings", json=bad, headers={"If-Match": '"0"'})).status_code, 422)
        self.assertEqual(self.app.state.auth_messages, [])

    async def test_database_read_does_not_block_event_loop(self):
        await self.login()
        ticks = 0
        done = asyncio.Event()
        async def ticker():
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.005)
        task = asyncio.create_task(ticker())
        def slow():
            time.sleep(0.08)
            return []
        try:
            with patch.object(self.store, "admin_groups", side_effect=slow):
                self.assertEqual((await self.client.get("/bot-admin/api/v1/wiki")).status_code, 200)
        finally:
            done.set()
            await task
        self.assertGreater(ticks, 5)

    def test_wiki_and_source_invalidation_reaches_realtime_resource(self):
        for table in ("group_wiki_settings", "group_wiki_pages", "durable_jobs", "context_compartments", "conversation_visibility"):
            with self.subTest(table=table):
                self.assertIn("wiki", _changed_database_resources({table: (1, 0, 0)}, {table: (1, 1, 0)}))
        self.assertNotIn("wiki", _changed_database_resources({"messages": (1, 0, 0)}, {"messages": (2, 0, 0)}))
        self.assertIn("wiki", _changed_database_resources({"messages": (1, 0, 0)}, {"messages": (1, 1, 0)}))
        self.assertIn("wiki", _changed_database_resources({"messages": (1, 0, 0)}, {"messages": (1, 0, 1)}))
