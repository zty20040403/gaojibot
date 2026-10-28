"""A private reply uses visible ledger evidence at its actual source endpoint."""
from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.model_catalog import ModelCatalog
from tests.handler_fixture import HandlerTestCaseMixin
from tests.postgres_fixture import postgres_for_test
from tests.test_shared_agent_turn import official


class PlatformPrivateReplyContextTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.app_context.model_catalog = ModelCatalog.from_json(json.dumps({
            "default": "fixture", "profiles": {"fixture": {"model": "TEST_MODEL", "api_key_required": False}},
        }), default_profile="fixture", environ={})
        self.ledger = self.app_context.message_ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.inbound, self.platform = official(kind="private", target="TEST_NONNUMERIC_C2C")
        self.inbound = replace(self.inbound, reply_to_native_message_id="TEST_QUOTE")

    def record(self, inbound, text):
        return self.ledger.record_message(inbound.scope, native_message_id="TEST_QUOTE",
            sender_native_user_id=inbound.actor.native_user_id, sender_display=inbound.sender_display,
            body=MessageBody((TextNode(0, text),)), actor=inbound.actor, source_endpoint=inbound.endpoint)

    async def prompt(self, inbound=None):
        model = AsyncMock(return_value="fixture answer")
        service = PlatformChatService(self.app_context, scope_enabled=lambda _: True, llm=model)
        await service.ask(inbound or self.inbound, self.platform, "解释引用内容")
        return model.await_args.kwargs["group_context"]

    async def test_private_explicit_quote_reaches_real_shared_prompt_without_group_policy(self):
        target = self.record(self.inbound, "PRIVATE_QUOTE_EVIDENCE " + "x" * 10000)
        context = await self.prompt()
        self.assertIn("PRIVATE_QUOTE_EVIDENCE", context)
        self.assertIn(f"msg#{target.canonical_message_id}", context)
        self.assertLess(len(context), 4300)
        self.assertNotIn("x" * 5000, context)

    async def test_bound_history_same_literal_id_cannot_quote_other_endpoint(self):
        scope = ConversationScope("onebot-v11", "private", "123")
        own = replace(self.inbound, canonical_scope=scope)
        foreign, _ = official(app_id="TEST_OTHER_APP", kind="private", target="TEST_NONNUMERIC_C2C")
        foreign = replace(foreign, canonical_scope=scope)
        self.record(foreign, "FOREIGN_QUOTE_MUST_NOT_APPEAR")
        self.assertEqual(await self.prompt(own), "")
        self.record(own, "OWN_QUOTE_EVIDENCE")
        context = await self.prompt(own)
        self.assertIn("OWN_QUOTE_EVIDENCE", context)
        self.assertNotIn("FOREIGN_QUOTE_MUST_NOT_APPEAR", context)

    async def test_missing_hidden_and_absent_reply_have_no_invented_quote(self):
        self.assertEqual(await self.prompt(), "")
        self.record(self.inbound, "HIDDEN_EVIDENCE")
        self.ledger.hide_history(self.inbound.scope)
        self.assertEqual(await self.prompt(), "")
        self.assertEqual(await self.prompt(replace(self.inbound, reply_to_native_message_id=None)), "")
