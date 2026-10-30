"""A native event or history alias cannot bypass actual platform tool gates."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import MessageSegment

from src.plugins.ai_chat.ai_tools import QUERY_ALERTS_TOOL
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import (
    AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures,
)
from src.plugins.ai_chat.self_source import SelfSource
from src.plugins.ai_chat.skills import SkillRegistry
from src.plugins.ai_chat.tools import domain_registry, registry_for
from src.plugins.ai_chat.tools.onebot_checkpoint import OneBotReplyCheckpointCodec
from src.plugins.ai_chat.tools.registry import FunctionTool, ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat import tool_policy


def inbound(*, platform="qq-official", app="TEST_APP", environment="production"):
    account = AccountKey(platform, app, environment)
    endpoint = EndpointKey(account, "group", "TEST_GROUP")
    actor = ActorKey(account, "group_member" if platform == "qq-official" else "user",
                     "TEST_GROUP" if platform == "qq-official" else "", "TEST_USER")
    return InboundMessage(
        endpoint=endpoint, actor=actor, event_id="TEST_EVENT", native_message_id="TEST_MESSAGE",
        sender_display="Test", platform_role="member", body=MessageBody((TextNode(0, "test"),)),
        mentions_bot=True, reply_to_native_message_id=None, occurred_at=1000, reply_credential=None,
    )


async def db_call(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class PoisonEvent:
    def __getattribute__(self, name):
        raise AssertionError("a structured tool inspected the legacy event")


class PlatformToolGatesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SimpleNamespace(db_call=db_call)
        self.services = SimpleNamespace(context=self.app)
        self.message = inbound()
        self.platform = SimpleNamespace(account=self.message.endpoint.account, features=PlatformFeatures())
        self.context = ToolContext(self.services, inbound=self.message, platform=self.platform)
        self.run = AsyncMock(return_value='{"ok": true}')
        self.available = Mock(return_value=True)
        overrides = patch.dict(tool_policy._TOOL_ENABLED_OVERRIDES, {}, clear=True)
        overrides.start()
        self.addCleanup(overrides.stop)

    def handler(self, **kwargs):
        return FunctionTool(QUERY_ALERTS_TOOL, self.available, self.run, **kwargs)

    async def assert_unavailable(self, registry, context):
        self.assertEqual(registry.definitions(context), [])
        result = json.loads(await registry.execute(TurnState(context), "query_alerts", {}))
        self.assertEqual(result["code"], "tool_unavailable")

    async def test_unmigrated_function_is_hidden_and_blocked_before_legacy_available(self):
        registry = ToolRegistry([self.handler()])
        await self.assert_unavailable(registry, replace(self.context, event=PoisonEvent()))
        self.available.assert_not_called()
        self.run.assert_not_awaited()

    async def test_custom_handler_without_declaration_also_fails_closed(self):
        custom = SimpleNamespace(definition=QUERY_ALERTS_TOOL, enabled=self.available,
                                 definition_for=Mock(return_value=QUERY_ALERTS_TOOL), run=self.run)
        await self.assert_unavailable(ToolRegistry([custom]), self.context)
        self.available.assert_not_called()
        custom.definition_for.assert_not_called()
        self.run.assert_not_awaited()

    async def test_all_feature_gates_apply_to_definition_and_execution(self):
        for feature in ("group_files", "reactions", "forward_messages", "member_list",
                        "native_ocr", "inline_voice_text", "markdown", "native_files", "proactive_group"):
            with self.subTest(feature=feature):
                self.available.reset_mock()
                self.run.reset_mock()
                registry = ToolRegistry([self.handler(platform_neutral=True,
                                                      required_features=frozenset({feature}))])
                await self.assert_unavailable(registry, self.context)
                self.available.assert_not_called()
                self.run.assert_not_awaited()
                self.platform.features = PlatformFeatures(**{feature: True})
                self.assertEqual(registry.definitions(self.context), [QUERY_ALERTS_TOOL])
                self.assertTrue(json.loads(await registry.execute(TurnState(self.context), "query_alerts", {}))["ok"])
                # Capability may disappear after advertisement; invocation rechecks.
                self.platform.features = PlatformFeatures()
                await self.assert_unavailable(registry, self.context)
                self.assertEqual(self.run.await_count, 1)

    async def test_custom_handler_cannot_skip_declared_feature_checks(self):
        custom = SimpleNamespace(definition=QUERY_ALERTS_TOOL, enabled=self.available,
                                 definition_for=Mock(return_value=QUERY_ALERTS_TOOL), run=self.run,
                                 platform_neutral=True, required_features=frozenset({"member_list"}))
        await self.assert_unavailable(ToolRegistry([custom]), self.context)
        self.available.assert_not_called()

    async def test_neutral_declaration_does_not_bypass_host_capability_or_disable(self):
        registry = ToolRegistry([self.handler(platform_neutral=True)])
        self.available.return_value = False
        await self.assert_unavailable(registry, self.context)
        self.available.return_value = True
        tool_policy.set_tool_enabled("query_alerts", False)
        await self.assert_unavailable(registry, self.context)
        self.run.assert_not_awaited()

    async def test_partial_structured_context_never_falls_back_to_legacy(self):
        registry = ToolRegistry([self.handler(platform_neutral=True)])
        for context in (replace(self.context, platform=None, event=PoisonEvent()),
                        replace(self.context, inbound=None, event=PoisonEvent())):
            await self.assert_unavailable(registry, context)
        self.available.assert_not_called()
        self.run.assert_not_awaited()

    async def test_no_event_or_platform_cannot_supply_native_features(self):
        registry = ToolRegistry([self.handler(platform_neutral=True,
                                              required_features=frozenset({"reactions"}))])
        await self.assert_unavailable(registry, ToolContext(self.services))
        self.available.assert_not_called()

    async def test_legacy_callers_without_platform_retain_original_availability(self):
        legacy = ToolContext(self.services, SimpleNamespace(user_id=7), frozenset({"alerts"}))
        handler = self.handler(required_features=frozenset({"group_files"}))
        registry = ToolRegistry([handler])
        self.assertEqual(registry.definitions(legacy), [QUERY_ALERTS_TOOL])
        self.assertTrue(json.loads(await registry.execute(TurnState(legacy), "query_alerts", {}))["ok"])
        self.assertIsNone(legacy.scope)

    async def test_structured_onebot_uses_features_and_requires_event_for_legacy_tools(self):
        message = inbound(platform="onebot-v11")
        platform = SimpleNamespace(account=message.endpoint.account, features=PlatformFeatures())
        context = ToolContext(self.services, object(), inbound=message, platform=platform)
        registry = ToolRegistry([self.handler(required_features=frozenset({"member_list"}))])
        await self.assert_unavailable(registry, context)
        platform.features = PlatformFeatures(member_list=True)
        self.assertEqual(registry.definitions(context), [QUERY_ALERTS_TOOL])
        await self.assert_unavailable(registry, replace(context, event=None))

    def test_account_validation_includes_app_and_environment(self):
        for other in (inbound(app="TEST_OTHER_APP"), inbound(environment="sandbox")):
            with self.assertRaisesRegex(ValueError, "account"):
                replace(self.context, inbound=other)

    async def test_history_binding_cannot_enable_legacy_tools(self):
        historical = ConversationScope("onebot-v11", "group", "123")
        message = replace(self.message, canonical_scope=historical)
        context = replace(self.context, inbound=message, event=PoisonEvent())
        self.assertEqual(context.scope, historical)
        await self.assert_unavailable(ToolRegistry([self.handler()]), context)
        self.available.assert_not_called()

    def test_unknown_or_non_boolean_feature_metadata_rejected(self):
        with self.assertRaises(ValueError):
            self.handler(required_features=frozenset({"member_lsit"}))
        with self.assertRaises(TypeError):
            self.handler(platform_neutral="yes")

    async def test_real_catalog_allows_scoped_neutral_tools_without_legacy_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills").mkdir()
            (root / "skills" / "test.md").write_text("# Test skill\nSummary: fixture\nSafe instructions.")
            (root / "README.md").write_text("Test public source snapshot")
            self.app.skill_registry = SkillRegistry(root / "skills")
            self.app.self_source = SelfSource(root)
            self.app.settings = SimpleNamespace(max_input_chars=1000, search_max_results=3, search_timeout_seconds=1,
                                                search_provider="scrape", search_api_key="")
            # These broad capability names cannot revive unmigrated tools.
            context = replace(self.context, event=PoisonEvent(), capabilities=frozenset({
                "self", "web", "conversation", "shared_content", "members", "ocr",
                "transcription", "projects", "fleet", "mcp",
            }))
            registry = registry_for(context)
            self.assertEqual({tool["function"]["name"] for tool in registry.definitions(context)},
                             {"use_skill", "inspect_source", "web_search", "web_fetch"})
            turn = TurnState(context, user_text="public query")
            skill = json.loads(await registry.execute(turn, "use_skill", {"name": "test"}))
            self.assertIn("Safe instructions", skill["instructions"])
            source = json.loads(await registry.execute(turn, "inspect_source", {"action": "read", "path": "README.md"}))
            self.assertIn("public source snapshot", source["slice"]["content"])
            forbidden = json.loads(await registry.execute(turn, "inspect_source", {"action": "read", "path": "../secret"}))
            self.assertFalse(forbidden["ok"])
            with patch("src.plugins.ai_chat.tools.sources.search_web", new=AsyncMock(return_value=[])) as search:
                result = json.loads(await registry.execute(turn, "web_search", {}))
                self.assertTrue(result["ok"])
                self.assertEqual(search.await_args.args, ("public query",))
            for name in ("memory_add", "context_search", "group_members", "inspect_shared_content",
                         "reminder_set", "ops_call", "view_forward", "send_file_from_sandbox"):
                result = json.loads(await registry.execute(turn, name, {}))
                self.assertEqual(result["code"], "tool_unavailable")

    def test_real_native_registrations_declare_required_features(self):
        expected = {"group_members": "member_list", "read_image_text": "native_ocr",
                    "transcribe_voice": "inline_voice_text", "view_forward": "forward_messages",
                    "list_recent_files": "group_files", "import_file_to_sandbox": "group_files",
                    "send_file_from_sandbox": "native_files"}
        handlers = {handler.definition["function"]["name"]: handler for handler in domain_registry.handlers}
        for name, feature in expected.items():
            self.assertIn(feature, handlers[name].required_features)


class PlatformReplyCheckpointTests(unittest.TestCase):
    def test_explicit_onebot_codec_preserves_checkpoint_and_native_segments(self):
        context = ToolContext(SimpleNamespace(context=None), object())
        original = TurnState(context, reply_checkpoint_codec=OneBotReplyCheckpointCodec())
        original.visual_reply_segment = MessageSegment.image("base64://aGVsbG8=")
        original.voice_reply_segment = MessageSegment.record("voice-file")
        original.voice_reply_text = "prepared"
        original.subagent_task_started = True
        original.subagent_delegations = 2
        payload = json.loads(json.dumps(original.reply_checkpoint()))
        self.assertEqual(payload, {
            "visual": {"type": "image", "data": original.visual_reply_segment.data},
            "voice": {"type": "record", "data": original.voice_reply_segment.data},
            "voice_text": "prepared", "task_started": True, "delegations": 2,
        })
        restored = TurnState(context, reply_checkpoint_codec=OneBotReplyCheckpointCodec())
        restored.restore_reply_checkpoint(payload)
        self.assertEqual(restored.visual_reply_segment, original.visual_reply_segment)
        self.assertEqual(restored.voice_reply_segment, original.voice_reply_segment)
        self.assertEqual(restored.reply_checkpoint(), original.reply_checkpoint())

    def test_text_checkpoint_needs_no_codec(self):
        turn = TurnState(ToolContext(SimpleNamespace(context=None)))
        turn.voice_reply_text = "plain text"
        restored = TurnState(turn.context)
        restored.restore_reply_checkpoint(turn.reply_checkpoint())
        self.assertEqual(restored.voice_reply_text, "plain text")

    def test_native_checkpoint_never_implicitly_selects_legacy_codec(self):
        context = ToolContext(SimpleNamespace(context=None), object())
        turn = TurnState(context, visual_reply_segment=MessageSegment.image("test"))
        with self.assertRaisesRegex(ValueError, "explicit"):
            turn.reply_checkpoint()
        restored = TurnState(context)
        with self.assertRaisesRegex(ValueError, "explicit"):
            restored.restore_reply_checkpoint({"visual": {"type": "image", "data": {"file": "test"}}})
        self.assertIsNone(restored.visual_reply_segment)

    def test_official_actual_platform_rejects_onebot_codec_even_with_legacy_history(self):
        message = replace(inbound(), canonical_scope=ConversationScope("onebot-v11", "group", "1"))
        context = ToolContext(SimpleNamespace(context=None), inbound=message)
        turn = TurnState(context, reply_checkpoint_codec=OneBotReplyCheckpointCodec())
        with self.assertRaisesRegex(ValueError, "actual platform"):
            turn.restore_reply_checkpoint({"visual": {"type": "image", "data": {"file": "test"}}})
        self.assertIsNone(turn.visual_reply_segment)

    def test_public_state_and_registry_import_with_onebot_blocked(self):
        script = """
import importlib.abc
import sys
class BlockOneBot(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('nonebot.adapters.onebot'):
            raise AssertionError('public tool ports imported OneBot: ' + fullname)
sys.meta_path.insert(0, BlockOneBot())
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.tools.registry import ToolRegistry
assert not any(name.startswith('nonebot.adapters.onebot') for name in sys.modules)
assert not any(name.startswith('src.plugins.ai_chat.onebot_runtime') for name in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
