from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat import tool_policy
from src.plugins.ai_chat.tools import domain_registry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.web_search import SearchError, SearchResult


class SourceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.app = SimpleNamespace(
            settings=SimpleNamespace(
                max_input_chars=40, search_max_results=4, search_timeout_seconds=12
            ),
            logger=Mock(),
            skill_registry=Mock(),
            self_source=Mock(),
        )
        self.turn = TurnState(
            ToolContext(
                SimpleNamespace(context=self.app), Mock(), frozenset({"web", "self"})
            ),
            user_text="question from the current turn",
        )
        override = patch.dict(tool_policy._TOOL_ENABLED_OVERRIDES, {}, clear=True)
        override.start()
        self.addCleanup(override.stop)

    async def execute(self, tool_name: str, **arguments: object) -> dict:
        return json.loads(await domain_registry.execute(self.turn, tool_name, arguments))

    async def test_search_preserves_fallback_limits_freshness_and_turn_sources(
        self,
    ) -> None:
        result = SearchResult("title", "https://example.test/evidence", "snippet")
        with (
            patch(
                "src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock
            ) as search,
            patch(
                "src.plugins.ai_chat.tools.sources.search_freshness", return_value=None
            ) as freshness,
        ):
            search.return_value = [result]
            response = await self.execute("web_search", freshness="month")
            search.assert_awaited_once_with(
                self.turn.user_text, max_results=4, timeout_seconds=12, freshness="m"
            )
            self.assertEqual(response["results"][0]["url"], result.url)
            self.assertEqual(self.turn.search_results, [result])
            freshness.return_value = "d"
            response = await self.execute(
                "web_search", query="x" * 80, freshness="year"
            )
            self.assertEqual(search.call_args.args, ("x" * 40,))
            self.assertEqual(response["freshness"], "d")
            self.assertEqual(self.turn.search_results, [result])
            self.assertEqual(TurnState(self.turn.context).search_results, [])

    async def test_search_failure_is_reported_without_leaking_provider_error(
        self,
    ) -> None:
        with patch(
            "src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock
        ) as search:
            search.side_effect = SearchError("upstream details")
            response = await self.execute("web_search")
            self.assertFalse(response["ok"])
            self.assertNotIn("upstream", response["error"])
            self.assertEqual(self.turn.search_results, [])
            search.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await self.execute("web_search")

    async def test_disabled_tools_never_access_search_skills_or_source(self) -> None:
        self.turn.context = replace(self.turn.context, capabilities=frozenset())
        with patch(
            "src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock
        ) as search:
            for name in ("web_search", "use_skill", "inspect_source"):
                self.assertEqual((await self.execute(name))["code"], "tool_unavailable")
            search.assert_not_awaited()
        self.app.skill_registry.get.assert_not_called()
        self.app.self_source.identity.assert_not_called()

    async def test_skill_and_source_reads_keep_existing_payloads_and_error_contract(
        self,
    ) -> None:
        skill = SimpleNamespace(name="example", title="Example", body="instructions")
        self.app.skill_registry.get.return_value = skill
        response = await self.execute("use_skill", name=" example ")
        self.assertEqual(response["instructions"], skill.body)
        self.app.skill_registry.get.assert_called_once_with("example")
        self.app.skill_registry.get.return_value = None
        self.app.skill_registry.list.return_value = [skill]
        self.assertEqual((await self.execute("use_skill"))["available"], ["example"])
        self.app.self_source.paths.return_value = (["bot.py"], False)
        self.app.self_source.search.return_value = [
            SimpleNamespace(path="bot.py", line=1, text="import nonebot")
        ]
        self.app.self_source.read.return_value = "import nonebot"
        self.app.self_source.identity.return_value = {"revision": "abc"}
        for action in ("list", "search", "read", "identity"):
            response = await self.execute(
                "inspect_source", action=action, path="bot.py", start_line=2, end_line=8
            )
            self.assertTrue(response["ok"])
        self.app.self_source.read.assert_called_once_with(
            "bot.py", start_line=2, end_line=8
        )
        self.app.self_source.read.side_effect = ValueError("path outside snapshot")
        self.assertFalse(
            (await self.execute("inspect_source", action="read", path="../secret"))[
                "ok"
            ]
        )
        self.assertFalse((await self.execute("inspect_source", action="execute"))["ok"])
