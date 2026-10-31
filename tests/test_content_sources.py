from __future__ import annotations

import json
import unittest



from src.plugins.ai_chat.content_sources import (
    canonicalize_source_url,
    classify_source,
    extract_shared_urls,
)
from src.plugins.ai_chat.message_ir import CardNode, MessageBody, TextNode


class ContentSourceHelpersTests(unittest.TestCase):
    def test_extracts_card_and_plain_urls_without_duplicates(self) -> None:
        body = MessageBody(
            (
                CardNode(
                    0,
                    title="测试视频",
                    url="https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=1",
                ),
                TextNode(
                    1,
                    "也看看 https://www.xiaohongshu.com/explore/abc?utm_source=qq。",
                ),
            )
        )
        urls = extract_shared_urls(body)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0][2], "测试视频")
        self.assertEqual(
            urls[1][1],
            "https://www.xiaohongshu.com/explore/abc?utm_source=qq",
        )

    def test_reextracts_public_url_from_historical_qq_card(self) -> None:
        payload = {
            "prompt": "[QQ小程序]测试视频",
            "meta": {
                "detail_1": {
                    "url": "m.q.qq.com/a/s/example",
                    "qqdocurl": "https://b23.tv/4VaXPFH",
                }
            },
        }
        body = MessageBody(
            (
                CardNode(
                    0,
                    title="[QQ小程序]测试视频",
                    url="m.q.qq.com/a/s/example",
                    raw_data={"data": json.dumps(payload)},
                ),
            )
        )

        self.assertEqual(
            extract_shared_urls(body),
            ((0, "https://b23.tv/4VaXPFH", "[QQ小程序]测试视频"),),
        )

    def test_canonicalizes_platform_urls_and_tracking_parameters(self) -> None:
        self.assertEqual(
            canonicalize_source_url(
                "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333#reply"
            ),
            "https://www.bilibili.com/video/BV1xx411c7mD",
        )
        self.assertEqual(
            canonicalize_source_url(
                "https://www.xiaohongshu.com/explore/abc?utm_source=qq&xsec_token=keep"
            ),
            "https://www.xiaohongshu.com/explore/abc?xsec_token=keep",
        )

    def test_classifies_supported_platforms_and_generic_pages(self) -> None:
        self.assertEqual(
            classify_source("https://b23.tv/abcd"),
            ("bilibili", "video"),
        )
        self.assertEqual(
            classify_source("https://www.xiaohongshu.com/explore/abc"),
            ("xiaohongshu", "post"),
        )
        self.assertEqual(
            classify_source("https://example.com/article"),
            ("web", "webpage"),
        )

    def test_rejects_non_http_and_credential_urls(self) -> None:
        with self.assertRaises(ValueError):
            canonicalize_source_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            canonicalize_source_url("https://user:pass@example.com/private")


if __name__ == "__main__":
    unittest.main()
