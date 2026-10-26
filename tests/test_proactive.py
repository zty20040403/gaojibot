from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message


from src.plugins.ai_chat.proactive import (
    ProactiveCheckGate,
    ProactiveDecision,
    is_candidate_message,
    parse_proactive_decision,
    should_use_proactive_voice,
)


from tests.handler_fixture import HandlerTestCaseMixin
from src.plugins.ai_chat.voice import VoiceError


def group_event(message_id: int, text: str) -> GroupMessageEvent:
    message = Message(text)
    return GroupMessageEvent(
        time=message_id,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=7,
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender={
            "user_id": 7,
            "nickname": "Alice",
            "card": "",
            "role": "member",
        },
        group_id=100,
    )


def proactive_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "proactive_enabled": True,
        "proactive_interest_threshold": 90,
        "proactive_gate_percent": 100,
        "proactive_max_checks_per_hour": 100,
        "proactive_classifier_profile": "deepseek",
        "proactive_voice_percent": 60,
        "voice_enabled": True,
        "voice_provider": "edge",
        "voice_name": "cute",
        "voice_rate": "+0%",
        "voice_pitch": "+0Hz",
        "voice_local_name": "Tingting",
        "voice_local_rate": 210,
        "voice_max_chars": 350,
        "voice_timeout_seconds": 45,
        "is_group_enabled": lambda _group_id: True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ProactivePolicyTests(unittest.TestCase):
    def test_decision_requires_reply_and_threshold(self) -> None:
        decision = parse_proactive_decision(
            {"interest": "93", "reply": "这个我会", "voice_suitable": True}
        )

        self.assertEqual(decision, ProactiveDecision(93, "这个我会", True))
        self.assertTrue(decision.should_reply(90))
        self.assertFalse(ProactiveDecision(100, "", True).should_reply(90))
        self.assertFalse(ProactiveDecision(89, "想说话", True).should_reply(90))

    def test_candidate_filter_skips_low_value_messages_without_llm(self) -> None:
        self.assertTrue(is_candidate_message("这个架构为什么这样设计"))
        self.assertFalse(is_candidate_message("哦"))
        self.assertFalse(is_candidate_message("[图片]"))
        self.assertFalse(is_candidate_message("https://example.com"))
        self.assertFalse(is_candidate_message("   "))
        self.assertFalse(is_candidate_message("/ai 问题"))

    def test_check_gate_samples_and_enforces_hourly_group_limit(self) -> None:
        gate = ProactiveCheckGate()
        self.assertTrue(
            gate.allows(
                100,
                percent=100,
                max_checks_per_hour=2,
                now=lambda: 1000,
                random_value=lambda: 0,
            )
        )
        self.assertTrue(
            gate.allows(
                100,
                percent=100,
                max_checks_per_hour=2,
                now=lambda: 1001,
                random_value=lambda: 0,
            )
        )
        self.assertFalse(
            gate.allows(
                100,
                percent=100,
                max_checks_per_hour=2,
                now=lambda: 1002,
                random_value=lambda: 0,
            )
        )
        self.assertTrue(
            gate.allows(
                100,
                percent=100,
                max_checks_per_hour=2,
                now=lambda: 4601,
                random_value=lambda: 0,
            )
        )

    def test_voice_probability_is_independent_of_reply_frequency(self) -> None:
        self.assertTrue(should_use_proactive_voice(60, random_value=lambda: 0.59))
        self.assertFalse(should_use_proactive_voice(60, random_value=lambda: 0.60))


class ProactiveHandlerTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def test_every_candidate_message_reaches_model_decision(self) -> None:
        decide = AsyncMock(return_value=ProactiveDecision(20, "", False))
        finish = AsyncMock(return_value=True)
        with (
            patch.object(self.app_context, 'settings', proactive_settings()),
            patch.object(self.handlers.triggers, '_generate_proactive_reply', decide),
            patch.object(self.handlers.replies, '_finish_safely', finish),
        ):
            await self.handlers.triggers.handle_proactive_chat(
                AsyncMock(), group_event(1, "第一条值得认真讨论")
            )
            await self.handlers.triggers.handle_proactive_chat(
                AsyncMock(), group_event(2, "第二条也值得认真讨论")
            )

        self.assertEqual(decide.await_count, 2)
        finish.assert_not_awaited()

    async def test_high_interest_reply_can_be_sent_as_voice(self) -> None:
        finish = AsyncMock(return_value=True)
        with (
            patch.object(self.app_context, 'settings', proactive_settings()),
            patch.object(
                self.handlers.triggers,
                '_generate_proactive_reply',
                new=AsyncMock(
                    return_value=ProactiveDecision(96, "这段确实有意思", True)
                ),
            ),
            patch('src.plugins.ai_chat.trigger_service.should_use_proactive_voice', return_value=True),
            patch(
                'src.plugins.ai_chat.trigger_service.synthesize_silk_voice',
                new=AsyncMock(return_value=(b"silk", "这段确实有意思")),
            ),
            patch.object(self.handlers.replies, '_finish_safely', finish),
        ):
            await self.handlers.triggers.handle_proactive_chat(
                AsyncMock(), group_event(3, "这里聊到了一个有趣话题")
            )

        outgoing = finish.await_args.args[1]
        self.assertEqual([segment.type for segment in outgoing], ["record"])
        self.assertFalse(finish.await_args.kwargs["retry_on_timeout"])

    async def test_voice_failure_falls_back_to_text(self) -> None:
        finish = AsyncMock(return_value=True)
        with (
            patch.object(self.app_context, 'settings', proactive_settings()),
            patch.object(
                self.handlers.triggers,
                '_generate_proactive_reply',
                new=AsyncMock(return_value=ProactiveDecision(95, "我也想聊这个", True)),
            ),
            patch('src.plugins.ai_chat.trigger_service.should_use_proactive_voice', return_value=True),
            patch(
                'src.plugins.ai_chat.trigger_service.synthesize_silk_voice',
                new=AsyncMock(side_effect=VoiceError("offline")),
            ),
            patch.object(self.handlers.replies, '_finish_safely', finish),
        ):
            await self.handlers.triggers.handle_proactive_chat(
                AsyncMock(), group_event(4, "继续聊聊这个有趣的话题")
            )

        self.assertIsInstance(finish.await_args.args[1], str)
        self.assertTrue(finish.await_args.kwargs["retry_on_timeout"])


if __name__ == "__main__":
    unittest.main()
