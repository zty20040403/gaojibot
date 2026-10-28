"""Both platform preparations exercise the actual shared context and agent path."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot
nonebot.init()

from src.plugins.ai_chat import tool_executor
from src.plugins.ai_chat.application import shared_agent_turn
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.message_lowering import OutboundCapabilities
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures
from src.plugins.ai_chat.turn_journal import ReplayBundle, TurnJournal
from tests.handler_fixture import HandlerTestCaseMixin
from tests.postgres_fixture import postgres_for_test
from tests.test_natural_tool_routing import _group_event


def official(*, app_id="TEST_APP_A", kind="group", target="TEST_OPENID:群", message_id="TEST_MSG_CURRENT"):
    account = AccountKey("qq-official", app_id, "test")
    endpoint = EndpointKey(account, kind, target)
    actor = ActorKey(account, "group_member" if kind == "group" else "c2c_user",
                     target if kind == "group" else "", "TEST_ACTOR:人" if kind == "group" else target)
    inbound = InboundMessage(
        endpoint, actor, "TEST_EVENT", message_id, "测试发言人", "owner",
        MessageBody((TextNode(0, "测试问题"),)), True, None, 1000, None,
    )
    platform = SimpleNamespace(account=account, features=PlatformFeatures(), capabilities=OutboundCapabilities())
    return inbound, platform


def completion(text="", *, tool=None, arguments=None):
    calls = [] if tool is None else [SimpleNamespace(
        id="TEST_CALL", function=SimpleNamespace(name=tool, arguments=json.dumps(arguments or {})),
    )]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))], usage=None)


class SharedAgentTurnTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.app_context.settings = replace(self.app_context.settings, tool_intent_enabled=False)
        self.app_context.model_catalog = ModelCatalog.from_json(json.dumps({
            "default": "test-model", "profiles": {"test-model": {
                "model": "TEST_MODEL", "api_key_required": False,
            }},
        }), default_profile="test-model", environ={})
        self.profile = self.app_context.model_catalog.resolve_preference(None)
        self.handlers.scope_enabled = lambda scope: True

    def reject_legacy_helpers(self):
        for owner, names in (
            (self.handlers.chat, ("_conversation_id", "_conversation_scope", "_preferred_model_profile",
                "_group_turn_context_plan", "_current_group_context", "_current_user_identity",
                "_current_turn_context", "_voice_cache_key", "_record_turn_loop_event")),
            (self.handlers.commands, ("_memory_scopes", "_current_long_term_memory", "_resolve_ocr_sources",
                "_resolve_voice_message_id")),
            (self.handlers.tools, ("_registered_admin", "_alert_tools_allowed", "_fleet_tools_allowed",
                "_fleet_logs_allowed", "_tool_intent_context", "_resolve_video_reference", "_ask_legacy_inbound")),
        ):
            for name in names:
                self.enterContext(patch.object(owner, name, side_effect=AssertionError("legacy helper was called")))
        for name in ("scope_from_event", "current_project_context", "routes_to_project", "image_sources",
                     "contains_video", "contains_voice", "reply_message_id", "ai_reply_message"):
            self.enterContext(patch.object(tool_executor, name, side_effect=AssertionError("legacy function was called")))

    def setup_storage(self):
        database = postgres_for_test(self)
        self.app_context.message_ledger = MessageLedger(database)
        self.app_context.context_store = ContextStore(
            database, input_budget_tokens=1000, high_watermark_tokens=500,
            low_watermark_tokens=300, compartment_target_tokens=250, raw_tail_min_messages=3,
        )
        self.app_context.turn_journal = TurnJournal(database)
        for store in (self.app_context.message_ledger, self.app_context.context_store, self.app_context.turn_journal):
            self.addCleanup(store.close)

    def record(self, inbound, native_id, text, *, occurred_at=1):
        return self.app_context.message_ledger.record_message(
            inbound.scope, native_message_id=native_id, sender_native_user_id=inbound.actor.native_user_id,
            sender_display=inbound.sender_display, body=MessageBody((TextNode(0, text),)),
            occurred_at=occurred_at, actor=inbound.actor, source_endpoint=inbound.endpoint,
        )

    async def test_official_runs_real_tool_loop_without_any_legacy_event(self):
        self.reject_legacy_helpers()
        inbound, platform = official(kind="private", target="TEST_PRIVATE:非数字")
        trace = AgentTrace()
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock(side_effect=[
            completion(tool="use_skill", arguments={"name": "TEST_MISSING_SKILL"}),
            completion("共享循环完成"),
        ])) as model:
            answer = await self.handlers.tools._ask_inbound(
                inbound, platform, "检查可用技能", selected_profile_override=self.profile, turn_trace=trace,
            )
        self.assertEqual(answer, "共享循环完成")
        self.assertEqual(model.await_count, 2)
        messages = model.await_args_list[-1].kwargs["messages"]
        receipts = [message["content"] for message in messages if message["role"] == "tool"]
        self.assertTrue(receipts)
        self.assertIn('"ok": false', receipts[0])
        self.assertIn('"available":', receipts[0])
        self.assertIn('kind="tool_result"', receipts[0])
        self.assertEqual(self.app_context.memory.get(conversation_key(inbound))[-1]["content"], answer)

    async def test_official_owner_role_cannot_enable_host_or_persistent_tools(self):
        self.reject_legacy_helpers()
        inbound, platform = official()
        seen = {}

        async def model(text, history, tools, execute_tool, **kwargs):
            seen["tools"] = {tool["function"]["name"] for tool in tools}
            for name in ("sandbox_exec", "delegate_agent", "run_subagents", "project_create", "send_sticker"):
                result = json.loads(await execute_tool(name, {}))
                self.assertFalse(result["ok"], name)
                self.assertNotIn(name, seen["tools"])
            group_write = json.loads(await execute_tool("memory_add", {"scope": "group", "content": "群主不能自行授权"}))
            self.assertEqual(group_write["code"], "memory_permission_denied")
            personal_write = json.loads(await execute_tool("memory_add", {"scope": "user", "content": "喜欢绿茶"}))
            self.assertTrue(personal_write["ok"])
            return "仅使用当前已验证的能力"

        with patch.object(tool_executor, "ask_deepseek_with_tools", new=model):
            await self.handlers.tools._ask_inbound(inbound, platform, "检查权限", selected_profile_override=self.profile)
        self.assertIn("use_skill", seen["tools"])
        self.assertNotIn("query_alerts", seen["tools"])

    async def test_onebot_and_official_enter_identical_shared_stages(self):
        stages = ("prepare_shared_turn_context", "build_shared_prompt_context", "run_shared_agent_turn", "append_shared_turn_memory")
        spies = {name: self.enterContext(patch.object(shared_agent_turn, name, wraps=getattr(shared_agent_turn, name))) for name in stages}
        with patch.object(tool_executor, "ask_deepseek_with_tools", new=AsyncMock(return_value="同一核心")):
            await self.handlers.tools._ask_ai(AsyncMock(), _group_event(), "OneBot问题", available_image_sources=[], selected_profile_override=self.profile)
            inbound, platform = official()
            await self.handlers.tools._ask_inbound(inbound, platform, "官方问题", selected_profile_override=self.profile)
        for name, spy in spies.items():
            self.assertEqual(spy.await_count, 2, name)
        self.assertIn("同一核心", self.app_context.memory.get("group:789:user:321")[-1]["content"])
        self.assertIn("同一核心", self.app_context.memory.get(conversation_key(inbound))[-1]["content"])

    async def test_official_uses_real_historian_projection_and_actor_memory(self):
        self.setup_storage()
        self.reject_legacy_helpers()
        inbound, platform = official()
        other, _ = official(app_id="TEST_APP_B")
        for index in range(1, 20):
            self.record(inbound, f"TEST_OLD_{index}", f"甲账号项目决定 {index} " + "证据" * 120, occurred_at=index)
        self.record(other, "TEST_FOREIGN", "乙账号禁止混入的历史")
        self.record(inbound, inbound.native_message_id, "TEST_CURRENT_EXCLUDED", occurred_at=inbound.occurred_at)
        projection = self.app_context.context_store.build_projection(self.app_context.message_ledger, inbound.scope)
        self.assertTrue(projection.compartment_handles)
        self.app_context.long_term_memory.add(inbound.actor.key, "user", "甲账号个人记忆：爱喝绿茶",
            creator_user_id=None, creator_actor_key=inbound.actor.key)
        self.app_context.long_term_memory.add(other.actor.key, "user", "乙账号个人记忆禁止混入",
            creator_user_id=None, creator_actor_key=other.actor.key)
        model = AsyncMock(return_value="记得绿茶")
        with patch.object(tool_executor, "ask_deepseek_with_tools", new=model):
            await self.handlers.tools._ask_inbound(inbound, platform, "还记得我的个人习惯和项目决定吗？", selected_profile_override=self.profile)
        options = model.await_args.kwargs
        self.assertIn("conversation compartments", options["group_context"])
        self.assertIn("甲账号项目决定", options["group_context"])
        self.assertNotIn("乙账号禁止", options["group_context"])
        self.assertNotIn("TEST_CURRENT_EXCLUDED", options["group_context"])
        self.assertIn("绿茶", options["memory_context"])
        self.assertNotIn("乙账号个人记忆", options["memory_context"])

    async def test_official_replay_and_quote_lookup_keep_endpoint_evidence(self):
        self.setup_storage()
        self.reject_legacy_helpers()
        inbound, platform = official()
        quoted = self.record(inbound, "TEST_QUOTE", "可信引用内容")
        current = self.record(inbound, inbound.native_message_id, "继续")
        journal = self.app_context.turn_journal
        old = journal.start_turn(inbound.scope, trigger_canonical_message_id=quoted.canonical_message_id,
                                 objective="旧回合", provider="test", model="TEST_MODEL")
        journal.finish_turn(old.turn_id, status="succeeded")
        new = journal.start_turn(inbound.scope, trigger_canonical_message_id=current.canonical_message_id,
                                 objective="继续", provider="test", model="TEST_MODEL")
        journal.add_fork_edge(new.turn_id, old.turn_id, created_by_principal_id=None)
        replay = ReplayBundle(mode="verbatim", messages=({"role": "user", "content": "旧请求"},
            {"role": "assistant", "content": "旧结果"}), digest_prefix="", reason="valid",
            turn_ordinals=(old.turn_ordinal,), covered_canonical_message_ids=())
        inbound = replace(inbound, reply_to_native_message_id="TEST_QUOTE")
        self.app_context.settings = replace(self.app_context.settings, turn_replay_enabled=True)
        model = AsyncMock(return_value="继续完成")
        with patch.object(journal, "build_replay", return_value=replay) as build, patch.object(tool_executor, "ask_deepseek_with_tools", new=model):
            await self.handlers.tools._ask_inbound(inbound, platform, "继续", selected_profile_override=self.profile, journal_turn_id=new.turn_id)
        self.assertEqual(build.call_args.args[0], inbound.scope)
        self.assertEqual(model.call_args.kwargs["replay_prefix"], list(replay.messages))
        self.assertIn("可信引用内容", model.call_args.kwargs["group_context"])
        self.assertEqual(model.call_args.args[1], [])

    async def test_same_literal_id_different_accounts_have_separate_short_term_history(self):
        self.reject_legacy_helpers()
        histories = []

        async def model(text, history, tools, execute_tool, **kwargs):
            histories.append(history)
            return "回答：" + text

        with patch.object(tool_executor, "ask_deepseek_with_tools", new=model):
            for app_id, question in (("TEST_APP_A", "甲秘密"), ("TEST_APP_B", "乙问题"), ("TEST_APP_A", "甲追问")):
                inbound, platform = official(app_id=app_id, kind="private", target="TEST_SAME_PRIVATE_OPENID")
                await self.handlers.tools._ask_inbound(inbound, platform, question, selected_profile_override=self.profile)
        self.assertEqual(histories[0], [])
        self.assertEqual(histories[1], [])
        self.assertIn("甲秘密", json.dumps(histories[2], ensure_ascii=False))
        self.assertNotIn("乙问题", json.dumps(histories[2], ensure_ascii=False))

    async def test_disabled_endpoint_cannot_use_a_bound_enabled_history(self):
        self.reject_legacy_helpers()
        inbound, platform = official()
        other, _ = official(app_id="TEST_APP_B")
        inbound = replace(inbound, canonical_scope=other.scope)
        enabled = self.handlers.scope_enabled = Mock(return_value=False)
        with patch.object(tool_executor, "ask_deepseek_with_tools", new_callable=AsyncMock) as model:
            reply = await self.handlers.tools._ask_inbound(inbound, platform, "请求")
        self.assertIn("没有开启", reply)
        self.assertEqual(enabled.call_args.args[0], inbound.endpoint.scope)
        model.assert_not_awaited()

    async def test_bound_history_does_not_exclude_other_endpoint_same_native_id(self):
        self.setup_storage()
        self.reject_legacy_helpers()
        inbound, platform = official()
        other, _ = official(app_id="TEST_APP_B")
        other = replace(other, canonical_scope=inbound.scope)
        self.record(other, inbound.native_message_id, "另一端点同字面ID必须保留", occurred_at=1)
        self.record(inbound, inbound.native_message_id, "当前消息应排除", occurred_at=2)
        model = AsyncMock(return_value="已读取")
        with patch.object(tool_executor, "ask_deepseek_with_tools", new=model):
            await self.handlers.tools._ask_inbound(inbound, platform, "问题", selected_profile_override=self.profile)
        context = model.call_args.kwargs["group_context"]
        self.assertIn("另一端点同字面ID必须保留", context)
        self.assertNotIn("当前消息应排除", context)


class SharedCoreImportTests(unittest.TestCase):
    def test_shared_core_import_does_not_import_onebot(self):
        source = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'nonebot' or fullname.startswith('nonebot.'):
            raise AssertionError('shared core imported NoneBot')
sys.meta_path.insert(0, Block())
from src.plugins.ai_chat.application import shared_agent_turn, platform_turn_ports
assert callable(shared_agent_turn.run_shared_agent_turn)
"""
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_official_orchestrator_service_tool_history_journal_without_sdk(self):
        fixture = Path(__file__).parent / "fixtures" / "platform_chat_without_sdk.py"
        result = subprocess.run([sys.executable, str(fixture)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["model_calls"], 2)
        self.assertGreaterEqual(receipt["tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
