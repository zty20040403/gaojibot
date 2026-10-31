"""The chat window, the memory file and the history tool against real PostgreSQL."""
from __future__ import annotations

from datetime import datetime
import json
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent_loop import _build_messages
from src.plugins.ai_chat.application.turn_context import render_memory
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.conversation_window import ConversationStore, build_tail
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.prompt import system_prompt
from src.plugins.ai_chat.tools import history as history_tool
from src.plugins.ai_chat.tools import memory as memory_tool
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.postgres_fixture import postgres_for_test

DAY = int(datetime(2026, 8, 11, 12, 0).timestamp())


class WindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = postgres_for_test(self, reset=True)
        self.ledger = MessageLedger(self.database)
        self.store = ConversationStore(self.database)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.store.close)
        self.scope = ConversationScope("onebot-v11", "group", "101")
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)
        self.ids = []

    def say(self, text, *, who="小明", at=DAY, direction="inbound"):
        message = self.ledger.record_message(
            self.scope, native_message_id=f"TEST_{len(self.ids)}", sender_native_user_id=str(len(who)),
            sender_display=who, body=MessageBody((TextNode(0, text),)), occurred_at=at, direction=direction)
        self.ids.append(message.canonical_message_id)
        return message

    def turn(self):
        app = SimpleNamespace(message_ledger=self.ledger, conversations=self.store, db_call=self.calls)
        context = ToolContext(SimpleNamespace(context=app), capabilities=frozenset({"context", "memory"}))
        return TurnState(context)

    async def call(self, module, name, **arguments):
        source = SimpleNamespace(scope=self.scope)
        with patch.object(module, "structured_tool_source", return_value=source):
            return json.loads(await getattr(module, name)(self.turn(), arguments))

    async def test_history_searches_reads_from_the_start_and_by_person(self):
        self.say("第一条：显卡价格怎么样", at=DAY)
        self.say("⚙ sandbox_exec {}", who="别的bot", at=DAY + 60)
        self.say("我觉得 5070 还行", who="小红", at=DAY + 86400)
        self.say("收到", direction="outbound", who="Bot", at=DAY + 86460)
        found = await self.call(history_tool, "history", query="显卡 5070")
        self.assertEqual(len(found["results"]), 2)
        self.assertFalse(any("sandbox_exec" in line for line in found["results"]))
        earliest = await self.call(history_tool, "history", **{"from": "2020-01-01", "limit": 2})
        self.assertIn("第一条", earliest["lines"][0])
        self.assertEqual(len(earliest["lines"]), 2)
        self.assertIn("next", earliest)
        by_person = await self.call(history_tool, "history", who="小红")
        self.assertEqual(len(by_person["lines"]), 1)
        around = await self.call(history_tool, "history", around=f"#{self.ids[2]}")
        self.assertTrue(any("你 #" in line for line in around["lines"]))
        self.assertFalse((await self.call(history_tool, "history"))["ok"])
        self.assertFalse((await self.call(history_tool, "history", **{"from": "yesterday"}))["ok"])

    def test_tail_start_stays_put_until_overflow_then_jumps_forward(self):
        for index in range(30):
            self.say(f"消息{index} " + "字" * 40, at=DAY + index)
        first = build_tail(self.ledger, self.store, self.scope, high_tokens=10_000)
        self.assertIn("消息0", first)
        self.assertEqual(self.store.tail_after(self.scope.key), 0)
        small = build_tail(self.ledger, self.store, self.scope, high_tokens=500)
        anchor = self.store.tail_after(self.scope.key)
        self.assertGreater(anchor, 0)
        self.assertNotIn("消息0 ", small)
        self.say("新来的一条", at=DAY + 100)
        again = build_tail(self.ledger, self.store, self.scope, high_tokens=500)
        self.assertEqual(self.store.tail_after(self.scope.key), anchor)
        self.assertTrue(again.startswith(small.split("\n")[0]))
        current = self.ids[-1]
        self.assertNotIn("新来的一条", build_tail(self.ledger, self.store, self.scope, high_tokens=500,
                                              exclude_ids=frozenset({current})))

    async def test_memory_append_write_secret_guard_and_tidy_conflict(self):
        self.assertTrue((await self.call(memory_tool, "memory_append", line="小明：喜欢叫我高高（10-31 #1）"))["ok"])
        await self.call(memory_tool, "memory_append", line="- 群规：不刷屏")
        content = self.store.memory(self.scope.key)
        self.assertEqual(content.splitlines(), ["- 小明：喜欢叫我高高（10-31 #1）", "- 群规：不刷屏"])
        self.assertFalse((await self.call(memory_tool, "memory_append", line="密码: hunter2"))["ok"])
        await self.call(memory_tool, "memory_write", content="## 群里\n- 群规：不刷屏\n")
        original = self.store.memory(self.scope.key)
        self.assertEqual([key for key, _ in self.store.memories_to_tidy()], [self.scope.key])
        self.assertFalse(self.store.save_tidied(self.scope.key, "stale text", "## x"))
        self.assertTrue(self.store.save_tidied(self.scope.key, original, "## 群里\n- 不刷屏\n"))
        self.assertEqual(self.store.memories_to_tidy(), [])

    def test_memory_is_capped_with_a_note(self):
        self.assertEqual(render_memory(""), "")
        self.assertIn("后面的没加载", render_memory("\n".join("- 条目" + "字" * 50 for _ in range(200))))

    def test_chat_layout_puts_stable_parts_first_and_new_message_last(self):
        messages = _build_messages("在吗", [], group_context="[08-11 12:00 小明 #1] hi",
            memory_context="- 小明：x", current_user="小红", system_prompt=system_prompt())
        self.assertEqual([m["role"] for m in messages], ["system", "user", "user"])
        self.assertIn("[silence]", messages[0]["content"])
        self.assertTrue(messages[1]["content"].startswith("[聊天记录]"))
        last = messages[2]["content"]
        self.assertTrue(last.startswith("[现在]"))
        self.assertLess(last.index("[记忆]"), last.index("[新消息]"))
        self.assertTrue(last.endswith("[新消息]\n小红：在吗"))
        self.assertIn("自定义人设", system_prompt("自定义人设"))

    def test_style_examples_sit_between_persona_and_rules(self):
        text = system_prompt("TEST 人设", "", "群友：TEST 问\n你：TEST 答")
        self.assertLess(text.index("TEST 人设"), text.index("[说话样例]"))
        self.assertLess(text.index("你：TEST 答"), text.index("[silence]"))
        self.assertNotIn("[说话样例]", system_prompt("TEST 人设"))

    def test_persona_and_examples_files_win_over_the_variable(self):
        import tempfile
        from src.plugins.ai_chat.config import Settings
        with tempfile.TemporaryDirectory() as folder:
            persona, examples = Path(folder) / "persona.md", Path(folder) / "examples.md"
            persona.write_text("TEST 文件人设\n", encoding="utf-8")
            examples.write_text("群友：TEST\n", encoding="utf-8")
            settings = Settings.from_env({"AI_SYSTEM_PROMPT": "TEST 变量人设",
                                          "AI_SYSTEM_PROMPT_FILE": str(persona),
                                          "AI_STYLE_EXAMPLES_FILE": str(examples)})
        self.assertEqual(settings.system_prompt, "TEST 文件人设")
        self.assertEqual(settings.style_examples, "群友：TEST")


if __name__ == "__main__":
    unittest.main()
