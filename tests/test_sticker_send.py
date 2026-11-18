"""send_sticker picks a library sticker by meaning and sends at most one per reply."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied
from src.plugins.ai_chat.adapters.qq_official.media_runtime import sticker_filename
from src.plugins.ai_chat.adapters.qq_official.media_tools import _send_sticker
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


class StickerSendTests(unittest.IsolatedAsyncioTestCase):
    def turn(self, records):
        library = SimpleNamespace(search_stickers=AsyncMock(return_value=records), mark_sent=Mock())
        context = ToolContext(SimpleNamespace(context=SimpleNamespace(media_library=library)))
        return TurnState(context), library

    def recent_turn(self, recent):
        records = [SimpleNamespace(handle="media#7", media_id=7, mime_type="image/png"),
                   SimpleNamespace(handle="media#8", media_id=8, mime_type="image/png")]
        turn, library = self.turn(records)
        library.recent_sticker_sends = Mock(return_value=recent)
        app = turn.context.app
        turn.context = SimpleNamespace(app=app, scope=SimpleNamespace(key="TEST_SCOPE"))
        return turn, library

    async def test_stickers_sent_here_lately_go_behind_fresh_ones(self):
        turn, library = self.recent_turn({7: 100})
        session = SimpleNamespace(sticker=AsyncMock(return_value={"state": "registered"}))
        await _send_sticker(session, turn, {"query": "酷"})
        self.assertEqual(session.sticker.await_args.args[0], {"media_handle": "media#8"})
        self.assertEqual(library.recent_sticker_sends.call_args.args[0], "TEST_SCOPE")

    async def test_only_recent_stickers_left_are_still_sent_oldest_first(self):
        turn, _ = self.recent_turn({7: 200, 8: 100})
        session = SimpleNamespace(sticker=AsyncMock(return_value={"state": "registered"}))
        await _send_sticker(session, turn, {"query": "酷"})
        self.assertEqual(session.sticker.await_args.args[0], {"media_handle": "media#8"})

    async def test_stickers_shown_by_find_stay_eligible_and_sends_are_counted(self):
        # find_stickers showed media#7; a query must still be allowed to pick it.
        turn, library = self.turn([SimpleNamespace(handle="media#7", mime_type="image/png")])
        turn.sticker_handles_seen.add("media#7")
        session = SimpleNamespace(sticker=AsyncMock(return_value={"state": "registered"}))
        result = await _send_sticker(session, turn, {"query": "得意"})
        self.assertEqual(result["state"], "registered")
        self.assertEqual(session.sticker.await_args.args[0], {"media_handle": "media#7"})
        library.mark_sent.assert_called_once_with(7)

    async def test_handle_from_find_is_observed_and_a_broken_one_is_not_counted(self):
        turn, library = self.turn([])
        turn.sticker_handles_seen.add("media#9")
        session = SimpleNamespace(sticker=AsyncMock(side_effect=MediaDenied("sticker_content_unavailable")))
        with self.assertRaises(MediaDenied):
            await _send_sticker(session, turn, {"media_handle": "media#9"})
        self.assertIn("media#9", session.sticker.await_args.args[1])
        self.assertIn("media#9", turn.sticker_handles_this_turn)
        self.assertFalse(turn.sticker_sent)
        library.mark_sent.assert_not_called()

    def test_sticker_file_is_named_after_its_tag(self):
        self.assertEqual(sticker_filename("宝宝露出调皮微笑", "image/jpeg"), "表情-宝宝露出调皮微笑.jpg")
        self.assertEqual(sticker_filename("a/../b\n", "image/png"), "表情-ab.png")
        self.assertEqual(sticker_filename("", "image/gif"), "sticker.gif")

    async def test_query_picks_best_unused_sticker_and_sends_once(self):
        records = [SimpleNamespace(handle="media#6", mime_type="image/webp"),
                   SimpleNamespace(handle="media#7", mime_type="image/gif"), SimpleNamespace(handle="media#8", mime_type="image/png")]
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


class StickerMarkerTests(unittest.IsolatedAsyncioTestCase):
    """[表情：…] at the end of a reply queues one sticker without an extra model round."""

    def test_markers_are_removed_and_the_last_query_is_used(self):
        from src.plugins.ai_chat.adapters.qq_official.media_tools import strip_sticker_markers
        self.assertEqual(strip_sticker_markers("笑死我了\n[表情：笑死 拍桌]"), ("笑死我了", "笑死 拍桌"))
        self.assertEqual(strip_sticker_markers("好的[表情:无语]"), ("好的", "无语"))
        self.assertEqual(strip_sticker_markers("普通回复"), ("普通回复", None))
        self.assertEqual(strip_sticker_markers("[表情：a]\n中间\n[表情：b]")[1], "b")

    def session(self):
        return SimpleNamespace(enabled=lambda name: name == "send_sticker",
                               sticker=AsyncMock(return_value={"state": "queued", "artifact_id": "pending:media#7"}))

    def marker_turn(self, recent):
        library = SimpleNamespace(search_stickers=AsyncMock(return_value=[SimpleNamespace(
            handle="media#7", media_id=7, mime_type="image/png")]), mark_sent=Mock(),
            recent_sticker_sends=Mock(return_value=recent))
        turn = TurnState(ToolContext(SimpleNamespace(context=SimpleNamespace(media_library=library))))
        turn.context = SimpleNamespace(app=turn.context.app, scope=SimpleNamespace(key="TEST_SCOPE"))
        return turn

    async def test_marker_queues_a_sticker_when_none_was_sent_lately(self):
        import time
        from src.plugins.ai_chat.adapters.qq_official.media_tools import take_sticker_marker
        session, turn = self.session(), self.marker_turn({8: int(time.time()) - 600})
        self.assertEqual(await take_sticker_marker(session, turn, "哈哈\n[表情：笑死]"), "哈哈")
        session.sticker.assert_awaited_once()
        self.assertTrue(turn.sticker_sent and turn.delivered)
