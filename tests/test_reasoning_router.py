"""Chit-chat thinks at medium; questions and any failure keep the profile's effort."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.plugins.ai_chat import reasoning_router


class ReasoningRouterTests(unittest.IsolatedAsyncioTestCase):
    def catalog(self):
        return SimpleNamespace(resolve=lambda name: SimpleNamespace(name=name))

    async def route(self, answer=None, error=None, text="TEST"):
        mock = AsyncMock(return_value=answer, side_effect=error)
        with patch.object(reasoning_router, "ask_deepseek_json", mock):
            return await reasoning_router.chat_effort(self.catalog(), "TEST_ROUTER", text), mock

    async def test_chat_gets_medium_and_questions_keep_the_profile_effort(self):
        self.assertEqual((await self.route({"kind": "chat"}))[0], "medium")
        self.assertIsNone((await self.route({"kind": "question"}))[0])
        self.assertIsNone((await self.route({"kind": "something else"}))[0])

    async def test_failures_and_missing_router_keep_the_profile_effort(self):
        self.assertIsNone((await self.route(error=RuntimeError("TEST")))[0])
        effort, mock = await self.route({"kind": "chat"}, text="   ")
        self.assertIsNone(effort); mock.assert_not_awaited()
        self.assertIsNone(await reasoning_router.chat_effort(self.catalog(), "", "TEST"))
