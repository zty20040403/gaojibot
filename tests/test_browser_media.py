from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
import nonebot

nonebot.init()

from src.plugins.ai_chat.ai_tools import (
    BROWSER_CLEAR_TOOL_NAME,
    BROWSER_NAVIGATE_TOOL_NAME,
    GET_SHARED_CONTENT_TOOL_NAME,
    INSPECT_SHARED_CONTENT_TOOL_NAME,
    VIEW_BILIBILI_TOOL_NAME,
    VIEW_FORWARD_TOOL_NAME,
)
from src.plugins.ai_chat.tools import available_tools
from src.plugins.ai_chat.tools.state import ToolContext
from src.plugins.ai_chat.browser_tools import (
    BrowserManager,
    BrowserPolicyError,
    RichMessageRenderer,
    _render_table_html,
    parse_rich_block,
)
from src.plugins.ai_chat.media_tools import BilibiliClient, find_bilibili_ref


class BrowserAndMediaTests(unittest.TestCase):
    def test_table_renderer_uses_server_available_chinese_fonts(self) -> None:
        markup = _render_table_html(
            "| 题型 | 得分 |\n| --- | --- |\n| 选择题 | 40 |"
        )

        self.assertIsNotNone(markup)
        self.assertIn('"Noto Sans CJK SC"', markup)
        self.assertIn('"Sarasa Gothic SC"', markup)

    def test_rich_block_parser_distinguishes_code_table_and_plain_text(self) -> None:
        self.assertEqual(
            parse_rich_block("```python\nprint('ok')\n```")[0:2],  # type: ignore[index]
            ("code", "python"),
        )
        self.assertEqual(
            parse_rich_block("| a | b |\n| --- | :---: |\n| 1 | 2 |")[0],  # type: ignore[index]
            "table",
        )
        self.assertIsNone(parse_rich_block("ordinary reply"))

    def test_codesnap_renderer_caches_valid_png_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "fake-codesnap"
            counter = root / "calls"
            executable.write_text(
                "#!/bin/sh\n"
                "output=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = '--output' ]; then output=\"$2\"; shift 2; "
                "else shift; fi\n"
                "done\n"
                "cat >/dev/null\n"
                f"printf x >>'{counter}'\n"
                "printf '\\211PNG\\r\\n\\032\\n012345678901234567890123' >\"$output\"\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            renderer = RichMessageRenderer(
                codesnap_executable_path=str(executable),
                codesnap_cache_root=root / "cache",
            )

            async def run() -> tuple[bytes | None, bytes | None]:
                first = await renderer.render(
                    '```python\nprint("中文不会乱码")\n```'
                )
                second = await renderer.render(
                    '```python\nprint("中文不会乱码")\n```'
                )
                return first, second

            first, second = asyncio.run(run())
            self.assertTrue(first.startswith(b"\x89PNG"))  # type: ignore[union-attr]
            self.assertEqual(first, second)
            self.assertEqual(counter.read_text(encoding="utf-8"), "x")

    def test_browser_policy_blocks_loopback_before_launching_playwright(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manager = BrowserManager(temp)

            async def run() -> None:
                with self.assertRaises(BrowserPolicyError):
                    await manager.navigate("group:1", "http://127.0.0.1/private")

            asyncio.run(run())

    def test_browser_profile_clear_is_owner_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manager = BrowserManager(temp)
            alice_key = hashlib.sha256(b"alice").hexdigest()[:24]
            bob_key = hashlib.sha256(b"bob").hexdigest()[:24]
            alice = manager.profile_root / alice_key
            bob = manager.profile_root / bob_key
            alice.mkdir()
            bob.mkdir()
            (alice / "cookie").write_text("private")
            (bob / "cookie").write_text("keep")

            self.assertTrue(asyncio.run(manager.clear_profile("alice")))
            self.assertFalse(alice.exists())
            self.assertTrue(bob.exists())

    def test_bilibili_metadata_and_comments_are_parsed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/x/web-interface/view"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "bvid": "BV1xx411c7mD",
                            "aid": 42,
                            "cid": 99,
                            "title": "Test video",
                            "desc": "Description",
                            "owner": {"name": "UP"},
                            "duration": 123,
                            "pubdate": 100,
                            "videos": 1,
                            "stat": {"view": 10, "like": 2},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "replies": [
                            {
                                "member": {"uname": "Alice"},
                                "like": 3,
                                "content": {"message": "nice"},
                            }
                        ]
                    },
                },
            )

        async def run():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            client = BilibiliClient(client=http)
            result = await client.inspect("BV1xx411c7mD")
            await http.aclose()
            return result

        result = asyncio.run(run())
        self.assertEqual(result["title"], "Test video")
        self.assertEqual(result["top_comments"][0]["text"], "nice")  # type: ignore[index]
        self.assertEqual(find_bilibili_ref("https://b23.tv/AbCd").short_url, "https://b23.tv/AbCd")  # type: ignore[union-attr]

    def test_bilibili_media_streams_choose_bounded_avc_video(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/x/web-interface/view"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "bvid": "BV1xx411c7mD",
                            "cid": 99,
                            "duration": 123,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [
                                {
                                    "height": 480,
                                    "bandwidth": 100,
                                    "codecs": "hev1",
                                    "baseUrl": "https://cdn.example/hevc.m4s",
                                },
                                {
                                    "height": 480,
                                    "bandwidth": 90,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.example/avc.m4s",
                                },
                                {
                                    "height": 1080,
                                    "bandwidth": 200,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.example/1080.m4s",
                                },
                            ],
                            "audio": [
                                {
                                    "bandwidth": 64,
                                    "baseUrl": "https://cdn.example/low.m4s",
                                },
                                {
                                    "bandwidth": 128,
                                    "baseUrl": "https://cdn.example/high.m4s",
                                },
                            ],
                        }
                    },
                },
            )

        async def run():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            client = BilibiliClient(client=http)
            result = await client.media_streams("BV1xx411c7mD")
            await http.aclose()
            return result

        result = asyncio.run(run())
        self.assertEqual(result["video_url"], "https://cdn.example/avc.m4s")
        self.assertEqual(result["audio_url"], "https://cdn.example/high.m4s")
        self.assertEqual(result["duration_seconds"], 123)

    def test_tool_catalog_exposes_conversation_and_browser_tools_separately(self) -> None:
        executor = SimpleNamespace(browser_manager=object(), source_store=object())
        tools = available_tools(ToolContext(
            SimpleNamespace(), SimpleNamespace(),
            frozenset({"conversation", "browser", "media"}), executor,
        ))
        names = {
            item["function"]["name"]
            for item in tools
        }
        self.assertIn(VIEW_FORWARD_TOOL_NAME, names)
        self.assertIn(VIEW_BILIBILI_TOOL_NAME, names)
        self.assertIn(BROWSER_NAVIGATE_TOOL_NAME, names)
        self.assertIn(BROWSER_CLEAR_TOOL_NAME, names)
        self.assertIn("view_image", names)
        self.assertNotIn("find_images", names)
        self.assertIn("find_stickers", names)

        source_tools = available_tools(ToolContext(
            SimpleNamespace(), SimpleNamespace(),
            frozenset({"conversation", "shared_content"}), executor,
        ))
        source_names = {item["function"]["name"] for item in source_tools}
        self.assertIn(INSPECT_SHARED_CONTENT_TOOL_NAME, source_names)
        self.assertIn(GET_SHARED_CONTENT_TOOL_NAME, source_names)
        self.assertNotIn(VIEW_BILIBILI_TOOL_NAME, source_names)


if __name__ == "__main__":
    unittest.main()
