from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import json
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.exception import FinishedException


from src.bot_storage import DatabaseError
from src.plugins.ai_chat.ai_tools import (
    CONTEXT_EXPAND_TOOL_NAME,
    CONTEXT_SEARCH_TOOL_NAME,
)
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.agent_loop import AgentLoopEvent
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.onebot_codec import record_onebot_event, scope_from_event
from src.plugins.ai_chat.turn_journal import ReplayBundle, TurnJournal


from tests.handler_fixture import HandlerTestCaseMixin
from src.plugins.ai_chat.agent_loop import DeepSeekConfigError
from src.plugins.ai_chat.application import ChatTurnResult as TrackedAIResult
from src.plugins.ai_chat.onebot_codec import decode_onebot_message
from src.plugins.ai_chat.stickers import ai_reply_message


def group_event(
    message_id: int,
    message: Message,
    *,
    user_id: int = 7,
) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=message_id,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={
            "user_id": user_id,
            "nickname": "Alice",
            "card": "",
            "role": "member",
        },
        group_id=100,
    )


class FakeMatcher:
    sent: list[Message] = []

    @classmethod
    async def send(cls, message):
        cls.sent.append(message)
        return {"message_id": 19 + len(cls.sent)}


class TimeoutMatcher:
    @classmethod
    async def send(cls, message):
        del message
        raise ActionFailed(
            status="failed",
            retcode=1200,
            data=None,
            message="Timeout: NodeIKernelMsgService/sendMsg",
            wording="Timeout: NodeIKernelMsgService/sendMsg",
            echo="1",
        )


class TurnContinuityTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = MessageLedger(postgres_for_test(self))
        self.journal = TurnJournal(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.journal.close)
        FakeMatcher.sent = []

    async def test_model_failures_deliver_notice_but_persist_failed_turn(self) -> None:
        cases = [
            (DeepSeekConfigError("bad config"), "model_config"),
            (DatabaseError("storage unavailable"), "database"),
            (RuntimeError("provider disconnected"), "model_request"),
            (ValueError("unexpected data"), "internal"),
            (None, "empty_response"),
        ]
        for index, (error, code) in enumerate(cases, start=80):
            with self.subTest(code=code):
                event = group_event(index, Message("answer please"))
                record_onebot_event(self.ledger, event)
                with (
                    patch.object(self.app_context, 'message_ledger', self.ledger),
                    patch.object(self.app_context, 'turn_journal', self.journal),
                    patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=AsyncMock(side_effect=error, return_value="")),
                    patch.object(self.app_context.memory, "append_turn") as remember,
                    patch.object(self.app_context.logger, "exception"),
                ):
                    result = await self.handlers.chat._run_tracked_ai(
                        AsyncMock(), event, "answer please", available_image_sources=[],
                    )
                self.assertIsNotNone(result)
                self.assertTrue(result.reply)
                self.assertEqual(result.status, "crashed")
                self.assertEqual(result.error_code, code)
                turn = self.journal.get_turn_by_id(result.turn_id)
                self.assertEqual(turn.status, "crashed")
                self.assertEqual(turn.final_text, result.reply)
                self.assertEqual(self.journal.archive_payload(result.turn_id)["failure"]["code"], code)
                remember.assert_not_called()

    async def test_failure_notice_text_is_not_used_to_classify_successful_answer(self) -> None:
        event = group_event(86, Message("quote an error message"))
        record_onebot_event(self.ledger, event)
        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch('src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools', new=AsyncMock(return_value="模型没有返回内容。")),
            patch.object(self.app_context.memory, "append_turn"),
        ):
            result = await self.handlers.chat._run_tracked_ai(
                AsyncMock(), event, "quote an error message", available_image_sources=[],
            )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.error_code, "")


    async def test_confirmed_final_send_links_back_to_turn(self) -> None:
        event = group_event(10, Message("build it"))
        trigger = record_onebot_event(self.ledger, event)
        scope = scope_from_event(event)
        turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=trigger.canonical_message_id,
            objective="build it",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        self.journal.finish_turn(turn.turn_id, status="succeeded")

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(
                self.handlers.chat,
                '_run_tracked_ai',
                new=AsyncMock(
                    return_value=TrackedAIResult(
                        reply="done",
                        turn_id=turn.turn_id,
                    )
                ),
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    FakeMatcher,
                    AsyncMock(),
                    event,
                    "build it",
                    label="test",
                )

        canonical_reply = self.ledger.canonical_id_for_native(scope, 20)
        self.assertIsNotNone(canonical_reply)
        linked = self.journal.find_turn_for_reply(scope, canonical_reply)  # type: ignore[arg-type]
        self.assertEqual(linked.turn_id, turn.turn_id)  # type: ignore[union-attr]
        self.assertEqual(FakeMatcher.sent[0][0].type, "reply")
        send_events = [
            event
            for event in self.journal.events_for_turn(turn.turn_id)
            if event.tool_name == "reply_send"
        ]
        self.assertEqual(
            [event.state for event in send_events],
            ["started", "committed"],
        )
        stored_turn = self.journal.get_turn_by_id(turn.turn_id)
        self.assertEqual(stored_turn.tool_call_count, 0)  # type: ignore[union-attr]

    async def test_final_send_timeout_is_journaled_as_outcome_unknown(self) -> None:
        event = group_event(10, Message("build it"))
        trigger = record_onebot_event(self.ledger, event)
        scope = scope_from_event(event)
        turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=trigger.canonical_message_id,
            objective="build it",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        self.journal.finish_turn(turn.turn_id, status="succeeded")

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(
                self.handlers.chat,
                '_run_tracked_ai',
                new=AsyncMock(
                    return_value=TrackedAIResult(
                        reply="done",
                        turn_id=turn.turn_id,
                    )
                ),
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    TimeoutMatcher,
                    AsyncMock(),
                    event,
                    "build it",
                    label="test",
                    retry_on_timeout=False,
                )

        send_events = [
            item
            for item in self.journal.events_for_turn(turn.turn_id)
            if item.tool_name == "reply_send"
        ]
        self.assertEqual(
            [item.state for item in send_events],
            ["started", "outcome-unknown"],
        )

    async def test_final_reply_is_split_without_requoting_every_chunk(self) -> None:
        event = group_event(10, Message("explain"))
        bot = AsyncMock()
        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(
                self.handlers.chat,
                '_run_tracked_ai',
                new=AsyncMock(
                    return_value=TrackedAIResult(
                        reply="第一段\n\n第二段",
                        turn_id=None,
                    )
                ),
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    FakeMatcher,
                    bot,
                    event,
                    "explain",
                    label="test",
                )

        self.assertEqual(len(FakeMatcher.sent), 2)
        self.assertEqual(FakeMatcher.sent[0][0].type, "reply")
        self.assertEqual(FakeMatcher.sent[0][1].type, "at")
        self.assertTrue(all(segment.type != "reply" for segment in FakeMatcher.sent[1]))
        self.assertEqual(FakeMatcher.sent[1].extract_plain_text(), "第二段")

    async def test_final_reply_resolves_model_mention_to_onebot_at(self) -> None:
        event = group_event(10, Message("叫 Bob 来看"))
        principal_id = self.ledger.ensure_principal_identity(
            "onebot-v11",
            88,
            "Bob",
        )
        bot = AsyncMock()
        bot.get_group_member_list.return_value = [
            {"user_id": 7, "nickname": "Alice", "role": "member"},
            {"user_id": 88, "card": "Bob", "role": "member"},
            {"user_id": 999, "nickname": "Bot", "role": "member"},
        ]
        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(
                self.handlers.chat,
                '_run_tracked_ai',
                new=AsyncMock(
                    return_value=TrackedAIResult(
                        reply=f"[mention#{principal_id}] 过来看一下",
                        turn_id=None,
                    )
                ),
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    FakeMatcher,
                    bot,
                    event,
                    "叫 Bob 来看",
                    label="test",
                )

        sent = FakeMatcher.sent[0]
        self.assertEqual(
            [segment.type for segment in sent],
            ["reply", "at", "text", "at", "text"],
        )
        self.assertEqual(sent[1].data["qq"], "7")
        self.assertEqual(sent[3].data["qq"], "88")
        self.assertNotIn("mention#", str(sent))

    async def test_native_model_segments_bypass_rich_table_rendering(self) -> None:
        event = group_event(10, Message("叫 Bob 看表格"))
        principal_id = self.ledger.ensure_principal_identity(
            "onebot-v11",
            88,
            "Bob",
        )
        bot = AsyncMock()
        bot.get_group_member_list.return_value = [
            {"user_id": 7, "nickname": "Alice", "role": "member"},
            {"user_id": 88, "card": "Bob", "role": "member"},
            {"user_id": 999, "nickname": "Bot", "role": "member"},
        ]
        renderer = AsyncMock()
        renderer.render.return_value = b"unexpected-image"
        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(self.app_context, 'rich_renderer', renderer),
            patch.object(
                self.handlers.chat,
                '_run_tracked_ai',
                new=AsyncMock(
                    return_value=TrackedAIResult(
                        reply=(
                            "| 成员 | 状态 |\n"
                            "| --- | --- |\n"
                            f"| [mention#{principal_id}] | 请查看 |"
                        ),
                        turn_id=None,
                    )
                ),
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    FakeMatcher,
                    bot,
                    event,
                    "叫 Bob 看表格",
                    label="test",
                )

        renderer.render.assert_not_awaited()
        sent = FakeMatcher.sent[0]
        self.assertTrue(any(segment.type == "at" for segment in sent))
        self.assertTrue(all(segment.type != "image" for segment in sent))
        self.assertNotIn("mention#", str(sent))

    async def test_fenced_code_reply_reaches_rich_image_renderer(self) -> None:
        event = group_event(10, Message("写个代码块"))
        bot = AsyncMock()
        renderer = AsyncMock()
        renderer.render.return_value = b"\x89PNG\r\n\x1a\nrendered-code"
        fenced_code = '```python\nprint("中文")\n```'
        decorated_reply = ai_reply_message(fenced_code, "写个代码块")

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(self.app_context, 'rich_renderer', renderer),
            patch.object(
                self.handlers.chat,
                '_run_tracked_ai',
                new=AsyncMock(
                    return_value=TrackedAIResult(
                        reply=decorated_reply,
                        turn_id=None,
                    )
                ),
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    FakeMatcher,
                    bot,
                    event,
                    "写个代码块",
                    label="test",
                )

        self.assertEqual(decorated_reply, fenced_code)
        renderer.render.assert_awaited_once_with(fenced_code)
        self.assertTrue(
            any(segment.type == "image" for segment in FakeMatcher.sent[0])
        )

    async def test_silence_sends_no_message_and_reacts_to_trigger(self) -> None:
        event = group_event(10, Message("ambient ping"))
        bot = AsyncMock()
        with patch.object(
            self.handlers.chat,
            '_run_tracked_ai',
            new=AsyncMock(
                return_value=TrackedAIResult(
                    reply="[silence:吃瓜]",
                    turn_id=None,
                    status="silence",
                )
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    FakeMatcher,
                    bot,
                    event,
                    "ambient ping",
                    label="test",
                )

        self.assertEqual(FakeMatcher.sent, [])
        calls = bot.call_api.await_args_list
        self.assertTrue(
            any(call.args[0] == "set_msg_emoji_like" and call.kwargs["emoji_id"] == 271 for call in calls)
        )

    async def test_reply_target_injects_previous_turn_and_ambient_delta(self) -> None:
        original_event = group_event(10, Message("build it"))
        trigger = record_onebot_event(self.ledger, original_event)
        scope = scope_from_event(original_event)
        old_turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=trigger.canonical_message_id,
            objective="build it",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        self.journal.record_tool_started(
            old_turn.turn_id,
            1,
            "sandbox_exec",
            {"command": "pytest"},
            ("write:sandbox",),
        )
        self.journal.record_tool_finished(
            old_turn.turn_id,
            1,
            "sandbox_exec",
            "succeeded",
            '{"ok":true,"stdout":"passed"}',
            ("write:sandbox",),
        )
        self.journal.finish_turn(
            old_turn.turn_id,
            status="succeeded",
            final_text="done",
            finished_at=15,
        )

        outbound = self.ledger.record_message(
            scope,
            native_message_id="20",
            sender_native_user_id="999",
            sender_display="机器人",
            body=decode_onebot_message(Message("done")).body,
            occurred_at=16,
            direction="outbound",
            reply_to_native_message_id="10",
        )
        self.journal.link_send(old_turn.turn_id, outbound.canonical_message_id)

        chatter = group_event(25, Message("new information"), user_id=8)
        record_onebot_event(self.ledger, chatter)
        continuation_message = Message(
            [MessageSegment.reply(20), MessageSegment.text("change it")]
        )
        continuation_event = group_event(30, continuation_message)
        continuation_trigger = record_onebot_event(
            self.ledger,
            continuation_event,
        )
        current_turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=(
                continuation_trigger.canonical_message_id
            ),
            objective="change it",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
        ):
            context = self.handlers.chat._current_turn_context(
                continuation_event,
                current_turn.turn_id,
            )

        self.assertIn("reply-targeted continuation", context)
        self.assertIn(old_turn.handle, context)
        self.assertIn("sandbox_exec", context)
        self.assertIn("new information", context)
        self.assertNotIn("QQ 8", context)

    async def test_chat_only_reply_does_not_create_work_fork(self) -> None:
        original_event = group_event(10, Message("hello"))
        trigger = record_onebot_event(self.ledger, original_event)
        scope = scope_from_event(original_event)
        old_turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=trigger.canonical_message_id,
            objective="hello",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        self.journal.finish_turn(
            old_turn.turn_id,
            status="succeeded",
            final_text="hi",
        )
        outbound = self.ledger.record_message(
            scope,
            native_message_id="20",
            sender_native_user_id="999",
            sender_display="机器人",
            body=MessageBody((TextNode(0, "hi"),)),
            occurred_at=20,
            direction="outbound",
        )
        self.journal.link_send(old_turn.turn_id, outbound.canonical_message_id)
        continuation = group_event(
            30,
            Message([MessageSegment.reply(20), MessageSegment.text("and then?")]),
        )
        current_trigger = record_onebot_event(self.ledger, continuation)
        current_turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=current_trigger.canonical_message_id,
            objective="and then?",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
        ):
            context = self.handlers.chat._current_turn_context(
                continuation,
                current_turn.turn_id,
            )

        self.assertNotIn("reply-targeted continuation", context)
        self.assertIsNone(self.journal.fork_parent(scope, current_turn.turn_id))

    async def test_context_expand_tool_reads_visible_turn_handle(self) -> None:
        event = group_event(10, Message("continue"))
        trigger = record_onebot_event(self.ledger, event)
        scope = scope_from_event(event)
        old_turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=trigger.canonical_message_id,
            objective="search docs",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        self.journal.record_tool_started(
            old_turn.turn_id,
            1,
            "web_search",
            {"query": "docs"},
            ("read",),
        )
        self.journal.record_tool_finished(
            old_turn.turn_id,
            1,
            "web_search",
            "succeeded",
            '{"ok":true}',
            ("read",),
        )
        self.journal.finish_turn(old_turn.turn_id, status="succeeded")
        captured = {}

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, kwargs
            names = {tool["function"]["name"] for tool in tools}
            self.assertIn(CONTEXT_EXPAND_TOOL_NAME, names)
            captured.update(
                json.loads(
                    await execute_tool(
                        CONTEXT_EXPAND_TOOL_NAME,
                        {"turn_id": old_turn.turn_ordinal},
                    )
                )
            )
            return "ok"

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch(
                'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
                new=fake_deepseek,
            ),
            patch.object(self.app_context.memory, "append_turn"),
        ):
            await self.handlers.tools._ask_ai(
                AsyncMock(),
                event,
                "continue",
                available_image_sources=[],
            )

        self.assertTrue(captured["ok"])
        self.assertIn("web_search", captured["record"])

    async def test_context_search_and_episode_expand_share_current_scope(self) -> None:
        context = ContextStore(
            postgres_for_test(self),
            input_budget_tokens=1000,
            high_watermark_tokens=500,
            low_watermark_tokens=300,
            compartment_target_tokens=250,
            raw_tail_min_messages=3,
        )
        self.addCleanup(context.close)
        event = group_event(80, Message("find the old project decision"))
        scope = scope_from_event(event)
        for index in range(1, 19):
            self.ledger.record_message(
                scope,
                native_message_id=str(index),
                sender_native_user_id="7",
                sender_display="Alice",
                body=MessageBody(
                    (TextNode(0, f"project decision {index} " + "x" * 180),)
                ),
                occurred_at=index,
            )
        context.build_projection(self.ledger, scope)
        captured: dict[str, object] = {}

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, kwargs
            names = {tool["function"]["name"] for tool in tools}
            self.assertIn(CONTEXT_SEARCH_TOOL_NAME, names)
            search = json.loads(
                await execute_tool(
                    CONTEXT_SEARCH_TOOL_NAME,
                    {"query": "project decision", "limit": 5},
                )
            )
            handle = search["episodes"][0]["handle"]
            expanded = json.loads(
                await execute_tool(CONTEXT_EXPAND_TOOL_NAME, {"target": handle})
            )
            captured.update({"search": search, "expanded": expanded})
            return "ok"

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(self.app_context, 'context_store', context),
            patch(
                'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
                new=fake_deepseek,
            ),
            patch.object(self.app_context.memory, "append_turn"),
        ):
            await self.handlers.tools._ask_ai(
                AsyncMock(),
                event,
                "find the old project decision",
                available_image_sources=[],
            )

        search = captured["search"]
        expanded = captured["expanded"]
        self.assertTrue(search["ok"])  # type: ignore[index]
        self.assertTrue(expanded["ok"])  # type: ignore[index]
        self.assertIn("exact evidence", expanded["record"])  # type: ignore[index]

    async def test_valid_reply_continuation_passes_replay_prefix_to_provider(self) -> None:
        original_event = group_event(10, Message("old task"))
        trigger = record_onebot_event(self.ledger, original_event)
        scope = scope_from_event(original_event)
        old_turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=trigger.canonical_message_id,
            objective="old task",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        self.journal.finish_turn(old_turn.turn_id, status="succeeded")
        outbound = self.ledger.record_message(
            scope,
            native_message_id="20",
            sender_native_user_id="999",
            sender_display="机器人",
            body=decode_onebot_message(Message("old result")).body,
            occurred_at=20,
            direction="outbound",
        )
        self.journal.link_send(old_turn.turn_id, outbound.canonical_message_id)

        event = group_event(
            30,
            Message([MessageSegment.reply(20), MessageSegment.text("continue")]),
        )
        current_trigger = record_onebot_event(self.ledger, event)
        current_turn = self.journal.start_turn(
            scope,
            trigger_canonical_message_id=current_trigger.canonical_message_id,
            objective="continue",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        self.journal.add_fork_edge(
            current_turn.turn_id,
            old_turn.turn_id,
            created_by_principal_id=None,
        )
        replay = ReplayBundle(
            mode="verbatim",
            messages=(
                {"role": "user", "content": "old task"},
                {"role": "assistant", "content": "old result"},
            ),
            digest_prefix="",
            reason="valid",
            turn_ordinals=(old_turn.turn_ordinal,),
            covered_canonical_message_ids=(
                trigger.canonical_message_id,
                outbound.canonical_message_id,
            ),
        )
        captured = {}

        async def fake_deepseek(user_text, history, tools, execute_tool, **kwargs):
            del user_text, tools, execute_tool
            captured["history"] = history
            captured["replay_prefix"] = kwargs["replay_prefix"]
            return "continued"

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch.object(self.app_context, 'context_store', None),
            patch.object(
                self.journal,
                "build_replay",
                return_value=replay,
            ),
            patch(
                'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
                new=fake_deepseek,
            ),
            patch.object(self.app_context.memory, "append_turn"),
        ):
            answer = await self.handlers.tools._ask_ai(
                AsyncMock(),
                event,
                "continue",
                available_image_sources=[],
                available_voice_message_id=0,
                journal_turn_id=current_turn.turn_id,
                selected_model_override="deepseek-chat",
            )

        self.assertTrue(str(answer))
        self.assertEqual(captured["history"], [])
        self.assertEqual(captured["replay_prefix"][0]["content"], "old task")

    async def test_run_tracked_ai_persists_live_loop_events(self) -> None:
        event = group_event(50, Message("run tests"))
        record_onebot_event(self.ledger, event)

        async def fake_deepseek(
            user_text,
            history,
            tools,
            execute_tool,
            **kwargs,
        ) -> str:
            del user_text, history, tools, execute_tool
            trace = kwargs["trace"]
            trace.messages.append({"role": "assistant", "content": "done"})
            sink = kwargs["event_sink"]
            await sink(
                AgentLoopEvent(
                    kind="tool_started",
                    sequence=1,
                    tool_name="sandbox_list",
                    arguments={},
                    state="started",
                )
            )
            await sink(
                AgentLoopEvent(
                    kind="tool_finished",
                    sequence=1,
                    tool_name="sandbox_list",
                    result='{"ok":true}',
                    state="succeeded",
                )
            )
            return "done"

        with (
            patch.object(self.app_context, 'message_ledger', self.ledger),
            patch.object(self.app_context, 'turn_journal', self.journal),
            patch(
                'src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools',
                new=fake_deepseek,
            ),
            patch.object(self.app_context.memory, "append_turn"),
        ):
            result = await self.handlers.chat._run_tracked_ai(
                AsyncMock(),
                event,
                "run tests",
                available_image_sources=[],
            )

        self.assertIsNotNone(result)
        turn = self.journal.get_turn_by_id(result.turn_id)  # type: ignore[union-attr]
        self.assertEqual(turn.status, "succeeded")  # type: ignore[union-attr]
        self.assertEqual(turn.tool_call_count, 1)  # type: ignore[union-attr]
        self.assertEqual(
            turn.profile,  # type: ignore[union-attr]
            self.app_context.model_catalog.default.name,
        )
        self.assertEqual(
            turn.provider,  # type: ignore[union-attr]
            self.app_context.model_catalog.default.provider_identity,
        )
        self.assertEqual(
            turn.model,  # type: ignore[union-attr]
            self.app_context.model_catalog.default.model,
        )
        self.assertEqual(
            [item.state for item in self.journal.events_for_turn(turn.turn_id)],  # type: ignore[union-attr]
            ["started", "succeeded"],
        )


if __name__ == "__main__":
    unittest.main()
