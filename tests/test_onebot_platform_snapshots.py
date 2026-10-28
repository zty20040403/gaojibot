"""Frozen pre-M1 OneBot wire behavior, captured from pre-qq-official (af0fba6).

These tests run the existing reply/durable-delivery/file transports and the real
NoneBot Bot.send dispatcher. Only LLM results, platform I/O and sandbox processes
are fixtures. Compare full final call_api parameters, not a renderer's own output.
Snapshot updates require reviewing intended behavior changes; no auto-update mode.
All numeric identities and media bytes are synthetic fixtures.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.exception import FinishedException

from src.plugins.ai_chat.agent.background import SubAgentDispatcher
from src.plugins.ai_chat.application import ChatTurnResult
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.project_file_transport import deliver_file
from src.plugins.ai_chat.reminders import Reminder
from tests.handler_fixture import HandlerTestCaseMixin
from tests.postgres_fixture import postgres_for_test
from tests import test_project_file_outbox as project_fixture


SNAPSHOTS = json.loads((Path(__file__).parent / "fixtures" / "onebot_platform_snapshots.json").read_text())
NOW = 1700000000


def event(*, private=False):
    fields = dict(time=NOW, self_id=999, post_type="message", user_id=123,
                  message_id=456, message=Message("测试问题"), original_message=Message("测试问题"),
                  raw_message="测试问题", font=0,
                  sender={"user_id": 123, "nickname": "TEST_REQUESTER", "card": "", "role": "member"})
    if private:
        return PrivateMessageEvent(**fields, message_type="private", sub_type="friend")
    return GroupMessageEvent(**fields, message_type="group", sub_type="normal", group_id=789)


def wire(value):
    if isinstance(value, MessageSegment):
        return {"type": value.type, "data": wire(value.data)}
    if isinstance(value, (Message, list, tuple)):
        return [wire(item) for item in value]
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    return value


def assert_snapshot(case, name, calls):
    case.assertEqual(wire(calls), SNAPSHOTS["cases"][name], name)


class OneBotPlatformSnapshots(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("time.time", return_value=NOW))
        self.app_context.settings = replace(self.app_context.settings, stream_enabled=False, reply_chunk_delay_seconds=0)
        self.app_context.message_ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.app_context.message_ledger.close)
        self.app_context.delivery_store = DeliveryStore(postgres_for_test(self))
        self.addCleanup(self.app_context.delivery_store.close)
        self.app_context.mirror_state = None
        self.calls = []
        self.bot = Bot(Mock(), "999")
        self.bot.call_api = AsyncMock(side_effect=self.api)
        self.handlers.group_enabled = lambda group_id: group_id == 789

    async def api(self, api_name, **params):
        self.calls.append({"api": api_name, "params": wire(params)})
        if api_name == "get_group_member_list":
            return [{"user_id": 88, "nickname": "TEST_OTHER", "role": "member"}]
        if api_name == "get_status":
            return {"online": True, "good": True}
        if api_name in {"send_msg", "send_group_msg", "send_private_msg"}:
            return {"message_id": 9000 + len(self.calls)}
        if api_name == "set_msg_emoji_like":
            return {}
        raise AssertionError(f"Unexpected outbound API {api_name}")

    async def finish(self, reply, *, incoming=None):
        incoming = incoming or event()
        # The model is fixed, but reply planning, mention resolution, outbox
        # enqueue/receipts, matcher send and NoneBot SDK dispatch remain real.
        self.handlers.chat._run_tracked_ai = AsyncMock(return_value=ChatTurnResult(reply, turn_id=None))
        matcher = SimpleNamespace(send=lambda message: self.bot.send(incoming, message))
        with self.assertRaises(FinishedException):
            await self.handlers.replies._finish_tracked_ai(matcher, self.bot, incoming, "测试问题", label="snapshot")
        self.assertTrue(self.calls)

    async def test_ordinary_group_reply(self):
        await self.finish("你好，测试完成。")
        assert_snapshot(self, "ordinary_group", self.calls)

    async def test_private_reply(self):
        await self.finish("这是私聊回复。", incoming=event(private=True))
        assert_snapshot(self, "private", self.calls)

    async def test_quote_resolves_ledger_message_in_current_group(self):
        target = self.app_context.message_ledger.record_message(
            scope_from_event(event()), native_message_id="777", sender_native_user_id="88",
            sender_display="TEST_OTHER", body=MessageBody((TextNode(0, "被引用原文"),)), occurred_at=NOW - 10,
        )
        await self.finish(f"[reply#{target.canonical_message_id}] 对这条消息的回复。")
        assert_snapshot(self, "quote", self.calls)

    async def test_model_mention_uses_current_group_roster(self):
        principal = self.app_context.message_ledger.ensure_principal_identity("onebot-v11", "88", "TEST_OTHER")
        await self.finish(f"[mention#{principal}] 看这里。")
        assert_snapshot(self, "mention", self.calls)

    async def test_image_reply_keeps_native_segment(self):
        await self.finish(Message([MessageSegment.image(b"TEST_IMAGE_BYTES")]))
        assert_snapshot(self, "image", self.calls)

    async def test_long_reply_keeps_chunk_order_and_first_quote(self):
        await self.finish("\n\n".join(f"第 {index} 段：" + "测试长回复。" * 8 for index in range(1, 13)))
        assert_snapshot(self, "long_reply", self.calls)

    async def test_subagent_final_uses_durable_enqueue_and_original_reply(self):
        incoming = event()
        scope = scope_from_event(incoming)
        task = SimpleNamespace(task_id=1, handle="task#1", status="completed", project_id=None,
                               scope_key=scope.key, requester_user_id=123, trigger_message_id=None,
                               result={"answer": "后台任务已完成。"}, last_error="")
        store = Mock()
        store.routine_parent.return_value = None
        store.control.return_value = {"revision": 1, "dispatch": {"bot_id": "999", "event": incoming.model_dump(mode="json")}}
        store.get.return_value = task
        store.runs.return_value = []
        store.external_calls.return_value = []
        store.revision_checkpoints.return_value = []
        self.app_context.subagent_store = store
        dispatcher = SubAgentDispatcher(self.handlers)
        delivery = dispatcher.enqueue_final(1)
        self.assertIsNotNone(delivery)
        claimed = self.app_context.delivery_store.claim_due()[0]
        await self.handlers.delivery._deliver_onebot_outbox(self.bot, claimed)
        self.assertEqual(self.app_context.delivery_store.get(delivery.delivery_id).status, "committed")
        store.mark_final_queued.assert_called_once_with(1, 1)
        assert_snapshot(self, "subagent", self.calls)

    async def test_group_and_private_reminders(self):
        for private in (False, True):
            incoming = event(private=private)
            scope = scope_from_event(incoming)
            reminder = Reminder(1, scope.key, "onebot-v11", scope.kind, scope.native_conversation_id,
                                "123", None, "测试提醒。", NOW, NOW, "sending", 1, NOW - 30, None, "")
            await self.handlers.delivery._deliver_reminder(self.bot, reminder)
        assert_snapshot(self, "reminders", self.calls)


class OneBotProjectFileSnapshots(unittest.IsolatedAsyncioTestCase):
    call = project_fixture.ProjectFileOutboxTests.call
    claim = project_fixture.ProjectFileOutboxTests.claim
    queued = project_fixture.ProjectFileOutboxTests.queued
    receipt = project_fixture.ProjectFileOutboxTests.receipt

    async def asyncSetUp(self):
        self.enterContext(patch("time.time", return_value=NOW))
        # Real PostgreSQL project admission, immutable file staging, access
        # recheck and receipt verification, with existing local sandbox fixtures.
        await project_fixture.ProjectFileOutboxTests.asyncSetUp(self)

    async def test_project_file_upload_and_group_receipt_lookup(self):
        _, delivery = await self.queued(name="snapshot.txt", content="TEST_FILE_BYTES")
        self.root_files.append(self.receipt(delivery))
        calls = []
        original = self.bot.call_api.side_effect

        async def capture(api_name, **params):
            calls.append({"api": api_name, "params": wire(params)})
            return await original(api_name, **params)

        self.bot.call_api.side_effect = capture
        await deliver_file(self.app, self.bot, self.claim())
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status, "committed")
        assert_snapshot(self, "project_file", calls)
