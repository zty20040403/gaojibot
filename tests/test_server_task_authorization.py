from __future__ import annotations

import asyncio
import copy
import os
import re
import unittest
from types import SimpleNamespace

import nonebot

os.environ.setdefault("AI_ALLOW_LEGACY_SQLITE", "true")
nonebot.init()

from src.bot_security.service import MobileAuthorization, current_principal
from src.bot_security.store import SecurityError, SecurityStore
from src.plugins.ai_chat.fleet_authorization import FleetAuthorization
from src.plugins.ai_chat.server_task_authorization import ServerTaskAuthorization, server_task
from src.plugins.ai_chat.agent.external import active_external
from tests.test_account_security import PASSWORD, QQ, BOT


class ServerTaskAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SecurityStore(":memory:", b"x" * 32)
        self.addCleanup(self.store.close)
        self.account = self.store.bootstrap("kenneth", PASSWORD, QQ)
        self.sent = []
        async def send(bot, qq, text):
            self.sent.append(text)
        self.mobile = MobileAuthorization(self.store, sender=send, bot_selector=lambda: BOT)
        self.task = SimpleNamespace(task_id=7, scope_key="group:test", requester_user_id=int(QQ),
            objective="检查并修复本任务涉及的服务", status="running", cancel_requested=False, finished_at=None)
        self.revision = 1
        self.context = SimpleNamespace(mobile_authorization=self.mobile,
            subagent_store=SimpleNamespace(get=lambda task_id: self.task if task_id == 7 else None,
                control=lambda _: {"revision": self.revision}),
            turn_journal=SimpleNamespace(get_turn_by_id=lambda _: None))
        self.tasks = ServerTaskAuthorization(self.context)
        self.scope = {"kind": "task", "id": 7, "scope": "group:test", "qq_id": QQ,
            "bot_id": BOT, "revision": 1, "objective": self.task.objective}
        self.records, self.writes = {}, []

        async def raw(method, path, body=None, **kwargs):
            if path == "/v1/operations?limit=200":
                return {"items": []}
            if path.endswith("/prepare"):
                key = "/v1/operations/op_" + str(body["host_id"])
                self.records[key] = {"operation_id": key.rsplit("/", 1)[1], "host_id": body["host_id"],
                    "status": "awaiting_approval", "contract_hash": "hash", "resource_version": 1}
                return copy.deepcopy(self.records[key])
            key = path.removesuffix("/approve")
            if method == "POST":
                self.writes.append(path)
                self.records[key]["status"] = "queued"
            return copy.deepcopy(self.records[key])
        self.raw = raw
        self.fleet = FleetAuthorization(SimpleNamespace(_raw_request=raw), self.mobile)
        self.fleet.tasks = self.tasks

    async def confirm(self):
        async with asyncio.timeout(3):
            while not self.sent:
                await asyncio.sleep(0.01)
        self.assertRegex(self.sent[0], r"AP-[A-F0-9]+")
        code = re.search(r"一次性口令：([0-9]{6})", self.sent[0])[1]
        response = await self.mobile.handle_message(qq_id=QQ, bot_id=BOT, private=True, text=code)
        self.assertIn("已授权", response)
        await self.mobile.run_once()

    async def prepare(self, host):
        token = server_task.set(self.scope)
        try:
            return await self.fleet.request("POST", "/v1/operations/prepare", {"host_id": host},
                actor="qq:" + QQ, origin="group:test")
        finally:
            server_task.reset(token)

    async def authorize(self):
        self.assertFalse(await self.tasks.ensure(self.scope, self.account, wait=False))
        await self.confirm()

    async def test_parallel_steps_share_one_approval_and_continue_automatically(self):
        jobs = [asyncio.create_task(self.prepare(host)) for host in ("h610", "tank")]
        try:
            await self.confirm()
            results = await asyncio.wait_for(asyncio.gather(*jobs), timeout=4)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        self.assertEqual(len([s for s in self.sent if "一次性口令：" in s]), 1)

        self.assertTrue(all(r["submitted"] and not r["approval_required"] for r in results))
        await self.prepare("h310")
        self.assertEqual(len(self.writes), 3)
        self.assertEqual(len([s for s in self.sent if "一次性口令：" in s]), 1)

    async def test_durable_task_returns_approval_handle_without_holding_worker(self):
        token = active_external.set(object())
        try:
            response = await asyncio.wait_for(self.prepare("h610"), timeout=2)
        finally:
            active_external.reset(token)
        self.assertTrue(response["approval_required"])
        self.assertEqual(response["operation"]["operation_id"], "op_h610")
        self.assertEqual(response["operation"]["status"], "awaiting_approval")
        self.assertFalse(self.writes)
        await self.confirm()
        await self.fleet.poll()
        self.assertEqual(self.writes, ["/v1/operations/op_h610/approve"])

    async def test_completed_cancelled_revised_and_other_scope_cannot_reuse_grant(self):
        await self.authorize()
        for changes in ({"status": "completed"}, {"cancel_requested": True}, {"scope_key": "group:other"},
                        {"requester_user_id": 99999}, {"objective": "另一个任务"}):
            original = vars(self.task).copy()
            vars(self.task).update(changes)
            with self.assertRaises(SecurityError):
                await self.tasks.ensure(self.scope, self.account, wait=False)
            vars(self.task).update(original)
        self.revision = 2
        with self.assertRaises(SecurityError):
            await self.tasks.ensure(self.scope, self.account, wait=False)
        self.assertFalse(self.writes)

    async def test_account_change_revokes_grant(self):
        await self.authorize()
        self.store.apply_account_change(self.account, {"action": "update", "account_id": self.account["account_id"],
            "expected_version": 1, "changes": {"qq_id": "333333333"}})
        with self.assertRaises(SecurityError):
            await self.tasks.ensure(self.scope, self.account, wait=False)

    async def test_restarted_watcher_uses_persisted_task_binding_without_new_code(self):
        await self.authorize()
        record = await self.raw("POST", "/v1/operations/prepare", {"host_id": "h610"})
        token = server_task.set(self.scope)
        try:
            await self.fleet.watch(record, self.account, "admin:kenneth", "group:test")
        finally:
            server_task.reset(token)
        restarted = FleetAuthorization(SimpleNamespace(_raw_request=self.raw), self.mobile)
        restarted.tasks = ServerTaskAuthorization(self.context)
        await restarted.poll()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len([s for s in self.sent if "一次性口令：" in s]), 1)

    async def test_admin_console_approve_is_direct_but_still_checks_contract(self):
        login = self.store.login("kenneth", PASSWORD, "test")
        token = current_principal.set(self.store.session(login["session"]))
        try:
            result = await self.fleet.request("POST", "/v1/operations/prepare", {"host_id": "h610"},
                actor="admin:kenneth", origin="admin-console")
            self.assertEqual(result["operation"]["status"], "awaiting_approval")
            self.assertFalse(self.sent)
            path = "/v1/operations/op_h610/approve"
            with self.assertRaises(SecurityError):
                await self.fleet.request("POST", path, {"contract_hash": "old", "resource_version": 1},
                    actor="admin:kenneth", origin="admin-console")
            await self.fleet.request("POST", path, {"contract_hash": "hash", "resource_version": 1},
                actor="admin:kenneth", origin="admin-console")
            self.assertEqual(self.writes, [path])
            self.assertFalse(self.sent)
        finally:
            current_principal.reset(token)

    async def test_missing_task_blocks_unclassified_host_mutations(self):
        with self.assertRaises(SecurityError):
            await self.fleet.request("POST", "/v1/host-shell", {"command": "anything"},
                actor="qq:" + QQ, origin="group:test")
        self.assertFalse(self.writes)
