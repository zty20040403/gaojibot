from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import nonebot

nonebot.init()

from src.bot_security.service import MobileAuthorization
from src.plugins.ai_chat.mcp_bridge.client import McpCallError
from src.plugins.ai_chat.mcp_bridge.service import McpService
from src.plugins.ai_chat.mcp_bridge.store import McpDenied
from tests.test_account_security import BOT, QQ
from tests.test_mcp_boundaries import GROUP
from tests import test_mcp_calls as call_fixtures


class McpServiceTests(unittest.IsolatedAsyncioTestCase):
    setUp = call_fixtures.McpCallTests.setUp
    policy = call_fixtures.McpCallTests.policy
    prepare = call_fixtures.McpCallTests.prepare
    read = call_fixtures.McpCallTests.read

    async def asyncSetUp(self):
        self.effects = 0
        self.allowed = True
        self.sent = []
        async def scope(*args):
            return self.allowed
        async def sender(*args):
            self.sent.append(args)
        async def call(config, definition, arguments, *, before_call):
            await before_call()
            self.effects += 1
            return {"ok": True, "source": "external_mcp", "untrusted": True, "content": "test result"}
        self.wire = SimpleNamespace(call=AsyncMock(side_effect=call))
        self.mobile = MobileAuthorization(self.security, sender=sender, bot_selector=lambda: BOT)
        self.service = McpService(self.store, self.wire, scope_allowed=scope, mobile=self.mobile)
        self.mobile.executors["mcp_tool"] = self.service.execute_approved

    async def invoke(self, **changes):
        values = dict(scope_key=GROUP, bot_id=BOT, user_id=QQ, invocation="message:100:call:0",
                      name="example", remote="lookup", arguments={"query": "hello"})
        values.update(changes)
        return await self.service.invoke(**values)

    async def test_safe_tool_is_durable_and_repeated_invocation_returns_saved_result(self):
        first, second = await self.invoke(), await self.invoke()
        self.assertTrue(first["ok"])
        self.assertTrue(first["dispatch_started"])
        self.assertEqual(first, second)
        self.assertEqual(self.effects, 1)
        self.assertFalse(self.sent)

    async def test_high_risk_tool_waits_then_exact_quorum_executor_runs_once(self):
        import re
        self.policy("approval")
        pending = await self.invoke()
        self.assertTrue(pending["approval_required"])
        self.assertFalse(pending["executed"])
        self.assertEqual(self.effects, 0)
        again = await self.invoke()
        self.assertEqual(pending["approval_id"], again["approval_id"])
        self.assertEqual(len(self.sent), 1)
        code = re.search(r"一次性口令：(\d{6})", self.sent[0][2]).group(1)
        self.security.confirm(pending["approval_id"], QQ, BOT, code)
        self.assertFalse(await self.mobile.run_once())
        review, code = self.security.request_review(pending["approval_id"], self.peer["qq_id"], BOT)
        self.security.delivered(review["approval_id"], review["code_generation"], True)
        self.security.confirm(review["approval_id"], self.peer["qq_id"], BOT, code)
        self.assertTrue(await self.mobile.run_once())
        self.assertEqual(self.effects, 1)
        result = await self.invoke()
        self.assertTrue(result["ok"])
        self.assertFalse(await self.mobile.run_once())
        self.assertEqual(self.effects, 1)

    async def test_non_admin_cannot_request_unreviewed_tool_or_borrow_an_approval(self):
        self.policy("approval")
        with self.assertRaises(McpDenied):
            await self.invoke(user_id="333333333")
        self.assertFalse(self.sent)
        self.assertEqual(self.effects, 0)

    async def test_revoked_membership_before_send_blocks_and_hides_result(self):
        async def call(config, definition, arguments, *, before_call):
            self.allowed = False
            await before_call()
            self.fail("Revoked actor reached the remote call")
        self.wire.call.side_effect = call
        with self.assertRaises(McpDenied):
            await self.invoke()
        self.assertEqual(self.prepare()["status"], "failed")
        self.assertEqual(self.effects, 0)

    async def test_uncertain_remote_failure_is_not_automatically_retried(self):
        async def call(config, definition, arguments, *, before_call):
            await before_call()
            self.effects += 1
            raise McpCallError("mcp_call_unconfirmed", may_have_run=True)
        self.wire.call.side_effect = call
        first, second = await self.invoke(), await self.invoke()
        self.assertEqual(first["status"], "needs_attention")
        self.assertFalse(first["executed"])
        self.assertTrue(first["dispatch_started"])
        self.assertEqual(first, second)
        self.assertEqual(self.effects, 1)

    async def test_cancellation_after_persisted_dispatch_is_unconfirmed_after_restart(self):
        dispatched = asyncio.Event()
        async def call(config, definition, arguments, *, before_call):
            await before_call()
            self.effects += 1
            dispatched.set()
            await asyncio.sleep(60)
        self.wire.call.side_effect = call
        task = asyncio.create_task(self.invoke())
        await asyncio.wait_for(dispatched.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.now += 181
        self.store.recover_calls()
        self.assertEqual((await self.invoke())["status"], "needs_attention")
        self.assertEqual(self.effects, 1)

    async def test_catalog_changed_during_connect_does_not_dispatch(self):
        async def call(config, definition, arguments, *, before_call):
            self.policy("approval")
            await before_call()
            self.fail("Stale config was dispatched")
        self.wire.call.side_effect = call
        result = await self.invoke()
        self.assertFalse(result["ok"])
        self.assertFalse(result["dispatch_started"])
        self.assertEqual(result["status"], "failed")

    async def test_scope_checked_before_disclosing_cached_result(self):
        await self.invoke()
        self.allowed = False
        with self.assertRaises(McpDenied):
            await self.invoke()
        self.assertEqual(self.effects, 1)
