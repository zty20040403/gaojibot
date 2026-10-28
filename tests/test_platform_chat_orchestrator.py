from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.plugins.ai_chat.application.chat_orchestrator import ChatOrchestrator, ChatPorts
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import AccountKey, PlatformFeatures
from src.plugins.ai_chat.tasks import RunningTaskRegistry
from tests.test_platform_types import inbound


class PlatformChatOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = inbound()
        self.platform = SimpleNamespace(account=self.message.endpoint.account, features=PlatformFeatures())
        self.tasks = RunningTaskRegistry()
        self.profile = SimpleNamespace(provider_identity="TEST_PROVIDER", model="TEST_MODEL", name="TEST_PROFILE")
        self.agent = AsyncMock(return_value="测试回复")
        self.preferences = Mock(get_explicit=Mock(return_value=None))
        self.catalog = Mock(resolve_preference=Mock(return_value=self.profile))
        self.ledger = Mock(canonical_id_for_native=Mock(return_value=17))
        self.journal = Mock(start_turn=Mock(return_value=SimpleNamespace(turn_id=23)))
        self.ports = ChatPorts(
            conversation_id=conversation_key,
            endpoint_enabled=Mock(return_value=True),
            group_default_profile=Mock(return_value=None),
            reply_target_turn=Mock(return_value=None),
            record_trigger=Mock(return_value=17),
            current_turn_context=Mock(return_value="已核实的上下文"),
            drain_feedback=AsyncMock(return_value=[]),
            ask_agent=self.agent,
            is_silence_reply=lambda reply: False,
            journal_reply_text=str,
        )
        self.orchestrator = ChatOrchestrator(
            ports=self.ports, running_tasks=self.tasks, model_preferences=self.preferences,
            model_catalog=self.catalog, message_ledger=self.ledger, turn_journal=self.journal,
            logger=Mock(), prompt_version="TEST_PROMPT",
        )

    async def test_nonnumeric_turn_passes_structured_identity_to_the_agent(self):
        async def ask(message, platform, user_text, **kwargs):
            self.assertIs(message, self.message)
            self.assertIs(platform, self.platform)
            self.assertEqual(user_text, "问题")
            active = self.tasks.list_for_endpoint(message.endpoint, message.actor)
            self.assertEqual(len(active), 1)
            self.assertIsNone(active[0].user_id)
            self.assertEqual(active[0].native_message_id, "TEST_MESSAGE")
            self.assertEqual(self.tasks.list_for(active[0].conversation_id), [])
            self.assertNotIn("event", kwargs)
            return "测试回复"
        self.agent.side_effect = ask
        result = await self.orchestrator.run(self.message, self.platform, "问题")
        self.assertEqual(result.reply, "测试回复")
        self.assertEqual(result.turn_id, 23)
        self.ledger.canonical_id_for_native.assert_called_once_with(
            self.message.scope, "TEST_MESSAGE", endpoint=self.message.endpoint,
        )
        self.assertEqual(self.tasks.list_all(), [])

    async def test_history_route_does_not_change_actual_endpoint_or_actor(self):
        canonical = ConversationScope("onebot-v11", "group", "900001")
        message = replace(self.message, canonical_scope=canonical)
        await self.orchestrator.run(message, self.platform, "问题")
        self.ports.endpoint_enabled.assert_called_once_with(message.endpoint)
        self.ledger.canonical_id_for_native.assert_called_once_with(
            canonical, "TEST_MESSAGE", endpoint=message.endpoint,
        )
        self.assertEqual(self.agent.await_args.args[:2], (message, self.platform))

    async def test_wrong_account_rejected_before_any_storage_or_model_access(self):
        wrong = SimpleNamespace(account=AccountKey("qq-official", "TEST_OTHER_APP", "test"))
        with self.assertRaises(ValueError):
            await self.orchestrator.run(self.message, wrong, "问题")
        self.preferences.get_explicit.assert_not_called()
        self.ledger.canonical_id_for_native.assert_not_called()
        self.agent.assert_not_awaited()
        self.assertEqual(self.tasks.list_all(), [])

    async def test_exception_cleans_registration_and_finishes_journal(self):
        self.agent.side_effect = RuntimeError("TEST_FAILURE")
        with self.assertRaisesRegex(RuntimeError, "TEST_FAILURE"):
            await self.orchestrator.run(self.message, self.platform, "问题")
        self.assertEqual(self.tasks.list_all(), [])
        self.assertEqual(self.journal.finish_turn.call_args.kwargs["status"], "crashed")
