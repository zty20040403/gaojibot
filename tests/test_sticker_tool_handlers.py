from __future__ import annotations

import json
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import MessageSegment
from src.plugins.ai_chat.media_library import MediaRecord
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.tools.stickers import HANDLERS


class StickerToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image = Path(directory.name) / "fixture.png"
        image.write_bytes(b"fixture-image")
        self.record = MediaRecord(
            media_id=7,
            handle="media#7",
            summary="happy",
            description="smiling",
            extracted_text="",
            emotions=("happy",),
            usage=("hello",),
            is_sticker=True,
            safety="safe",
            storage_path=image,
            mime_type="image/png",
            score=0.9,
        )
        self.library = SimpleNamespace(
            search_stickers=AsyncMock(return_value=[self.record]),
            get_sticker=Mock(return_value=self.record),
            mark_sent=Mock(),
        )
        self.app = SimpleNamespace(media_library=self.library, logger=Mock())
        self.turn = TurnState(
            ToolContext(
                SimpleNamespace(context=self.app),
                Mock(),
                frozenset({"media", "stickers"}),
            ),
            user_text="happy",
        )
        self.registry = ToolRegistry(HANDLERS)

    async def run_tool(self, name: str, **arguments) -> dict:
        return json.loads(await self.registry.execute(self.turn, name, arguments))

    async def test_search_records_current_turn_handles_and_preserves_global_catalog(
        self,
    ) -> None:
        self.assertFalse((await self.run_tool("find_stickers", query=""))["ok"])
        response = await self.run_tool("find_stickers", query="happy", limit=500)
        self.library.search_stickers.assert_awaited_once_with("happy", limit=10)
        self.assertEqual(response["items"][0]["media_handle"], "media#7")
        self.assertEqual(self.turn.sticker_handles_this_turn, {"media#7"})
        self.assertIsNone(self.turn.visual_reply_segment)
        self.library.mark_sent.assert_not_called()
        self.assertEqual(TurnState(self.turn.context).sticker_handles_this_turn, set())

    async def test_query_takes_priority_over_stale_handle_and_prepares_only_one_reply(
        self,
    ) -> None:
        result = await self.run_tool(
            "send_sticker", query="happy", media_handle="media#999"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["media_handle"], "media#7")
        self.assertEqual(self.turn.visual_reply_segment.type, "image")
        self.library.search_stickers.assert_awaited_once_with("happy", limit=10)
        self.library.get_sticker.assert_not_called()
        self.library.mark_sent.assert_called_once_with(7)
        self.assertIn("准备发送", result["message"])
        first = self.turn.visual_reply_segment
        await self.run_tool("send_sticker", query="different")
        await self.run_tool("send_qq_face", expression="开心")
        self.assertIs(self.turn.visual_reply_segment, first)
        self.assertEqual(self.library.search_stickers.await_count, 1)

    async def test_only_handles_returned_this_turn_are_eligible_for_direct_read(
        self,
    ) -> None:
        self.library.search_stickers.return_value = []
        with patch(
            "src.plugins.ai_chat.tools.stickers.random_sticker_message"
        ) as fallback:
            result = await self.run_tool("send_sticker", media_handle="media#7")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "没有这个表情。")
            self.library.get_sticker.assert_not_called()
            fallback.assert_not_called()
        self.library.search_stickers.return_value = [self.record]
        await self.run_tool("find_stickers", query="happy")
        self.library.search_stickers.reset_mock()
        self.assertTrue(
            (await self.run_tool("send_sticker", media_handle="media#7"))["ok"]
        )
        self.library.get_sticker.assert_called_once_with(7)
        self.library.search_stickers.assert_not_awaited()

    async def test_change_request_never_reuses_recent_sticker_as_fallback(self) -> None:
        recent = replace(self.record, last_sent_at=int(time.time()))
        self.library.search_stickers.return_value = [recent]
        self.library.get_sticker.return_value = recent
        self.turn.user_text = "换个表情"
        self.turn.sticker_handles_this_turn.add("media#7")
        for arguments in ({"query": "happy"}, {"media_handle": "media#7"}):
            response = await self.run_tool("send_sticker", **arguments)
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "没有别的匹配表情。")
        self.assertIsNone(self.turn.visual_reply_segment)
        self.library.mark_sent.assert_not_called()

    async def test_unsafe_or_non_sticker_records_never_become_visual_replies(
        self,
    ) -> None:
        for record in (
            replace(self.record, safety="review"),
            replace(self.record, is_sticker=False),
        ):
            self.library.search_stickers.return_value = [record]
            self.assertFalse((await self.run_tool("send_sticker", query="happy"))["ok"])
        self.assertIsNone(self.turn.visual_reply_segment)
        self.library.mark_sent.assert_not_called()

    async def test_legacy_sticker_fallback_and_face_errors_keep_their_boundaries(
        self,
    ) -> None:
        self.app.media_library = None
        with patch(
            "src.plugins.ai_chat.tools.stickers.random_sticker_message",
            return_value=MessageSegment.face(14),
        ) as fallback:
            self.assertTrue((await self.run_tool("send_sticker"))["ok"])
            fallback.assert_called_once_with()
            self.assertEqual(self.turn.visual_reply_segment.type, "face")
        self.turn.visual_reply_segment = None
        with patch(
            "src.plugins.ai_chat.tools.stickers.qq_face_message",
            return_value="unknown expression",
        ):
            self.assertFalse(
                (await self.run_tool("send_qq_face", expression="invalid"))["ok"]
            )
        self.assertIsNone(self.turn.visual_reply_segment)
        self.turn.context = replace(self.turn.context, capabilities=frozenset())
        with patch(
            "src.plugins.ai_chat.tools.stickers.random_sticker_message"
        ) as fallback:
            self.assertEqual(
                (await self.run_tool("send_sticker"))["code"], "tool_unavailable"
            )
            fallback.assert_not_called()
