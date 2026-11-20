from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import Mock, patch

import httpx

from src.plugins.ai_chat import web_search as web_search_module
from src.plugins.ai_chat.web_fetch import FetchError, fetch_page, public_address
from src.plugins.ai_chat.web_search import SearchError, SearchResult, render_search_sources, search_web


ADDRESSES = {"shop.example": "93.184.216.34", "internal.example": "100.100.10.10"}


async def resolver(host: str, port: int) -> str:
    return await public_address(ADDRESSES.get(host, host), port)


class WebFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_address_refuses_private_tailscale_and_mapped_addresses(self) -> None:
        self.assertEqual(await public_address("8.8.8.8", 443), "8.8.8.8")
        for address in ("127.0.0.1", "10.1.2.3", "100.100.10.10", "169.254.169.254", "::1", "::ffff:10.0.0.1"):
            with self.assertRaises(FetchError):
                await public_address(address, 80)

    async def test_connects_to_checked_address_and_extracts_gbk_text(self) -> None:
        seen = []
        html = (
            '<html><head><meta charset="gbk"><title>显卡 报价</title>'
            "<style>.x{}</style><script>var price=1</script></head>"
            "<body><h1>RTX 5070</h1><p>到手价 4599 元</p><p>到手价 4599 元</p></body></html>"
        ).encode("gbk")

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

        page = await fetch_page("https://shop.example/item?id=1", resolver=resolver,
                                transport=httpx.MockTransport(handler))
        self.assertEqual(seen[0].url.host, "93.184.216.34")
        self.assertEqual(seen[0].headers["host"], "shop.example")
        self.assertEqual(seen[0].extensions["sni_hostname"], "shop.example")
        self.assertEqual(page.url, "https://shop.example/item?id=1")
        self.assertEqual(page.title, "显卡 报价")
        self.assertEqual(page.text, "RTX 5070\n到手价 4599 元")

    async def test_continuously_active_body_hits_total_deadline_and_closes(self):
        closed = asyncio.Event()

        class Trickle(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b'.'
                    await asyncio.sleep(0.002)

            async def aclose(self):
                closed.set()

        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Trickle()))
        with self.assertRaises(TimeoutError):
            await fetch_page('https://shop.example/', timeout_seconds=.03, resolver=resolver, transport=transport)
        self.assertTrue(closed.is_set())

    async def test_redirect_into_private_network_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})

        with self.assertRaisesRegex(FetchError, "公网"):
            await fetch_page("https://shop.example/", resolver=resolver,
                             transport=httpx.MockTransport(handler))

    async def test_binary_files_and_bad_schemes_are_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

        with self.assertRaisesRegex(FetchError, "application/pdf"):
            await fetch_page("https://shop.example/a.pdf", resolver=resolver,
                             transport=httpx.MockTransport(handler))
        with self.assertRaises(FetchError):
            await fetch_page("file:///etc/passwd", resolver=resolver)


def exa_response(request: httpx.Request, text: str) -> httpx.Response:
    body = json.loads(request.content)
    if body["method"] == "notifications/initialized":
        return httpx.Response(202)
    result = ({"protocolVersion": "2025-03-26"} if body["method"] == "initialize" else
              {"content": [{"type": "text", "text": text}]})
    data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
    return httpx.Response(200, headers={"Content-Type": "text/event-stream", "Mcp-Session-Id": "TEST_SESSION"},
                          text=f"event: message\ndata: {data}\n\n")


def block(name: str, snippet: str = "snippet") -> str:
    return f"Title: {name}\nURL: https://example.test/{name}\nPublished: 2026-11-19\nHighlights: {snippet}"


class WebSearchProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.multiple(web_search_module, _QUERY_SLOTS=asyncio.Semaphore(4), _bocha_exhausted=False))

    async def test_mcp_handshake_and_header_parsing(self) -> None:
        requests = []
        def handler(request):
            requests.append(request)
            text = block("first", "x" * 900) + "\n\n---\n\n" + block("second", "Title: nested\nURL: https://nested.test\n---")
            return exa_response(request, text.replace("\n", "\r\n"))
        query = '“TEST_query”「C++」 OR (-SITE:example.test)，ORACLE OR-Tools or don’t?'
        results = await search_web([query], max_results=2, transport=httpx.MockTransport(handler))
        self.assertEqual([json.loads(r.content)["method"] for r in requests], ["initialize", "notifications/initialized", "tools/call"])
        self.assertEqual(requests[0].headers["accept"], "application/json, text/event-stream")
        self.assertEqual(json.loads(requests[0].content)["params"]["protocolVersion"], "2025-03-26")
        self.assertEqual(requests[2].headers["mcp-session-id"], "TEST_SESSION")
        self.assertEqual(json.loads(requests[2].content)["params"], {"name": "web_search_exa", "arguments": {
            "query": "TEST_query C++ ，ORACLE OR-Tools or don’t?",
            "objective": "TEST_query C++ ，ORACLE OR-Tools or don’t?", "numResults": 2}})
        self.assertEqual(results[0], SearchResult("first", "https://example.test/first", "x" * 800, "2026-11-19", "exa"))
        self.assertEqual((results[1].title, results[1].url), ("second", "https://example.test/second"))
        self.assertIn("Title: nested", results[1].snippet)

    async def test_fallback_balance_and_safe_logs(self) -> None:
        cases = [("empty", 200, {}, False), ("429", 200, {}, False), ("schema", 200, {}, False),
                 ("500", 402, {}, True), ("500", 200, {"code": "INSUFFICIENT_BALANCE"}, True),
                 ("500", 403, {"msg": "余额不足"}, True), ("500", 403, {}, False), ("500", 429, {}, False)]
        for exa, status, payload, exhausted in cases:
            with self.subTest(exa=exa, status=status, payload=payload):
                web_search_module._bocha_exhausted = False
                def handler(request):
                    body = json.loads(request.content)
                    if request.url.host == "api.bochaai.com":
                        self.assertEqual(body["query"], "TEST_query")
                        data = {"code": 200, "data": {"webPages": {"value": [{"name": "B", "url": "https://example.test/b",
                                "summary": "摘要", "datePublished": "2026-11-19"}]}}}
                        return httpx.Response(status, json=payload or (data if status == 200 else {}))
                    if exa in {"429", "500"}:
                        return httpx.Response(int(exa), text="TEST_SECRET TEST_query")
                    if exa == "schema":
                        return httpx.Response(200, text="TEST_SECRET")
                    if body["method"] == "tools/call":
                        self.assertEqual(body["params"]["arguments"]["query"], "TEST_query")
                    return exa_response(request, "No results found.")
                post = Mock(side_effect=handler)
                with self.assertLogs(web_search_module._logger, level="INFO") as logs:
                    for _ in range(2):
                        search = search_web(['"TEST_query" OR site:example.test'], api_key="TEST_SECRET", transport=httpx.MockTransport(post))
                        if exa in {"empty", "429", "schema"}:
                            self.assertEqual((await search)[0], SearchResult("B", "https://example.test/b", "摘要", "2026-11-19", "bocha"))
                        else:
                            with self.assertRaises(SearchError):
                                await search
                self.assertEqual(sum(c.args[0].url.host == 'api.bochaai.com' for c in post.call_args_list), 1 if exhausted else 2)
                self.assertEqual(sum("disabled=true" in line for line in logs.output), int(exhausted))
                self.assertNotIn("TEST_SECRET", " ".join(logs.output))
                self.assertNotIn("TEST_query", " ".join(logs.output))
                if exa == "schema":
                    self.assertTrue(any(r.provider == "bocha" and r.fallback_reason == "schema_error" for r in logs.records))

    async def test_timeout_opens_one_fresh_exa_session_before_bocha(self):
        for recover in (True, False):
            calls = []
            async def upstream(request):
                if request.url.host == 'api.bochaai.com':
                    calls.append('bocha')
                    return httpx.Response(200, json={'data': {'webPages': {'value': []}}})
                method = json.loads(request.content)['method']
                calls.append(method)
                if method == 'tools/call' and calls.count('initialize') == 1:
                    raise httpx.ReadTimeout('TEST')
                if not recover and calls.count('initialize') == 2:
                    await asyncio.Event().wait()
                return exa_response(request, block('retry'))
            results = await asyncio.wait_for(search_web(['TEST'], timeout_seconds=0.2, api_key='TEST_KEY',
                transport=httpx.MockTransport(upstream), browser=self.fail), 1)
            handshake = ['initialize', 'notifications/initialized', 'tools/call']
            self.assertEqual(calls, handshake + (handshake if recover else ['initialize', 'bocha']))
            self.assertEqual([r.title for r in results], ['retry'] if recover else [])

    async def test_concurrency_interleaving_and_partial_failure(self) -> None:
        active = peak = 0
        four, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            nonlocal active, peak
            body = json.loads(request.content)
            if body["method"] == "initialize":
                active += 1
                peak = max(peak, active)
                if active == 4:
                    four.set()
                await release.wait()
            if body["method"] == "tools/call":
                active -= 1
                query = body["params"]["arguments"]["query"]
                if query == "bad":
                    return httpx.Response(500)
                if query == "one":
                    await asyncio.sleep(0.01)
                return exa_response(request, block(query) + "\n\n---\n\n" + block("shared"))
            return exa_response(request, "")
        transport = httpx.MockTransport(handler)
        with self.assertLogs(web_search_module._logger, level="WARNING") as logs:
            tasks = [asyncio.create_task(search_web(queries, max_results=3, transport=transport))
                     for queries in (["one", "bad", "two"], ["three", "four"])]
            await asyncio.wait_for(four.wait(), 1)
            self.assertEqual((active, peak), (4, 4))
            release.set()
            first, second = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        self.assertEqual(peak, 4)
        self.assertEqual([r.title for r in first], ["one", "two", "shared"])
        self.assertEqual([r.title for r in second], ["three", "four", "shared"])
        self.assertEqual(logs.records[0].outcome, "http_error")

    async def test_empty_cleaned_queries_never_contact_providers(self) -> None:
        for queries in ([], ['"“”「」『』" OR site:example.test'], ['(-site:example.test)'], ['TEST', 'OR']):
            with self.subTest(queries=queries), self.assertRaises(SearchError):
                await search_web(queries, api_key="TEST_KEY", transport=httpx.MockTransport(self.fail))

    async def test_unexpected_internal_exception_propagates(self) -> None:
        handler = Mock(side_effect=RuntimeError('internal bug'))
        with self.assertRaisesRegex(RuntimeError, 'internal bug'):
            await search_web(['query'], api_key='TEST_SECRET', transport=httpx.MockTransport(handler))
        self.assertEqual([c.args[0].url.host for c in handler.call_args_list], ['mcp.exa.ai'])

    def test_render_search_sources_only_lists_cited_and_renumbers_outside_code(self) -> None:
        results = [
            SearchResult("One", "https://example.test/1", "s1"),
            SearchResult("Two", "https://example.test/2", "s2"),
            SearchResult("Three", "https://example.test/3", "s3"),
        ]
        self.assertEqual(render_search_sources(results, "不确定。"), ("不确定。", ""))
        answer = "先看[3]，再看[1]，忽略[0]和[9]。\n```python\narr = items[0] + items[2]\n```"
        rendered_answer, sources = render_search_sources(results, answer)
        self.assertEqual(rendered_answer, "先看[1]，再看[2]，忽略[0]和[9]。\n```python\narr = items[0] + items[2]\n```")
        self.assertEqual(sources, "来源：\n1. Three\nhttps://example.test/3\n2. One\nhttps://example.test/1")


if __name__ == "__main__":
    unittest.main()
