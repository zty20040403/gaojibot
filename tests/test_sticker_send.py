"""send_sticker picks a library sticker by meaning and sends at most one per reply."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied
from src.plugins.ai_chat.adapters.qq_official.media_tools import _send_sticker
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


class StickerSendTests(unittest.IsolatedAsyncioTestCase):
    def turn(self, records):
        library = SimpleNamespace(search_stickers=AsyncMock(return_value=records))
        context = ToolContext(SimpleNamespace(context=SimpleNamespace(media_library=library)))
        return TurnState(context), library

    async def test_query_picks_best_unused_sticker_and_sends_once(self):
        records = [SimpleNamespace(handle="media#6", mime_type="image/gif"),
                   SimpleNamespace(handle="media#7", mime_type="image/jpeg"), SimpleNamespace(handle="media#8", mime_type="image/png")]
        turn, library = self.turn(records)
        session = SimpleNamespace(sticker=AsyncMock(return_value={"state": "registered"}))
        result = await _send_sticker(session, turn, {"query": "买不起"})
        self.assertEqual(result["state"], "registered")
        library.search_stickers.assert_awaited_once_with("买不起", limit=20)
        session.sticker.assert_awaited_once()
        self.assertEqual(session.sticker.await_args.args[0], {"media_handle": "media#7"})
        second = await _send_sticker(session, turn, {"query": "笑死"})
        self.assertEqual(second["state"], "skipped")
        session.sticker.assert_awaited_once()

    async def test_no_match_sends_nothing(self):
        turn, _ = self.turn([])
        session = SimpleNamespace(sticker=AsyncMock())
        result = await _send_sticker(session, turn, {"query": "不存在的意思"})
        self.assertEqual(result["state"], "not_found")
        session.sticker.assert_not_awaited()

    async def test_no_query_sends_the_least_sent_sticker(self):
        turn, library = self.turn([SimpleNamespace(handle="media#3", mime_type="image/png")])
        session = SimpleNamespace(sticker=AsyncMock(return_value={"state": "registered"}))
        result = await _send_sticker(session, turn, {})
        self.assertEqual(result["state"], "registered")
        library.search_stickers.assert_awaited_once_with("", limit=20)
        self.assertEqual(session.sticker.await_args.args[0], {"media_handle": "media#3"})

    async def test_unusable_file_yields_to_the_next_candidate(self):
        records = [SimpleNamespace(handle="media#4", mime_type="image/png"),
                   SimpleNamespace(handle="media#5", mime_type="image/jpeg")]
        turn, _ = self.turn(records)
        session = SimpleNamespace(sticker=AsyncMock(side_effect=[MediaDenied("sticker_content_unavailable"),
                                                                   {"state": "registered"}]))
        result = await _send_sticker(session, turn, {"query": "猫"})
        self.assertEqual(result["state"], "registered")
        self.assertEqual(session.sticker.await_args.args[0], {"media_handle": "media#5"})


if __name__ == "__main__":
    unittest.main()
