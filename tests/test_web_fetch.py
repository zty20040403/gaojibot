from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from src.plugins.ai_chat import web_search as web_search_module
from src.plugins.ai_chat.web_fetch import FetchError, fetch_page, public_address
from src.plugins.ai_chat.web_search import SearchResult, clean_source_url, relevant, render_search_sources, search_freshness, search_web


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


class WebSearchProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_bocha_results_carry_publish_dates(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": [
                {"name": "RTX 5070 报价", "url": "https://shop.example/1", "snippet": "短",
                 "summary": "到手价 4599 元", "datePublished": "2026-10-29T08:00:00+08:00"},
            ]}}})

        results = await search_web("RTX 5070 价格", freshness="w", provider="bocha", api_key="key",
                                   transport=httpx.MockTransport(handler))
        self.assertEqual(str(seen[0].url), "https://api.bochaai.com/v1/web-search")
        self.assertEqual(seen[0].headers["authorization"], "Bearer key")
        self.assertEqual(json.loads(seen[0].content)["freshness"], "oneWeek")
        self.assertEqual(results[0].title, "RTX 5070 报价")
        self.assertEqual(results[0].snippet, "[2026-10-29] 到手价 4599 元")

    async def test_failed_api_falls_back_to_scraping(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.tavily.com":
                return httpx.Response(432, json={"detail": "out of credits"})
            return httpx.Response(200, text=(
                '<a class="result__a" href="//duckduckgo.com/y.js?ad_domain=ads.example">Ad</a>'
                '<div class="result__snippet">ad</div>'
                '<a class="result__a" href="https://shop.example/2">Scraped query</a>'
                '<div class="result__snippet">snippet</div>'
            ))

        results = await search_web("query", provider="tavily", api_key="key",
                                   transport=httpx.MockTransport(handler))
        self.assertEqual([result.url for result in results], ["https://shop.example/2"])

    async def test_paid_api_failure_filters_actual_scrape_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.tavily.com":
                return httpx.Response(432, json={"detail": "out of credits"})
            return httpx.Response(200, text=(
                '<a class="result__a" href="https://afdian.com/a">爱发电创作者</a>'
                '<div class="result__snippet">赞助支持</div>'
                '<a class="result__a" href="https://example.test/b">吴家村是什么梗</a>'
                '<div class="result__snippet">解释</div>'
            ))

        with self.assertLogs("src.plugins.ai_chat.web_search", level="WARNING"):
            results = await search_web("吴家村 是哪里", max_results=1, provider="tavily", api_key="key",
                                       transport=httpx.MockTransport(handler))
        self.assertEqual([result.url for result in results], ["https://example.test/b"])

    async def test_paid_api_results_are_not_filtered_as_scrape(self) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"results": [
            {"title": "不含查询词", "url": "https://example.test/api", "content": "语义相关"}
        ]}))
        results = await search_web("吴家村", provider="tavily", api_key="key", transport=transport)
        self.assertEqual([result.url for result in results], ["https://example.test/api"])

    def test_direct_command_no_longer_limits_price_queries_to_one_day(self) -> None:
        self.assertIsNone(search_freshness("目前显卡价格"))
        self.assertEqual(search_freshness("今天天气"), "d")
        self.assertEqual(search_freshness("最近新闻"), "w")

    def test_clean_source_url_strips_tracking_parameters(self) -> None:
        self.assertEqual(
            clean_source_url("https://www.bilibili.com/video/BV1TEST?vd_source=abc&p=2&share_source=copy&t=42"),
            "https://www.bilibili.com/video/BV1TEST?p=2&t=42",
        )
        self.assertEqual(
            clean_source_url("https://b23.tv/TEST123?share_medium=android&buvid=xyz"),
            "https://b23.tv/TEST123",
        )
        self.assertEqual(
            clean_source_url("https://shop.example/item?id=9&utm_source=x&share_id=1&spm=2&fbclid=3"),
            "https://shop.example/item?id=9",
        )
        self.assertEqual(clean_source_url("http://[::1"), "http://[::1")

    def test_render_search_sources_only_lists_cited_and_renumbers_outside_code(self) -> None:
        results = [
            SearchResult("One", "https://example.test/1", "s1"),
            SearchResult("Two", "https://example.test/2", "s2"),
            SearchResult("Three", "https://example.test/3", "s3"),
        ]
        self.assertEqual(render_search_sources(results, "不确定。"), ("不确定。", ""))
        answer = "先看[3]，再看[1]，忽略[0]和[9]。\n```python\narr = items[0] + items[2]\n```"
        rendered_answer, sources = render_search_sources(results, answer)
        self.assertEqual(
            rendered_answer,
            "先看[1]，再看[2]，忽略[0]和[9]。\n```python\narr = items[0] + items[2]\n```",
        )
        self.assertEqual(
            sources,
            "来源：\n1. Three\nhttps://example.test/3\n2. One\nhttps://example.test/1",
        )

    async def test_duckduckgo_anomaly_block_skips_subsequent_requests_for_one_hour(self) -> None:
        web_search_module._ddg_blocked_until = 0.0
        self.addCleanup(setattr, web_search_module, "_ddg_blocked_until", 0.0)
        ddg_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "duckduckgo.com":
                ddg_hosts.append(request.url.host)
                return httpx.Response(200, text="<html><body>anomaly challenge</body></html>")
            return httpx.Response(
                200,
                text=(
                    "<rss><channel><item><title>吴家村 梗</title>"
                    "<link>https://example.test/bing</link>"
                    "<description>解释</description></item></channel></rss>"
                ),
            )

        with self.assertLogs("src.plugins.ai_chat.web_search", level="INFO") as logs:
            first = await search_web("吴家村", transport=httpx.MockTransport(handler))
            second = await search_web("吴家村", transport=httpx.MockTransport(handler))
        self.assertEqual([r.url for r in first], ["https://example.test/bing"])
        self.assertEqual([r.url for r in second], ["https://example.test/bing"])
        self.assertEqual(len(ddg_hosts), 1)
        self.assertEqual(sum("anomaly" in line.lower() for line in logs.output), 1)

    async def test_403_anomaly_still_opens_cooldown(self) -> None:
        web_search_module._ddg_blocked_until = 0.0
        self.addCleanup(setattr, web_search_module, '_ddg_blocked_until', 0.0)
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            if request.url.host == 'duckduckgo.com':
                return httpx.Response(403, text='anomaly challenge')
            return httpx.Response(200, text='<rss><channel/></rss>')
        with self.assertLogs('src.plugins.ai_chat.web_search', level='INFO') as logs:
            await search_web('吴家村', transport=httpx.MockTransport(handler))
            await search_web('吴家村', transport=httpx.MockTransport(handler))
        self.assertEqual(hosts.count('duckduckgo.com'), 1)
        self.assertEqual(hosts.count('www.bing.com'), 2)
        self.assertEqual(sum('anomaly' in line.lower() for line in logs.output), 1)

    async def test_filtered_duckduckgo_hits_fall_back_to_relevant_bing(self) -> None:
        web_search_module._ddg_blocked_until = 0.0
        self.addCleanup(setattr, web_search_module, '_ddg_blocked_until', 0.0)
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            if request.url.host == 'duckduckgo.com':
                return httpx.Response(200, text='<a class="result__a" href="https://example.test/no">爱发电</a>')
            return httpx.Response(200, text=(
                '<rss><channel><item><title>吴家村是什么梗</title><link>https://example.test/yes</link>'
                '<description>解释</description></item></channel></rss>'
            ))
        results = await search_web('吴家村 是哪里', transport=httpx.MockTransport(handler))
        self.assertEqual(hosts, ['duckduckgo.com', 'www.bing.com'])
        self.assertEqual([r.url for r in results], ['https://example.test/yes'])

    async def test_unrelated_duckduckgo_and_failed_bing_return_empty(self) -> None:
        web_search_module._ddg_blocked_until = 0.0
        self.addCleanup(setattr, web_search_module, '_ddg_blocked_until', 0.0)
        def handler(request):
            if request.url.host == 'duckduckgo.com':
                return httpx.Response(200, text='<a class="result__a" href="https://example.test/no">爱发电</a>')
            return httpx.Response(500)
        try:
            results = await search_web('吴家村', transport=httpx.MockTransport(handler))
        except web_search_module.SearchError as error:
            self.fail(f'an unrelated DDG result still permits an empty answer: {error}')
        self.assertEqual(results, [])

    async def test_bocha_error_envelope_falls_back_without_leaking_key(self) -> None:
        web_search_module._ddg_blocked_until = 0.0
        self.addCleanup(setattr, web_search_module, '_ddg_blocked_until', 0.0)
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            if request.url.host == 'api.bochaai.com':
                return httpx.Response(200, json={'code': 401, 'msg': 'TEST_SECRET'})
            return httpx.Response(200, text='<a class="result__a" href="https://example.test/yes">吴家村</a>')
        with self.assertLogs('src.plugins.ai_chat.web_search', level='WARNING') as logs:
            results = await search_web('吴家村', provider='bocha', api_key='TEST_SECRET',
                                       transport=httpx.MockTransport(handler))
        self.assertEqual(hosts, ['api.bochaai.com', 'duckduckgo.com'])
        self.assertEqual([r.url for r in results], ['https://example.test/yes'])
        self.assertNotIn('TEST_SECRET', '\n'.join(logs.output))

    async def test_concurrent_anomalies_log_only_once_and_skip_new_requests(self) -> None:
        web_search_module._ddg_blocked_until = 0.0
        self.addCleanup(setattr, web_search_module, "_ddg_blocked_until", 0.0)
        both_started = asyncio.Event()
        count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal count
            if request.url.host == "duckduckgo.com":
                count += 1
                if count == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=2)
                return httpx.Response(200, text="anomaly challenge")
            return httpx.Response(200, text=(
                "<rss><channel><item><title>吴家村 梗</title>"
                "<link>https://example.test/bing</link><description>解释</description>"
                "</item></channel></rss>"
            ))

        transport = httpx.MockTransport(handler)
        with self.assertLogs("src.plugins.ai_chat.web_search", level="INFO") as logs:
            await asyncio.wait_for(asyncio.gather(
                search_web("吴家村", transport=transport), search_web("吴家村", transport=transport),
            ), timeout=5)
            await search_web("吴家村", transport=transport)
        self.assertEqual(count, 2)
        self.assertEqual(sum("anomaly" in line.lower() for line in logs.output), 1)

    def test_relevant_filters_unrelated_results(self) -> None:
        results = [
            SearchResult("爱发电创作者主页", "https://afdian.com/1", "支持创作者"),
            SearchResult("吴家村是什么梗", "https://example.test/wjc", "抖音热梗"),
        ]
        self.assertEqual([r.url for r in relevant("吴家村 是哪里", results)], ["https://example.test/wjc"])
        self.assertEqual(len(relevant("a", results)), 2)


if __name__ == "__main__":
    unittest.main()
