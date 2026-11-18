from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bot_storage import DatabaseCalls


from src.plugins.ai_chat import tool_policy
from src.plugins.ai_chat.tools import _base_registry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.web_fetch import FetchError, FetchedPage
from src.plugins.ai_chat.web_search import SearchError, SearchResult


class SourceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.app = SimpleNamespace(
            settings=SimpleNamespace(
                max_input_chars=40, search_max_results=4, search_timeout_seconds=12,
                search_api_key="TEST_KEY",
            ),
            logger=Mock(),
            skill_registry=Mock(),
            self_source=Mock(),
        )
        self.app.db_call = DatabaseCalls()
        self.addAsyncCleanup(self.app.db_call.close)
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
        return json.loads(await _base_registry().execute(self.turn, tool_name, arguments))

    async def test_search_bounds_queries_and_results_preserves_metadata_and_ids(self) -> None:
        result = SearchResult("title", "https://example.test/evidence?utm_source=TEST", "s" * 900,
                              date="2026-11-19", provider="TEST_PROVIDER")
        with patch("src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock, return_value=[result]) as search:
            response = await self.execute("web_search", queries=[" x ", "y" * 80], max_results=9)
            search.assert_awaited_once_with(["x", "y" * 40], max_results=4, timeout_seconds=12, api_key="TEST_KEY", all_results=[])
            hit = response["results"][0]
            self.assertEqual(response["queries"], ["x", "y" * 40])
            self.assertEqual((hit["id"], hit["url"]), (1, "https://example.test/evidence"))
            self.assertEqual((hit["date"], hit["provider"], len(hit["snippet"])), ("2026-11-19", "TEST_PROVIDER", 800))
            self.assertEqual(self.turn.search_results[0].snippet, hit["snippet"])
            await self.execute("web_search", queries=["z", "a", "b"], max_results=2.0)
            self.assertEqual(search.await_args.args, (["z", "a", "b"],))
            self.assertEqual(search.await_args.kwargs["max_results"], 2)
            self.assertEqual(len(self.turn.search_results), 1)
            self.assertEqual(TurnState(self.turn.context).search_results, [])

    async def test_search_rejects_invalid_contract_without_calling_provider(self) -> None:
        invalid = [
            {}, {"query": "TEST"}, {"queries": []}, {"queries": [" "]},
            {"queries": ["TEST", 1]}, {"queries": ["x"] * 4}, {"queries": ["x" * 1501]},
            {"queries": ["x"], "freshness": "day"}, {"queries": ["x"], "max_results": 0},
            {"queries": ["x"], "max_results": 11}, {"queries": ["x"], "max_results": True},
        ]
        with patch("src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock) as search:
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    self.assertFalse((await self.execute("web_search", **arguments))["ok"])
            search.assert_not_awaited()

    async def test_empty_search_is_not_retried(self) -> None:
        with patch("src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock, return_value=[]) as search:
            response = await self.execute("web_search", queries=["显卡"])
            self.assertTrue(response["ok"])
            self.assertEqual(response["results"], [])
            search.assert_awaited_once()

    async def test_fetch_pages_text_and_keeps_citation_ids_stable(self) -> None:
        self.turn.search_results.append(SearchResult("hit", "https://example.test/hit", ""))
        page = FetchedPage("https://shop.example/item?utm_source=ad", "RTX 5070", "价" * 9000)
        with patch(
            "src.plugins.ai_chat.tools.sources.fetch_page", new_callable=AsyncMock
        ) as fetch:
            fetch.return_value = page
            first = await self.execute("web_fetch", url="https://shop.example/item?utm_source=ad")
            self.assertEqual(first["id"], 2)
            self.assertEqual(first["url"], "https://shop.example/item")
            self.assertEqual(first["total_chars"], 9000)
            self.assertEqual(len(first["text"]), 8000)
            self.assertEqual(first["next_offset"], 8000)
            rest = await self.execute("web_fetch", url="https://shop.example/item", offset=8000)
            self.assertEqual(rest["id"], 2)
            self.assertEqual(len(rest["text"]), 1000)
            self.assertIsNone(rest["next_offset"])
            self.assertEqual([item.url for item in self.turn.search_results],
                             ["https://example.test/hit", "https://shop.example/item"])
            fetch.side_effect = FetchError("只能读取公网网页。")
            refused = await self.execute("web_fetch", url="http://10.0.0.1/")
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["error"], "只能读取公网网页。")

    async def test_search_then_fetch_preserves_stable_ids_and_cleans_urls(self) -> None:
        raw = [
            SearchResult("A", "https://www.bilibili.com/video/BV1TEST?vd_source=x&p=3", "s1", "2026-01-01", "TEST_EXA"),
            SearchResult("B", "https://shop.example/p?id=1&utm_source=feed", "s2", "", "TEST_BOCHA"),
        ]
        with patch("src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock, return_value=raw), \
                patch("src.plugins.ai_chat.tools.sources.fetch_page", new_callable=AsyncMock,
                      return_value=FetchedPage("https://other.example/c", "C", "body")):
            search_resp = await self.execute("web_search", queries=["TEST"])
            self.assertEqual([r["id"] for r in search_resp["results"]], [1, 2])
            self.assertEqual([(r["date"], r["provider"]) for r in search_resp["results"]],
                             [("2026-01-01", "TEST_EXA"), ("", "TEST_BOCHA")])
            self.assertEqual(search_resp["results"][0]["url"], "https://www.bilibili.com/video/BV1TEST?p=3")
            self.assertEqual(search_resp["results"][1]["url"], "https://shop.example/p?id=1")
            fetch_resp = await self.execute("web_fetch", url="https://other.example/c")
            self.assertEqual(fetch_resp["id"], 3)
            self.assertEqual(
                [item.url for item in self.turn.search_results],
                ["https://www.bilibili.com/video/BV1TEST?p=3", "https://shop.example/p?id=1", "https://other.example/c"],
            )

    async def test_search_and_fetched_results_strip_tracking_only_after_fetch(self) -> None:
        raw = 'https://www.bilibili.com./video/TEST?buvid=TEST&p=2'
        clean = 'https://www.bilibili.com./video/TEST?p=2'
        with patch('src.plugins.ai_chat.tools.sources.search_web', new_callable=AsyncMock,
                   return_value=[SearchResult('TEST', raw, 'snippet'), SearchResult('TEST_DUP', clean, '')]), \
             patch('src.plugins.ai_chat.tools.sources.fetch_page', new_callable=AsyncMock,
                   return_value=FetchedPage(raw, 'TEST', 'text')) as fetch:
            search = await self.execute('web_search', queries=['TEST'])
            self.assertEqual([item['url'] for item in search['results']], [clean, clean])
            self.assertEqual([item.url for item in self.turn.search_results], [clean])
            page = await self.execute('web_fetch', url=raw)
            fetch.assert_awaited_once_with(raw, timeout_seconds=15)
            self.assertEqual(page['url'], clean)
            self.assertEqual([item.url for item in self.turn.search_results], [clean])

    async def test_search_failure_is_reported_without_leaking_provider_error(
        self,
    ) -> None:
        with patch(
            "src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock
        ) as search:
            search.side_effect = SearchError("TEST_KEY upstream details")
            response = await self.execute("web_search", queries=["TEST"])
            self.assertFalse(response["ok"])
            self.assertNotIn("upstream", response["error"])
            self.assertNotIn("TEST_KEY", str(self.app.logger.warning.call_args))
            self.assertEqual(self.turn.search_results, [])
            search.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await self.execute("web_search", queries=["TEST"])

    async def test_disabled_tools_never_access_search_skills_or_source(self) -> None:
        self.turn.context = replace(self.turn.context, capabilities=frozenset())
        with patch(
            "src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock
        ) as search:
            for name in ("web_search", "web_fetch", "use_skill", "inspect_source"):
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


class WebFetchFallbackTests(unittest.IsolatedAsyncioTestCase):
    """403/412/429: retry once with browser headers, then fall back to the search snippet."""

    async def asyncSetUp(self):
        from types import SimpleNamespace
        from src.plugins.ai_chat.tools.state import TurnState
        self.turn = SimpleNamespace(
            search_results=[SearchResult("TEST title", "https://blocked.example/p", "TEST snippet")],
            search_read=[],
            context=SimpleNamespace(app=SimpleNamespace(settings=SimpleNamespace(search_timeout_seconds=1),
                                                        logger=SimpleNamespace(warning=lambda *_: None))))
        self.assertTrue(TurnState)

    async def call(self, side_effect):
        from src.plugins.ai_chat.tools.sources import web_fetch
        with patch("src.plugins.ai_chat.tools.sources.fetch_page", new_callable=AsyncMock,
                   side_effect=side_effect) as fetch:
            result = json.loads(await web_fetch(self.turn, {"url": "https://blocked.example/p"}))
        return result, fetch

    async def test_browser_headers_retry_succeeds(self):
        from src.plugins.ai_chat.web_fetch import BROWSER_HEADERS, FetchError
        result, fetch = await self.call([FetchError("HTTP 403", status=403),
                                         FetchedPage("https://blocked.example/p", "TEST", "body")])
        self.assertTrue(result["ok"]); self.assertNotIn("partial", result)
        self.assertEqual(fetch.await_args_list[1].kwargs["headers"], BROWSER_HEADERS)

    async def test_still_blocked_returns_marked_snippet(self):
        from src.plugins.ai_chat.web_fetch import FetchError
        result, fetch = await self.call([FetchError("HTTP 412", status=412), FetchError("HTTP 403", status=403)])
        self.assertEqual((result["ok"], result["partial"], result["id"], result["text"]), (True, True, 1, "TEST snippet"))
        self.assertIn("搜索摘要", result["note"])

    async def test_other_failures_are_not_retried(self):
        from src.plugins.ai_chat.web_fetch import FetchError
        result, fetch = await self.call([FetchError("HTTP 500", status=500)])
        self.assertFalse(result["ok"]); self.assertEqual(fetch.await_count, 1)
