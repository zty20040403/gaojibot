"""Unexpected execution errors become safe replies; control signals stay intact."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

from src.plugins.ai_chat.application import ChatFailure
from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.model_budget import ModelBudgetExceeded
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests.handler_fixture import HandlerTestCaseMixin
from tests.test_shared_agent_turn import official


class PlatformChatFailureTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.app_context.model_catalog = ModelCatalog.from_json(json.dumps({
            "default": "fixture", "profiles": {"fixture": {"model": "TEST_MODEL", "api_key_required": False}},
        }), default_profile="fixture", environ={})
        self.app_context.logger = Mock()
        self.inbound, self.platform = official(kind="private", target="TEST_OPAQUE_C2C")

    def service(self, error):
        return PlatformChatService(self.app_context, scope_enabled=lambda _: True,
            llm=AsyncMock(side_effect=error))

    async def test_unexpected_errors_become_internal_failure_without_sensitive_text(self):
        secret = "TEST_SECRET https://credential:password@private.invalid/private-openid"
        for error_type in (ValueError, TypeError, OSError):
            with self.subTest(error=error_type.__name__):
                with self.assertRaises(ChatFailure) as caught:
                    await self.service(error_type(secret)).ask(self.inbound, self.platform, "question")
                failure = caught.exception
                self.assertEqual(failure.code, "internal")
                self.assertEqual(failure.reply, "我这边处理消息时出错了。")
                for visible in (str(failure), repr(failure), failure.reply,
                                str(self.app_context.logger.mock_calls)):
                    self.assertNotIn("TEST_SECRET", visible)
                    self.assertNotIn("private.invalid", visible)
                    self.assertNotIn("private-openid", visible)
                self.assertEqual(self.app_context.memory.get(conversation_key(self.inbound)), [])

    async def test_cancellation_is_not_converted_to_a_user_failure(self):
        cancellation = asyncio.CancelledError("test cancellation")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.service(cancellation).ask(self.inbound, self.platform, "question")
        self.assertIs(caught.exception, cancellation)
        self.assertEqual(self.app_context.memory.get(conversation_key(self.inbound)), [])

    async def test_budget_deferred_and_existing_chat_failure_keep_control_semantics(self):
        controls = (ModelBudgetExceeded("fixture budget"), JobDeferred("fixture deferred", 42),
                    ChatFailure("existing safe failure", code="fixture"))
        for error in controls:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as caught:
                    await self.service(error).ask(self.inbound, self.platform, "question")
                self.assertIs(caught.exception, error)
        self.assertEqual(controls[1].delay_seconds, 42)
