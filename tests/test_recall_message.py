"""Acceptance tests for §8A recall_message tool: own outbound message within 120s only."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_turn_endtoend as base
from tests.test_qq_official_runtime_sender import verified_ready_platform


def _tool_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        index=0,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


class RecallMessageTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.OfficialTurnEndToEndTests.setUp
    runtime = base.OfficialTurnEndToEndTests.runtime
    query = base.OfficialTurnEndToEndTests.query
    wait_db = base.OfficialTurnEndToEndTests.wait_db
    application = base.OfficialTurnEndToEndTests.application
    open_runtime = base.OfficialTurnEndToEndTests.open_runtime
    emit = base.OfficialTurnEndToEndTests.emit
    complete = base.OfficialTurnEndToEndTests.complete

    async def test_recalls_own_recent_message_and_rejects_others_or_expired(self):
        app = self.application()
        step = 0
        tool_results: list[dict] = []

        async def completion(**kwargs):
            nonlocal step
            step += 1
            if step == 1:
                return base.model_response("说错了一句话", [], kwargs)
            if step == 2:
                # Try recalling inbound user message -> must fail (forbidden)
                inbound_cid = self.query(
                    "SELECT canonical_message_id FROM messages WHERE native_message_id='TEST_R_TURN1'"
                )[0]["canonical_message_id"]
                return base.model_response("", [_tool_call("c1", "recall_message", {"id": inbound_cid})], kwargs)
            if step == 3:
                last_tool = next(m for m in reversed(kwargs["messages"]) if m.get("role") == "tool")
                tool_results.append(json.loads(last_tool["content"].split("\n")[1]))
                # Recall bot's own outbound message (#id as "#N" string)
                bot_cid = self.query(
                    "SELECT canonical_message_id FROM messages WHERE direction='outbound' ORDER BY canonical_message_id DESC LIMIT 1"
                )[0]["canonical_message_id"]
                return base.model_response("", [_tool_call("c2", "recall_message", {"id": f"#{bot_cid}"})], kwargs)
            if step == 4:
                last_tool = next(m for m in reversed(kwargs["messages"]) if m.get("role") == "tool")
                tool_results.append(json.loads(last_tool["content"].split("\n")[1]))
                # Duplicate recall of same bot message -> already_recalled
                bot_cid = self.query(
                    "SELECT canonical_message_id FROM messages WHERE direction='outbound' ORDER BY canonical_message_id DESC LIMIT 1"
                )[0]["canonical_message_id"]
                return base.model_response("", [_tool_call("c3", "recall_message", {"id": bot_cid})], kwargs)
            if step == 5:
                last_tool = next(m for m in reversed(kwargs["messages"]) if m.get("role") == "tool")
                tool_results.append(json.loads(last_tool["content"].split("\n")[1]))
                return base.model_response("已经撤回啦", [], kwargs)
            if step == 6:
                # Turn 3: try recalling an expired outbound message (>120s old)
                old_cid = self.query(
                    "SELECT canonical_message_id FROM messages WHERE native_message_id='TEST_SENT_2'"
                )[0]["canonical_message_id"]
                return base.model_response("", [_tool_call("c4", "recall_message", {"id": old_cid})], kwargs)
            last_tool = next(m for m in reversed(kwargs["messages"]) if m.get("role") == "tool")
            tool_results.append(json.loads(last_tool["content"].split("\n")[1]))
            return base.model_response("超过两分钟撤不掉了", [], kwargs)

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native="TEST_R_INIT", content="init", group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_R_INIT'")
                endpoint = EndpointKey(runtime.loaded.account, "group", "TEST_GROUP")
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?", (endpoint.key,))

                # Turn 1: Bot sends TEST_SENT_1
                await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                    "id": "TEST_R_TURN1",
                    "content": "<@!TEST_BOT_MENTION> 随便说一句",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_USER_A", "username": "UserA"},
                    "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                })
                await self.complete("TEST_R_TURN1")
                self.assertEqual(len(fake.received), 1)
                self.assertEqual(fake.received[0]["receipt"]["id"], "TEST_SENT_1")

                # Turn 2: User asks bot to recall -> tests forbidden inbound, ok own recall, and already_recalled
                await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                    "id": "TEST_R_TURN2",
                    "content": "<@!TEST_BOT_MENTION> 把刚才那条撤了",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_USER_A", "username": "UserA"},
                    "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                })
                await self.complete("TEST_R_TURN2")
                self.assertEqual(len(fake.recalled), 1)
                self.assertEqual(fake.recalled[0], {"kind": "group", "target": "TEST_GROUP", "message_id": "TEST_SENT_1"})

                # Age TEST_SENT_2 past 120s and test expired rejection
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE messages SET occurred_at = occurred_at - 300 WHERE native_message_id='TEST_SENT_2'")

                await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                    "id": "TEST_R_TURN3",
                    "content": "<@!TEST_BOT_MENTION> 把上一条也撤了",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_USER_A", "username": "UserA"},
                    "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                })
                await self.complete("TEST_R_TURN3")
                self.assertEqual(len(fake.recalled), 1)

                self.assertEqual(tool_results[0]["code"], "forbidden")
                self.assertTrue(tool_results[1]["ok"])
                self.assertEqual(tool_results[2]["code"], "already_recalled")
                self.assertEqual(tool_results[3]["code"], "expired")
                await runtime.stop()
