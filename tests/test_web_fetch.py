from __future__ import annotations

import json
import unittest

import httpx

from src.plugins.ai_chat.web_fetch import FetchError, fetch_page, public_address
from src.plugins.ai_chat.web_search import search_freshness, search_web


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
                '<a class="result__a" href="https://shop.example/2">Scraped</a>'
                '<div class="result__snippet">snippet</div>'
            ))

        results = await search_web("query", provider="tavily", api_key="key",
                                   transport=httpx.MockTransport(handler))
        self.assertEqual([result.url for result in results], ["https://shop.example/2"])

    def test_direct_command_no_longer_limits_price_queries_to_one_day(self) -> None:
        self.assertIsNone(search_freshness("目前显卡价格"))
        self.assertEqual(search_freshness("今天天气"), "d")
        self.assertEqual(search_freshness("最近新闻"), "w")


if __name__ == "__main__":
    unittest.main()
