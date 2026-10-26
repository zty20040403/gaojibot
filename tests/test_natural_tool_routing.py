from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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

from src.plugins.ai_chat import (
    _ask_ai,
    _looks_like_secret,
    group_context as current_group_context,
    memory as conversation_memory,
)
from src.plugins.ai_chat.ai_tools import (
    MEMORY_ADD_TOOL_NAME,
    MEMORY_LIST_TOOL_NAME,
    MEMORY_REMOVE_TOOL_NAME,
    MODEL_STATUS_TOOL_NAME,
    QUERY_ALERTS_TOOL_NAME,
    SEND_QQ_FACE_TOOL_NAME,
    SEND_STICKER_TOOL_NAME,
    VIEW_IMAGE_TOOL_NAME,
    VIEW_VIDEO_TOOL_NAME,
)
from src.plugins.ai_chat.adapters import OneBotIngestAdapter
from src.plugins.ai_chat.context_pipeline import TurnContextPlan
from src.plugins.ai_chat.media_library import MediaRecord
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.intent_routing import ToolIntent


def _group_event(user_id: int = 321, group_id: int = 789) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=654,
        message=Message("发个表情包"),
        original_message=Message("发个表情包"),
        raw_message="发个表情包",
        font=0,
        sender={
            "user_id": user_id,
            "nickname": "Alice",
            "card": "",
            "role": "member",
        },
        group_id=group_id,
    )


def _private_image_event(user_id: int = 321) -> PrivateMessageEvent:
    message = Message(
        [
            MessageSegment.image("https://multimedia.nt.qq.com.cn/test.jpg"),
            MessageSegment.text("看看这张图"),
        ]
    )
    return PrivateMessageEvent(
        time=1,
        self_id=999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=655,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": user_id, "nickname": "Alice"},
    )


def _private_video_event(user_id: int = 321) -> PrivateMessageEvent:
    message = Message(
        [
            MessageSegment(
                "video",
                {"url": "https://multimedia.nt.qq.com.cn/test.mp4"},
            ),
            MessageSegment.text("评价一下这个视频"),
        ]
    )
    return PrivateMessageEvent(
        time=1,
        self_id=999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=656,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": user_id, "nickname": "Alice"},
    )


class NaturalToolRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        memory_patcher = patch.object(conversation_memory, "append_turn")
        context_patcher = patch.object(current_group_context, "append")
        memory_patcher.start()
        context_patcher.start()
        self.addCleanup(memory_patcher.stop)
        self.addCleanup(context_patcher.stop)

    async def test_consecutive_requests_are_not_rate_limited(self) -> None:
        ask_deepseek = AsyncMock(return_value="正常回答")
        event = _group_event(user_id=320)

        with patch(
            'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
            new=ask_deepseek,
        ):
            first = await _ask_ai(AsyncMock(), event, "第一个问题")
            second = await _ask_ai(AsyncMock(), event, "紧接着的第二个问题")

        self.assertIn("正常回答", first)
        self.assertIn("正常回答", second)
        self.assertEqual(ask_deepseek.await_count, 2)

    async def test_simple_chat_profile_does_not_take_media_turns(self) -> None:
        import json
        import src.plugins.ai_chat as ai_chat

        catalog = ModelCatalog.from_json(
            json.dumps(
                {
                    "default": "strong",
                    "profiles": {
                        "strong": {
                            "model": "strong-model",
                            "api_key_required": False,
                        },
                        "qwen-local": {
                            "model": "qwen3.8-27b",
                            "api_key_required": False,
                        },
                    },
                }
            ),
            default_profile="strong",
            environ={},
        )
        selected: list[str] = []

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, tools, execute_tool
            selected.append(kwargs["profile"].name)
            return "正常回答"

        with (
            patch.object(ai_chat.app_context, 'model_catalog', catalog),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=fake_deepseek),
        ):
            await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _group_event(),
                "今天吃什么",
                available_image_sources=[],
                selected_profile_override=catalog.resolve("strong"),
                simple_chat_profile="qwen-local",
            )
            await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _group_event(),
                "看看这张图",
                available_image_sources=["https://example.test/image.jpg"],
                selected_profile_override=catalog.resolve("strong"),
                simple_chat_profile="qwen-local",
            )

        self.assertEqual(selected, ["qwen-local", "strong"])

    async def test_group_chat_does_not_send_personal_bot_history_to_model(self) -> None:
        captured_history = None

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            nonlocal captured_history
            del user_text, tools, execute_tool, kwargs
            captured_history = history
            return "基于群聊现场回答"

        with (
            patch(
                'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
                new=fake_deepseek,
            ),
            patch.object(
                conversation_memory,
                "get",
                return_value=[
                    {"role": "user", "content": "个人旧问题"},
                    {"role": "assistant", "content": "个人旧回答"},
                ],
            ),
        ):
            answer = await _ask_ai(
                AsyncMock(),
                _group_event(user_id=320),
                "PostgreSQL 主库怎么选",
            )

        self.assertIn("基于群聊现场回答", answer)
        self.assertEqual(captured_history, [])

    async def test_group_prompt_uses_unranked_chronological_projection(self) -> None:
        captured: dict[str, object] = {}
        ledger = Mock()
        ledger.principal_label_for_native.return_value = None
        context_store = Mock()
        context_store.build_projection.return_value = SimpleNamespace(
            text=(
                "[protected live tail]\n"
                "[msg#11 | Alice] 我看 h310 一直寄\n"
                "[msg#12 | Bob] 今晚吃什么\n"
                "[msg#13 | Alice] 这机器网络还是不稳定"
            )
        )

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, tools, execute_tool
            captured.update(kwargs)
            return "按上面的 h310 现场锐评"

        import src.plugins.ai_chat as ai_chat

        with (
            patch.object(ai_chat.app_context, 'message_ledger', ledger),
            patch.object(ai_chat.app_context, 'context_store', context_store),
            patch.object(ai_chat.app_context, 'pin_store', None),
            patch.object(ai_chat.app_context, 'source_store', None),
            patch.object(ai_chat.handlers.chat, '_group_turn_context_plan', return_value=None),
            patch.object(ai_chat.handlers.commands, '_current_long_term_memory', return_value=""),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=fake_deepseek),
            patch.object(ai_chat.app_context.memory, "append_turn"),
        ):
            answer = await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _group_event(),
                "你锐评一下",
                available_image_sources=[],
            )

        group_prompt = str(captured["group_context"])
        self.assertIn("我看 h310 一直寄", group_prompt)
        self.assertIn("这机器网络还是不稳定", group_prompt)
        self.assertLess(
            group_prompt.index("我看 h310 一直寄"),
            group_prompt.index("这机器网络还是不稳定"),
        )
        self.assertIn("按上面的 h310 现场锐评", answer)

    async def test_chronological_projection_is_written_to_context_debug(self) -> None:
        ledger = Mock()
        ledger.principal_label_for_native.return_value = None
        ledger.canonical_id_for_native.return_value = None
        ledger.get_in_scope.return_value = SimpleNamespace(
            canonical_message_id=11,
            prompt_text="我看 h310 一直寄",
        )
        context_store = Mock()
        context_store.build_projection.return_value = SimpleNamespace(
            text="[protected live tail]\n[msg#11 | Alice] 我看 h310 一直寄",
            token_estimate=77,
            raw_message_ids=(11,),
            compartment_handles=("chapter-1",),
        )
        journal = Mock()
        journal.fork_parent.return_value = None
        plan = TurnContextPlan(
            scope_key="onebot-v11:group:789",
            current_message_id=12,
            current_principal_id=1,
            focus_message_id=None,
            confidence=0.0,
            reason_codes=("standalone_message",),
            related_message_ids=(),
            candidates=(),
            rendered_context="",
            topic_query="你锐评一下",
        )

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, tools, execute_tool, kwargs
            return "按时间线回答"

        import src.plugins.ai_chat as ai_chat

        with (
            patch.object(ai_chat.app_context, 'message_ledger', ledger),
            patch.object(ai_chat.app_context, 'context_store', context_store),
            patch.object(ai_chat.app_context, 'pin_store', None),
            patch.object(ai_chat.app_context, 'source_store', None),
            patch.object(ai_chat.app_context, 'turn_journal', journal),
            patch.object(ai_chat.handlers.chat, '_group_turn_context_plan', return_value=plan),
            patch.object(ai_chat.handlers.commands, '_current_long_term_memory', return_value=""),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=fake_deepseek),
            patch.object(ai_chat.app_context.memory, "append_turn"),
        ):
            answer = await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _group_event(),
                "你锐评一下",
                available_image_sources=[],
                journal_turn_id=44,
            )

        self.assertIn("按时间线回答", answer)
        self.assertEqual(journal.record_context_plan.call_count, 2)
        final_payload = journal.record_context_plan.call_args_list[-1].args[1]
        self.assertEqual(
            final_payload["recall_route"]["mode"],
            "chronological_projection",
        )
        self.assertEqual(final_payload["adaptive_budget"]["used"]["timeline"], 77)
        self.assertEqual(
            [item["handle"] for item in final_payload["recall_candidates"]],
            ["msg#11", "episode#chapter-1"],
        )
        self.assertTrue(final_payload["context_hash"])

    async def test_recent_image_handle_reaches_model_and_view_tool(self) -> None:
        captured: dict[str, object] = {}
        ledger = Mock()
        ledger.principal_label_for_native.return_value = None
        context_store = Mock()
        context_store.build_projection.return_value = SimpleNamespace(
            text=(
                "[protected live tail]\n"
                "[msg#21 | Alice] [image#21.0: 一张待分析图片]"
            )
        )

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, execute_tool
            captured["group_context"] = kwargs["group_context"]
            captured["tool_names"] = {
                tool["function"]["name"] for tool in tools
            }
            return "已看图"

        import src.plugins.ai_chat as ai_chat

        with (
            patch.object(ai_chat.app_context, 'message_ledger', ledger),
            patch.object(ai_chat.app_context, 'context_store', context_store),
            patch.object(ai_chat.app_context, 'pin_store', None),
            patch.object(ai_chat.app_context, 'source_store', None),
            patch.object(ai_chat.app_context, 'vision_worker', object()),
            patch.object(ai_chat.handlers.chat, '_group_turn_context_plan', return_value=None),
            patch.object(ai_chat.handlers.commands, '_current_long_term_memory', return_value=""),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=fake_deepseek),
            patch.object(ai_chat.app_context.memory, "append_turn"),
        ):
            await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _group_event(),
                "锐评下这个",
                available_image_sources=[],
            )

        self.assertIn("[image#21.0", str(captured["group_context"]))
        self.assertIn(VIEW_IMAGE_TOOL_NAME, captured["tool_names"])

    async def test_classified_private_image_selects_vision_tool_and_announces_source(self) -> None:
        captured: dict[str, object] = {}
        classifier = AsyncMock(return_value=ToolIntent(VIEW_IMAGE_TOOL_NAME, "semantic", "cheap"))

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, execute_tool
            captured["tool_names"] = {
                tool["function"]["name"] for tool in tools
            }
            captured.update(kwargs)
            return "已看图"

        import src.plugins.ai_chat as ai_chat

        with (
            patch('src.plugins.ai_chat.tool_executor.classify_tool_intent', new=classifier),
            patch.object(ai_chat.app_context, 'vision_worker', object()),
            patch.object(ai_chat.app_context, 'message_ledger', None),
            patch.object(ai_chat.app_context, 'pin_store', None),
            patch.object(ai_chat.app_context, 'source_store', None),
            patch.object(ai_chat.handlers.commands, '_current_long_term_memory', return_value=""),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=fake_deepseek),
            patch.object(ai_chat.app_context.memory, "append_turn"),
        ):
            answer = await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _private_image_event(),
                "看看这张图",
                available_image_sources=[
                    "https://multimedia.nt.qq.com.cn/test.jpg"
                ],
            )

        self.assertIn("已看图", answer)
        self.assertIn(VIEW_IMAGE_TOOL_NAME, captured["tool_names"])
        self.assertEqual(
            captured["tool_choice"]["function"]["name"],
            VIEW_IMAGE_TOOL_NAME,
        )
        self.assertIn("当前会话可用图片", captured["tool_context"])
        self.assertIn(VIEW_IMAGE_TOOL_NAME, classifier.call_args.kwargs["allowed_tools"])
        self.assertTrue(classifier.call_args.kwargs["media"]["image_attached_now"])

    async def test_private_image_is_recorded_for_follow_up_message(self) -> None:
        recent_images = Mock()
        adapter = OneBotIngestAdapter(
            group_enabled=lambda _group_id: True,
            canonical_scope=Mock(),
            image_cache_key=lambda event: f"private:{event.user_id}",
            voice_cache_key=lambda event: f"private:{event.user_id}",
            ocr_max_images=2,
            logger=Mock(),
            recent_images=recent_images,
        )

        await adapter.observe_group_activity(_private_image_event())

        recent_images.record.assert_called_once_with(
            "private:321",
            ["https://multimedia.nt.qq.com.cn/test.jpg"],
        )

    async def test_classified_private_video_selects_video_tool(self) -> None:
        captured: dict[str, object] = {}
        classifier = AsyncMock(return_value=ToolIntent(VIEW_VIDEO_TOOL_NAME, "semantic", "cheap"))

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, execute_tool
            captured["tool_names"] = {
                tool["function"]["name"] for tool in tools
            }
            captured.update(kwargs)
            return "已经看完了"

        import src.plugins.ai_chat as ai_chat

        with (
            patch('src.plugins.ai_chat.tool_executor.classify_tool_intent', new=classifier),
            patch.object(ai_chat.handlers, 'video_analyzer', object()),
            patch.object(ai_chat.app_context, 'message_ledger', None),
            patch.object(ai_chat.app_context, 'pin_store', None),
            patch.object(ai_chat.app_context, 'source_store', None),
            patch.object(ai_chat.handlers.commands, '_current_long_term_memory', return_value=""),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=fake_deepseek),
            patch.object(ai_chat.app_context.memory, "append_turn"),
        ):
            answer = await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _private_video_event(),
                "评价一下这个视频",
                available_image_sources=[],
            )

        self.assertIn("已经看完了", answer)
        self.assertIn(VIEW_VIDEO_TOOL_NAME, captured["tool_names"])
        self.assertEqual(
            captured["tool_choice"]["function"]["name"],
            VIEW_VIDEO_TOOL_NAME,
        )
        self.assertIn("当前会话可用视频", captured["tool_context"])
        self.assertIn(VIEW_VIDEO_TOOL_NAME, classifier.call_args.kwargs["allowed_tools"])

    async def test_private_video_is_recorded_for_follow_up_message(self) -> None:
        recent_videos = Mock()
        adapter = OneBotIngestAdapter(
            group_enabled=lambda _group_id: True,
            canonical_scope=Mock(),
            image_cache_key=lambda event: f"private:{event.user_id}",
            voice_cache_key=lambda event: f"private:{event.user_id}",
            video_cache_key=lambda event: f"private:{event.user_id}",
            ocr_max_images=2,
            logger=Mock(),
            recent_videos=recent_videos,
        )

        await adapter.observe_group_activity(_private_video_event())

        recent_videos.record.assert_called_once_with("private:321", 656)

    async def test_natural_request_uses_auto_tool_choice(self) -> None:
        captured_tool_names: set[str] = set()

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, execute_tool
            captured_tool_names.update(
                tool["function"]["name"] for tool in tools
            )
            self.assertEqual(kwargs["tool_choice"], "auto")
            return "正常回答"

        with patch(
            'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
            new=fake_deepseek,
        ):
            await _ask_ai(AsyncMock(), _group_event(), "发个表情包")

        self.assertIn(SEND_STICKER_TOOL_NAME, captured_tool_names)
        self.assertIn(SEND_QQ_FACE_TOOL_NAME, captured_tool_names)
        self.assertIn(MEMORY_ADD_TOOL_NAME, captured_tool_names)
        self.assertIn(MEMORY_LIST_TOOL_NAME, captured_tool_names)
        self.assertIn(MEMORY_REMOVE_TOOL_NAME, captured_tool_names)

    async def test_alert_ranking_uses_authoritative_alert_tool(self) -> None:
        captured: dict[str, object] = {}
        classifier = AsyncMock(return_value=ToolIntent(QUERY_ALERTS_TOOL_NAME, "semantic", "cheap"))
        alert_store = Mock()
        alert_store.snapshot.return_value = {
            "timezone": "Asia/Shanghai",
            "range_start": 100,
            "generated_at": 200,
            "summary": {"triggered": 14, "current_active": 8},
            "incidents": [
                {
                    "incident_key": "host:r2s",
                    "severity": "warning",
                    "status": "firing",
                    "event_count": 1,
                    "active_event_count": 1,
                    "summary": "r2s service failed",
                    "last_seen_at": 190,
                },
                {
                    "incident_key": "host:h310",
                    "severity": "critical",
                    "status": "firing",
                    "event_count": 13,
                    "active_event_count": 7,
                    "summary": "h310 unreachable",
                    "last_seen_at": 195,
                },
            ],
        }
        alert_store.rank_incidents.return_value = {
            "range_start": 100,
            "generated_at": 200,
            "items": [
                {
                    "incident_key": "host:h310",
                    "event_count": 13,
                    "active_event_count": 7,
                    "first_seen_at": 101,
                    "last_seen_at": 195,
                },
                {
                    "incident_key": "host:r2s",
                    "event_count": 1,
                    "active_event_count": 1,
                    "first_seen_at": 102,
                    "last_seen_at": 190,
                },
            ],
        }

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history
            captured["tool_names"] = {
                tool["function"]["name"] for tool in tools
            }
            captured["tool_choice"] = kwargs["tool_choice"]
            captured["result"] = await execute_tool(
                QUERY_ALERTS_TOOL_NAME,
                {"days": 7, "limit": 10},
            )
            return "h310 最多"

        import src.plugins.ai_chat as ai_chat

        with (
            patch('src.plugins.ai_chat.tool_executor.classify_tool_intent', new=classifier),
            patch.object(ai_chat.app_context, 'alert_store', alert_store),
            patch.object(ai_chat.handlers.tools, '_alert_tools_allowed', return_value=True),
            patch.object(ai_chat.app_context, 'message_ledger', None),
            patch.object(ai_chat.app_context, 'pin_store', None),
            patch.object(ai_chat.app_context, 'source_store', None),
            patch.object(ai_chat.handlers.commands, '_current_long_term_memory', return_value=""),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=fake_deepseek),
            patch.object(ai_chat.app_context.memory, "append_turn"),
        ):
            answer = await ai_chat.handlers.tools._ask_ai(
                AsyncMock(),
                _group_event(group_id=611798505),
                "告警系统里面谁最多",
                available_image_sources=[],
            )

        payload = str(captured["result"])
        self.assertIn("h310", answer)
        self.assertIn(QUERY_ALERTS_TOOL_NAME, captured["tool_names"])
        self.assertEqual(
            captured["tool_choice"]["function"]["name"],
            QUERY_ALERTS_TOOL_NAME,
        )
        self.assertLess(payload.find("host:h310"), payload.find("host:r2s"))
        alert_store.snapshot.assert_called_once_with(days=7, limit=100)
        alert_store.rank_incidents.assert_called_once_with(days=7, limit=10)
        self.assertIn(QUERY_ALERTS_TOOL_NAME, classifier.call_args.kwargs["allowed_tools"])

    async def test_classifier_auto_does_not_restore_keyword_forcing(self) -> None:
        import src.plugins.ai_chat as ai_chat

        captured: list[dict] = []

        async def answer(*args, **kwargs):
            captured.append(kwargs)
            return "正常回答"

        classifier = AsyncMock(return_value=ToolIntent(reason="timeout"))
        with (
            patch('src.plugins.ai_chat.tool_executor.classify_tool_intent', new=classifier),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=answer),
            patch.object(ai_chat.app_context, 'vision_worker', object()),
            patch.object(ai_chat.handlers, 'video_analyzer', object()),
            patch.object(ai_chat.app_context, 'alert_store', Mock()),
            patch.object(ai_chat.app_context, 'fleet_client', Mock()),
            patch.object(ai_chat.handlers.tools, '_alert_tools_allowed', return_value=True),
            patch.object(ai_chat.handlers.tools, '_fleet_tools_allowed', return_value=True),
        ):
            for event, text in ((_private_image_event(), "这个算法你觉得呢，先不要看图"),
                                (_private_video_event(), "先不分析视频，看看这个算法"),
                                (_group_event(), "解释告警最多是什么意思"),
                                (_group_event(), "现在千问呢")):
                with self.subTest(text=text):
                    await _ask_ai(AsyncMock(), event, text, available_image_sources=["https://example.test/old.jpg"])
                    self.assertEqual(captured[-1]["tool_choice"], "auto")
        self.assertEqual(classifier.await_count, 4)

    async def test_explicit_ocr_bypasses_classifier(self) -> None:
        import src.plugins.ai_chat as ai_chat

        model = AsyncMock(return_value="已看图")
        with (
            patch('src.plugins.ai_chat.tool_executor.classify_tool_intent', new_callable=AsyncMock) as classifier,
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=model),
            patch.object(ai_chat.app_context, 'vision_worker', object()),
        ):
            await _ask_ai(AsyncMock(), _private_image_event(), "看看", force_ocr=True,
                          available_image_sources=["https://example.test/current.jpg"])
        classifier.assert_not_awaited()
        self.assertEqual(model.call_args.kwargs["tool_choice"]["function"]["name"], VIEW_IMAGE_TOOL_NAME)

    async def test_classifier_cannot_enable_unavailable_infrastructure_tools(self) -> None:
        import src.plugins.ai_chat as ai_chat

        classifier = AsyncMock()
        model = AsyncMock(return_value="正常回答")
        with (
            patch('src.plugins.ai_chat.tool_executor.classify_tool_intent', new=classifier),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=model),
            patch.object(ai_chat.app_context, 'alert_store', Mock()),
            patch.object(ai_chat.app_context, 'fleet_client', Mock()),
            patch.object(ai_chat.handlers.tools, '_alert_tools_allowed', return_value=False),
            patch.object(ai_chat.handlers.tools, '_fleet_tools_allowed', return_value=False),
        ):
            for tool in (QUERY_ALERTS_TOOL_NAME, MODEL_STATUS_TOOL_NAME):
                classifier.return_value = ToolIntent(tool, "semantic", "cheap")
                await _ask_ai(AsyncMock(), _group_event(), "现在状态呢", available_image_sources=[])
                self.assertNotIn(tool, classifier.call_args.kwargs["allowed_tools"])
                self.assertNotIn(tool, [item["function"]["name"] for item in model.call_args.args[2]])
                self.assertEqual(model.call_args.kwargs["tool_choice"], "auto")

    def test_classifier_history_and_explicit_reply_are_scoped(self) -> None:
        import src.plugins.ai_chat as ai_chat
        from src.plugins.ai_chat.ledger import MessageLedger
        from src.plugins.ai_chat.onebot_codec import record_onebot_event

        ledger = MessageLedger(":memory:")
        self.addCleanup(ledger.close)

        def record(event, message_id, text):
            event.message_id = message_id
            event.time = message_id
            event.message = event.original_message = Message(text)
            record_onebot_event(ledger, event)

        record(_group_event(), 1, "explicit-old-reference")
        for message_id in range(2, 10):
            record(_group_event(user_id=322), message_id, f"same-group-{message_id}")
        record(_group_event(group_id=790), 10, "other-group-private-facts")
        record(_private_image_event(user_id=322), 11, "other-user-private-facts")
        record(_group_event(), 654, "current-duplicated-message")
        event = _group_event()
        event.original_message = Message([MessageSegment.reply(1), MessageSegment.text("你觉得呢")])
        with patch.object(ai_chat.app_context, "message_ledger", ledger):
            context = ai_chat.handlers.tools._tool_intent_context(event)
        self.assertIn("same-group-9", context)
        self.assertIn("[explicit reply]", context)
        self.assertIn("explicit-old-reference", context)
        self.assertNotIn("other-group-private-facts", context)
        self.assertNotIn("other-user-private-facts", context)
        self.assertNotIn("current-duplicated-message", context)

    def test_secret_like_content_is_not_eligible_for_memory(self) -> None:
        self.assertTrue(_looks_like_secret("API_KEY=secret-value"))
        self.assertTrue(_looks_like_secret("密码：123456"))
        self.assertTrue(_looks_like_secret("sk-" + "x" * 26))
        self.assertFalse(_looks_like_secret("我喜欢写 Python"))

    async def test_sticker_is_sent_only_after_model_tool_call(self) -> None:
        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, tools, kwargs
            await execute_tool(SEND_STICKER_TOOL_NAME, {})
            return ""

        with (
            patch(
                'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
                new=fake_deepseek,
            ),
            patch(
                "src.plugins.ai_chat.tools.stickers.random_sticker_message",
                return_value=MessageSegment.face(14),
            ),
        ):
            answer = await _ask_ai(
                AsyncMock(),
                _group_event(user_id=322),
                "发个表情包",
            )

        self.assertIsInstance(answer, Message)
        self.assertEqual(len(answer), 1)
        self.assertEqual(answer[0].type, "face")

    async def test_send_sticker_query_overrides_stale_media_handle(self) -> None:
        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, tools, kwargs
            await execute_tool(
                SEND_STICKER_TOOL_NAME,
                {"query": "猫娘卖萌", "media_handle": "media#46"},
            )
            return ""

        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "sticker.png"
            image_path.write_bytes(b"fake-png")
            record = MediaRecord(
                media_id=42,
                handle="media#42",
                summary="猫娘开心卖萌",
                description="猫耳少女露出开心表情。",
                extracted_text="",
                emotions=("开心",),
                usage=("卖萌",),
                is_sticker=True,
                safety="safe",
                storage_path=image_path,
                mime_type="image/png",
                score=0.9,
            )
            fake_library = SimpleNamespace(
                search_stickers=AsyncMock(return_value=[record]),
                get_sticker=Mock(),
                mark_sent=Mock(),
            )
            with (
                patch(
                    'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
                    new=fake_deepseek,
                ),
                patch('src.plugins.ai_chat.app_context.media_library', fake_library),
            ):
                answer = await _ask_ai(
                    AsyncMock(),
                    _group_event(user_id=323),
                    "发个猫娘表情",
                )

        self.assertIsInstance(answer, Message)
        self.assertEqual(len(answer), 1)
        self.assertEqual(answer[0].type, "image")
        fake_library.search_stickers.assert_awaited_once_with(
            "猫娘卖萌",
            limit=10,
        )
        fake_library.get_sticker.assert_not_called()
        fake_library.mark_sent.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
