from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11 import Bot

nonebot.init()

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.onebot_availability import delivery_blocker, file_delivery_blocker
from src.plugins.ai_chat.onebot_delivery import OneBotDelivery


class OneBotDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = DeliveryStore(":memory:")
        self.addCleanup(self.store.close)
        self.scope = ConversationScope("onebot-v11", "group", "100")
        self.bot = Mock(spec=Bot, self_id="123")
        self.bot.call_api = AsyncMock(return_value={"online": True, "good": True})
        self.bot.send_group_msg = AsyncMock(return_value={"message_id": 9001})
        self.bot.send_private_msg = AsyncMock(return_value={"message_id": 9002})
        self.context = SimpleNamespace(
            delivery_store=self.store, settings=SimpleNamespace(outbox_check_seconds=1),
            subagent_store=Mock(), logger=Mock(), mirror_state=None,
            message_ledger=None, bridge_manager=None,
        )
        self.context.subagent_store.control.return_value = {"dispatch": {"bot_id": "123"}}
        self.services = SimpleNamespace(
            context=self.context, group_enabled=lambda _: True,
            replies=SimpleNamespace(_sent_message_id=lambda response: response["message_id"]),
        )
        self.delivery = OneBotDelivery(self.services)

    def enqueue(self, key="subagent-final:1:1", scope=None):
        record, _ = self.store.enqueue(
            idempotency_key=key, source_scope_key=self.scope.key,
            target_scope=scope or self.scope, body=MessageBody((TextNode(0, "done"),)),
        )
        return record

    async def cycle(self, bots=None):
        with patch("src.plugins.ai_chat.onebot_delivery.get_bots", return_value=(
                {"123": self.bot} if bots is None else bots)), patch(
                "src.plugins.ai_chat.onebot_delivery.asyncio.sleep",
                new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await self.delivery._delivery_loop()

    async def test_connected_but_offline_leaves_final_pending_without_attempt(self):
        record = self.enqueue()
        self.bot.call_api.return_value = {"online": False}
        await self.cycle()
        pending = self.store.get(record.delivery_id)
        self.assertEqual((pending.status, pending.attempts), ("pending", 0))
        self.assertIsNone(pending.lease_until)
        self.assertIn("消息未发送", pending.last_error)
        self.assertGreater(pending.next_attempt_at, record.next_attempt_at)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_imessage_poll_worker_sleeps_after_success_and_failure_and_cancels(self):
        from src.plugins.ai_chat.bridges import BridgeRetryableError
        manager = SimpleNamespace(imessage=object(), imessage_poll_enabled=True,
            sync_imessage_once=AsyncMock(return_value=1))
        self.context.bridge_manager = manager
        self.context.settings.imessage_poll_seconds = 5
        for failure in (None, BridgeRetryableError("BlueBubbles HTTP 503")):
            manager.sync_imessage_once.side_effect = failure
            with patch("src.plugins.ai_chat.onebot_delivery.asyncio.sleep", side_effect=asyncio.CancelledError) as sleep:
                with self.assertRaises(asyncio.CancelledError):
                    await self.delivery._imessage_sync_loop()
                sleep.assert_called_once_with(5)
        manager.imessage_poll_enabled = False
        manager.sync_imessage_once.reset_mock()
        await self.delivery._imessage_sync_loop()
        manager.sync_imessage_once.assert_not_awaited()

    async def test_unknown_status_or_api_error_does_not_send(self):
        for index, status in enumerate((None, {}, {"online": "true"},
                {"online": True, "good": False}, TimeoutError("unavailable"))):
            with self.subTest(status=status):
                record = self.enqueue(f"subagent-final:{index + 1}:1")
                self.bot.call_api.side_effect = status if isinstance(status, Exception) else None
                self.bot.call_api.return_value = status
                await self.cycle()
                pending = self.store.get(record.delivery_id)
                self.assertEqual((pending.status, pending.attempts), ("pending", 0))
        self.bot.send_group_msg.assert_not_awaited()

    async def test_online_group_and_private_delivery_checks_account_once_per_batch(self):
        group = self.enqueue()
        private = self.enqueue("turn:2:1", ConversationScope("onebot-v11", "private", "456"))
        await self.cycle()
        self.bot.call_api.assert_awaited_once_with("get_status")
        self.assertEqual(self.store.get(group.delivery_id).native_message_id, "9001")
        self.assertEqual(self.store.get(private.delivery_id).native_message_id, "9002")
        self.assertEqual(self.store.get(group.delivery_id).status, "committed")

    async def test_original_account_is_not_replaced_with_another_online_account(self):
        record = self.enqueue()
        other = Mock(spec=Bot, self_id="456", call_api=AsyncMock())
        await self.cycle({"456": other})
        pending = self.store.get(record.delivery_id)
        self.assertEqual((pending.status, pending.attempts), ("pending", 0))
        other.call_api.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_account_status_is_rechecked_after_next_due_time(self):
        record = self.enqueue()
        self.bot.call_api.return_value = {"online": False}
        await self.cycle()
        pending = self.store.get(record.delivery_id)
        self.bot.call_api.return_value = {"online": True}
        with patch("src.plugins.ai_chat.delivery.time.time", return_value=pending.next_attempt_at):
            await self.cycle()
        delivered = self.store.get(record.delivery_id)
        self.assertEqual((delivered.status, delivered.attempts), ("committed", 1))
        self.assertEqual(self.bot.call_api.await_count, 2)
        self.bot.send_group_msg.assert_awaited_once()

    async def test_ambiguous_final_is_not_retried_after_login(self):
        record = self.enqueue()
        self.store.begin_direct_attempt(record.delivery_id)
        self.store.mark_ambiguous(record.delivery_id, "receipt lost")
        await self.cycle()
        self.assertEqual(self.store.get(record.delivery_id).status, "ambiguous")
        self.bot.call_api.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_disabled_bridge_cannot_flush_old_mirror_into_qq(self):
        record = self.enqueue("mirror:old-route:1:onebot-v11:group:100")
        await self.cycle()
        failed = self.store.get(record.delivery_id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("消息互通已关闭", failed.last_error)
        self.bot.send_group_msg.assert_not_awaited()
        self.bot.call_api.assert_not_awaited()

    async def test_file_guard_keeps_file_specific_notice(self):
        self.bot.call_api.return_value = {"online": False}
        self.assertIn("文件未上传", await file_delivery_blocker(self.bot))

    async def test_status_probe_does_not_swallow_shutdown(self):
        self.bot.call_api.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await delivery_blocker(self.bot)
