"""Acceptance tests for §7 reply flow: mid-turn merge, hybrid proactive wakeup, and real @mention."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import re
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.proactive import parse_proactive_decision
from tests import test_qq_official_turn_endtoend as base
from tests.test_qq_official_runtime_sender import verified_ready_platform


class MaxReplyFlowTests(base.OfficialTurnFixture, unittest.IsolatedAsyncioTestCase):
    def test_classifier_requires_one_action_and_bounds_its_note(self):
        for action in ('speak', 'sticker'):
            decision = parse_proactive_decision({'action': action, 'reason': ' 庆祝' * 40})
            self.assertEqual(decision.action, action)
            self.assertEqual(len(decision.reason), 40)
        silent = parse_proactive_decision({'action': 'silent', 'classification_reason': '群友之间的对话'})
        self.assertEqual((silent.reason, silent.classification_reason), ('', '群友之间的对话'))
        for action in ('speak', 'sticker', 'silent'):
            for basis in ('TEST_MEMBER 原文', '群友之间的对话\nTEST_INJECTION', '长' * 5000, None):
                decision = parse_proactive_decision({'action': action, 'reason': 'TEST', 'classification_reason': basis})
                self.assertIsNone(decision.classification_reason)
        for payload in ({'reply': '旧格式'}, {'action': 'sticker', 'reason': ''}, {'action': 'unknown'}, {'action': []}):
            with self.assertRaisesRegex(ValueError, 'invalid proactive decision'):
                parse_proactive_decision(payload)

    async def test_group_roster_injected_and_mention_id_renders_qqbot_at_user(self):
        app = self.application()
        seen_prompts: list[str] = []

        async def completion(**kwargs):
            text_blob = json.dumps(kwargs["messages"], ensure_ascii=False)
            seen_prompts.append(text_blob)
            match_alice = re.search(r"\[mention#(\d+)\]\s*Alice", text_blob)
            match_bob = re.search(r"\[mention#(\d+)\]\s*Bob", text_blob)
            assert match_alice and match_bob, text_blob
            alice_pid, bob_pid = match_alice.group(1), match_bob.group(1)
            reply = f"没问题 [mention#{alice_pid}] 给你弄好了\n```\n[mention#{bob_pid}]\n```"
            return base.model_response(reply, [], kwargs)

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                # Seed unbound message to create group endpoint row, then activate binding
                await self.emit(fake, native="TEST_SETUP_M", content="init", group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_SETUP_M'")
                endpoint = EndpointKey(runtime.loaded.account, "group", "TEST_GROUP")
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?", (endpoint.key,))

                # Bob chats passively so Bob is in the group's ledger & roster
                await fake.emit("GROUP_MESSAGE_CREATE", {
                    "id": "TEST_BOB_MSG",
                    "content": "大家早上好",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_BOB_OPENID", "username": "Bob"},
                })
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_BOB_MSG'")

                # Alice @mentions the bot
                await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                    "id": "TEST_ALICE_AT",
                    "content": "<@!TEST_BOT_MENTION> 帮我看一下",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_ALICE_OPENID", "username": "Alice"},
                    "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                })
                await self.complete("TEST_ALICE_AT")
                self.assertTrue(seen_prompts)
                self.assertIn("[上下文里的人]", seen_prompts[0])
                self.assertGreaterEqual(len(fake.received), 1)
                combined_markdown = "\n".join(item["payload"]["markdown"]["content"] for item in fake.received)
                self.assertIn('<qqbot-at-user id="TEST_ALICE_OPENID" />', combined_markdown)
                self.assertNotIn('<qqbot-at-user id="TEST_BOB_OPENID" />', combined_markdown)
                await runtime.stop()

    async def test_mid_turn_from_another_member_keeps_initial_effort_and_merges(self):
        from src.plugins.ai_chat.model_catalog import ModelCatalog
        app = self.application()
        app.settings = replace(app.settings, reasoning_router_profile="test")
        prof = app.model_catalog.resolve("test").with_reasoning_effort("high")
        app.model_catalog = ModelCatalog(profiles={"test": prof}, default_profile="test")
        router_calls = 0
        calls = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fake_router(catalog, profile_name, text):
            nonlocal router_calls
            router_calls += 1
            return "high"

        async def completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                await release.wait()
                for _ in range(30):
                    st = next(iter(runtime.active_conversation_turns.values()), None)
                    if st is not None and st.pending:
                        break
                    await asyncio.sleep(0.02)
                return base.model_response("first draft", [], kwargs)
            return base.model_response("combined reply for both", [], kwargs)

        with patch("src.plugins.ai_chat.reasoning_router.chat_effort", new=fake_router), \
                patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native="TEST_SETUP_E", content="init", group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_SETUP_E'")
                endpoint = EndpointKey(runtime.loaded.account, "group", "TEST_GROUP")
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?", (endpoint.key,))

                await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                    "id": "TEST_M1",
                    "content": "<@!TEST_BOT_MENTION> 推导一下欧拉公式",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_U1", "username": "UserOne"},
                    "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                })
                await asyncio.wait_for(entered.wait(), 5)
                await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                    "id": "TEST_M2",
                    "content": "<@!TEST_BOT_MENTION> 顺便举个例子",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_U2", "username": "UserTwo"},
                    "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                })
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_M2' AND status='processed'")
                release.set()
                await self.complete("TEST_M1")
                self.assertEqual(router_calls, 1)
                self.assertEqual(len(calls), 2)
                second_blob = json.dumps(calls[1]["messages"], ensure_ascii=False)
                self.assertIn("顺便举个例子", second_blob)
                self.assertEqual(len(fake.received), 1)
                await runtime.stop()
