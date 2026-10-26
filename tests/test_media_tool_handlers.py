from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from src.plugins.ai_chat.chat_orchestrator import OneBotChatService
from src.plugins.ai_chat.ocr import OCRError, RecentImageStore
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.tools.media import HANDLERS
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.video import RecentVideoStore
from src.plugins.ai_chat.voice import VoiceError


def event(message: Message, *, private: bool = False):
    fields = dict(
        time=1,
        self_id=999,
        post_type="message",
        user_id=7,
        message_id=10,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": 7, "nickname": "Alice"},
    )
    if private:
        return PrivateMessageEvent(**fields, message_type="private", sub_type="friend")
    return GroupMessageEvent(
        **fields, message_type="group", sub_type="normal", group_id=100
    )


def media_message(kind: str, source: str) -> Message:
    return Message([MessageSegment(kind, {"url": source})])


class MediaToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.worker = SimpleNamespace(
            submit_and_wait=AsyncMock(
                return_value=SimpleNamespace(
                    as_dict=lambda: {"summary": "image description"}
                )
            )
        )
        self.app = SimpleNamespace(
            settings=SimpleNamespace(
                media_timeout_seconds=180,
                ocr_timeout_seconds=12,
                ocr_max_chars=1000,
                voice_timeout_seconds=30,
                voice_provider="local",
                voice_name="voice",
                voice_rate="+0%",
                voice_pitch="+0Hz",
                voice_local_name="voice",
                voice_local_rate=160,
                voice_max_chars=300,
            ),
            logger=Mock(),
            vision_worker=self.worker,
            message_ledger=Mock(),
            recent_images=RecentImageStore(300),
            recent_videos=RecentVideoStore(300),
        )
        self.bot = AsyncMock()
        self.services = SimpleNamespace(
            context=self.app,
            commands=Mock(),
            video_analyzer=SimpleNamespace(
                analyze_qq_video=AsyncMock(
                    return_value={"summary": "video description"}
                )
            ),
        )
        self.services.chat = OneBotChatService(self.services)
        self.services.commands._canonical_message_id.return_value = 23
        self.turn = TurnState(
            ToolContext(
                self.services,
                event(Message("look")),
                frozenset({"ocr", "media", "video", "transcription", "voice"}),
            ),
            bot=self.bot,
        )
        self.registry = ToolRegistry(HANDLERS)

    async def run_tool(self, name: str, **arguments) -> dict:
        return json.loads(await self.registry.execute(self.turn, name, arguments))

    async def test_image_current_reply_and_recent_work_for_group_and_private(
        self,
    ) -> None:
        for private in (False, True):
            for origin in ("current", "reply", "recent"):
                with self.subTest(private=private, origin=origin):
                    source = f"https://qpic.cn/{origin}.jpg"
                    message = (
                        media_message("image", source)
                        if origin == "current"
                        else Message("look")
                    )
                    if origin == "reply":
                        message = Message(
                            [MessageSegment.reply(20), MessageSegment.text("look")]
                        )
                    current = event(message, private=private)
                    self.turn.context = replace(self.turn.context, event=current)
                    key = self.services.chat._image_cache_key(current)
                    self.app.recent_images.record(
                        key,
                        [source if origin == "recent" else "https://qpic.cn/stale.jpg"],
                    )
                    self.bot.get_msg.return_value = {
                        "message": media_message("image", source)
                    }
                    self.bot.get_msg.reset_mock()
                    self.worker.submit_and_wait.reset_mock()
                    result = await self.run_tool(
                        "view_image", mode="detail", question="read title"
                    )
                    self.assertTrue(result["ok"])
                    self.assertFalse(result["stored"])
                    self.worker.submit_and_wait.assert_awaited_once_with(
                        scope_key=scope_from_event(current).key,
                        native_message_id=20 if origin == "reply" else 10,
                        segment_index=0,
                        requester_native_user_id=7,
                        source_url=source,
                        mode="detail",
                        question="read title",
                        wait_seconds=45,
                    )
                    self.assertEqual(
                        self.bot.get_msg.await_count, int(origin == "reply")
                    )

    async def test_explicit_image_handle_checks_scope_before_reading_qq(self) -> None:
        self.app.message_ledger.get_in_scope.return_value = None
        result = await self.run_tool("view_image", message_handle="msg#23")
        self.assertFalse(result["ok"])
        self.app.message_ledger.get_in_scope.assert_called_once_with(
            scope_from_event(self.turn.context.event), 23
        )
        self.bot.get_msg.assert_not_awaited()
        self.worker.submit_and_wait.assert_not_awaited()
        self.app.message_ledger.get_in_scope.return_value = SimpleNamespace(
            native_message_id="27"
        )
        self.bot.get_msg.return_value = {
            "message": media_message("image", "https://qpic.cn/allowed.jpg")
        }
        self.assertTrue(
            (await self.run_tool("view_image", message_handle="msg#23"))["ok"]
        )
        self.bot.get_msg.assert_awaited_once_with(message_id=27)

    async def test_absent_or_invalid_image_returns_without_waiting_for_worker(
        self,
    ) -> None:
        for args in ({}, {"segment_index": 42}):
            self.assertFalse((await self.run_tool("view_image", **args))["ok"])
        self.worker.submit_and_wait.assert_not_awaited()
        self.turn.context = replace(
            self.turn.context,
            event=event(media_message("image", "https://qpic.cn/one.jpg")),
        )
        self.worker.submit_and_wait.return_value = None
        self.assertFalse((await self.run_tool("view_image"))["ok"])

    async def test_video_lookup_order_and_scope_match_image_contract(self) -> None:
        analyzer = self.services.video_analyzer.analyze_qq_video
        for private in (False, True):
            for origin in ("current", "reply", "recent"):
                with self.subTest(private=private, origin=origin):
                    source = f"https://qq.com/{origin}.mp4"
                    message = (
                        media_message("video", source)
                        if origin == "current"
                        else Message("watch")
                    )
                    if origin == "reply":
                        message = Message([MessageSegment.reply(20)])
                    current = event(message, private=private)
                    self.turn.context = replace(self.turn.context, event=current)
                    self.app.recent_videos.record(
                        self.services.chat._video_cache_key(current), 30
                    )
                    self.bot.get_msg.return_value = {
                        "message": media_message("video", source)
                    }
                    self.bot.get_msg.reset_mock()
                    analyzer.reset_mock()
                    self.assertTrue(
                        (await self.run_tool("view_video", question="review"))["ok"]
                    )
                    analyzer.assert_awaited_once_with(source, question="review")
                    if origin != "current":
                        self.bot.get_msg.assert_awaited_once_with(
                            message_id=20 if origin == "reply" else 30
                        )
                    else:
                        self.bot.get_msg.assert_not_awaited()
        self.app.message_ledger.get_in_scope.return_value = None
        analyzer.reset_mock()
        self.bot.get_msg.reset_mock()
        self.assertFalse(
            (await self.run_tool("view_video", message_handle="msg#23"))["ok"]
        )
        self.app.message_ledger.get_in_scope.assert_called_once_with(
            scope_from_event(self.turn.context.event), 23
        )
        self.bot.get_msg.assert_not_awaited()
        analyzer.assert_not_awaited()

    async def test_ocr_and_transcription_only_record_successful_current_turn_text(
        self,
    ) -> None:
        self.turn.image_sources = ["https://qpic.cn/one.jpg"]
        self.turn.voice_message_id = 55
        for tool, function, error, evidence in (
            ("read_image_text", "recognize_images", OCRError, self.turn.used_ocr_texts),
            (
                "transcribe_voice",
                "transcribe_voice",
                VoiceError,
                self.turn.used_voice_texts,
            ),
        ):
            with patch(
                "src.plugins.ai_chat.tools.media." + function, new_callable=AsyncMock
            ) as operation:
                operation.return_value = "recognized"
                self.assertTrue((await self.run_tool(tool))["ok"])
                self.assertEqual(evidence, ["recognized"])
                self.assertIs(operation.call_args.args[0], self.bot)
                operation.side_effect = error("unavailable")
                self.assertFalse((await self.run_tool(tool))["ok"])
                operation.side_effect = None
                operation.return_value = ""
                self.assertFalse((await self.run_tool(tool))["ok"])
                self.assertEqual(evidence, ["recognized"])
        fresh = TurnState(self.turn.context)
        self.assertEqual(fresh.used_ocr_texts, [])
        self.assertEqual(fresh.used_voice_texts, [])

    async def test_voice_synthesis_happens_once_and_does_not_claim_delivery(
        self,
    ) -> None:
        with patch(
            "src.plugins.ai_chat.tools.media.synthesize_silk_voice",
            new_callable=AsyncMock,
        ) as synth:
            synth.return_value = (b"audio", "spoken text")
            result = await self.run_tool("reply_with_voice", text="answer")
            self.assertTrue(result["ok"])
            self.assertIn("等待发送", result["message"])
            self.assertEqual(self.turn.voice_reply_segment.type, "record")
            self.assertEqual(self.turn.voice_reply_text, "spoken text")
            self.assertTrue(
                (await self.run_tool("reply_with_voice", text="again"))["ok"]
            )
            synth.assert_awaited_once()
            self.assertEqual(synth.call_args.kwargs["timeout_seconds"], 30)
            self.turn.voice_reply_segment = None
            synth.side_effect = VoiceError("failed")
            self.assertFalse(
                (await self.run_tool("reply_with_voice", text="answer"))["ok"]
            )
            self.assertIsNone(self.turn.voice_reply_segment)

    async def test_missing_capabilities_do_not_execute_media_tools(self) -> None:
        self.turn.context = replace(self.turn.context, capabilities=frozenset())
        self.assertEqual(self.registry.definitions(self.turn.context), [])
        for handler in HANDLERS:
            result = await self.run_tool(handler.definition["function"]["name"])
            self.assertEqual(result["code"], "tool_unavailable")
        self.worker.submit_and_wait.assert_not_awaited()
        self.services.video_analyzer.analyze_qq_video.assert_not_awaited()
