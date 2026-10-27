from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11 import Bot, GroupIncreaseNoticeEvent, Message, MessageSegment

nonebot.init()

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.onebot_v11 import OneBotIngestAdapter
from src.plugins.ai_chat.bridges import BridgePermanentError, BridgeRetryableError
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.group_wiki import WikiLeaseLost, WikiSuperseded
from src.plugins.ai_chat.group_wiki_handlers import GroupWikiHandlers
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.onebot_delivery import OneBotDelivery
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests.test_group_wiki import GroupWikiFixture
from tests.test_routine_tools import event


class GroupWikiNewcomerTests(GroupWikiFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.db_call = DatabaseCalls()
        self.addAsyncCleanup(self.db_call.close)
        self.outbox = self.owned(DeliveryStore(self.db))
        self.app = SimpleNamespace(group_wiki_store=self.store, db_call=self.db_call, logger=Mock(),
            job_worker=Mock(), delivery_store=self.outbox, mirror_state=None, message_ledger=self.ledger,
            bridge_manager=None, settings=replace(settings, admin_user_ids={7}, group_wiki_enabled=True))
        self.services = SimpleNamespace(context=self.app, group_enabled=Mock(return_value=True),
            replies=SimpleNamespace(_sent_message_id=lambda response: response["message_id"]))
        self.handler = GroupWikiHandlers(self.services)
        self.services.wiki = self.handler
        self.delivery_handler = OneBotDelivery(self.services)
        self.bot = Mock(spec=Bot, self_id="301", get_group_member_list=AsyncMock(return_value=[{"user_id": 8}]),
            call_api=AsyncMock(return_value={"online": True}), send_private_msg=AsyncMock(return_value={"message_id": 900}))
        self.enterContext(patch("src.plugins.ai_chat.group_wiki_handlers.get_bot", return_value=self.bot))

    def publish(self, **item_overrides):
        message, _ = self.chapter()
        if item_overrides.get("category") == "expertise":
            item_overrides["subject_principal_id"] = message.sender_principal_id
        job = self.job()
        self.store.publish(job, [self.entry(message, **item_overrides)], now=100)
        self.jobs.mark_succeeded(job.job_id, job.lease_owner, now=100)

    def join(self, **overrides):
        options = dict(bot_id="301", user_id="8", noticed_at=int(time.time()))
        options.update(overrides)
        return self.store.enqueue_newcomer(self.scope, **options)

    def ready(self):
        self.publish()
        self.enable(newcomer_enabled=True, expected_version=1)
        self.join()
        return self.jobs.claim_due("newcomer-worker")[0]

    async def cycle(self, bots=None):
        with patch("src.plugins.ai_chat.onebot_delivery.get_bots", return_value=bots or {"301": self.bot}), patch(
            "src.plugins.ai_chat.onebot_delivery.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.delivery_handler._delivery_loop()

    async def test_opt_in_required_and_notice_idempotent(self):
        self.publish()
        self.assertIsNone(self.join())
        self.enable(newcomer_enabled=True, expected_version=1)
        notice = GroupIncreaseNoticeEvent(time=int(time.time()), self_id=301, post_type="notice",
            notice_type="group_increase", sub_type="approve", group_id=100, operator_id=7, user_id=8)
        await self.handler.newcomer_notice(notice)
        await self.handler.newcomer_notice(notice)
        jobs = self.jobs.claim_due("newcomer-worker")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].payload["user_id"], "8")
        for values in ({"bot_id": "999"}, {"user_id": "301"}, {"user_id": "0"}, {"noticed_at": 100}):
            self.assertIsNone(self.join(**values))
        self.assertIsNone(self.store.enqueue_newcomer(self.other, bot_id="301", user_id="8", noticed_at=notice.time))

    async def test_takeover_queues_once_and_delivery_does_not_pollute_private_memory(self):
        job = self.ready()
        first = await self.handler.handle_newcomer_job(job)
        self.assertTrue(self.jobs.defer(job, delay_seconds=0))
        replacement = self.jobs.claim_due("replacement")[0]
        self.assertEqual(await self.handler.handle_newcomer_job(replacement), first)
        other = Mock(spec=Bot, self_id="999", call_api=AsyncMock(), send_private_msg=AsyncMock())
        await self.cycle({"999": other, "301": self.bot})
        record = self.outbox.get(first["delivery_id"])
        self.assertEqual(record.status, "committed")
        self.assertEqual(self.ledger.visible_message_count_after(record.target_scope, 0), 0)
        self.bot.send_private_msg.assert_awaited_once()
        other.call_api.assert_not_awaited()
        self.bot.get_group_member_list.assert_awaited_with(group_id=100, no_cache=True)

    async def test_missing_original_bot_defers_and_never_uses_another_account(self):
        job = self.ready()
        with patch("src.plugins.ai_chat.group_wiki_handlers.get_bot", side_effect=KeyError):
            with self.assertRaises(JobDeferred):
                await self.handler.handle_newcomer_job(job)
        record = self.store.queue_newcomer_delivery(job, self.outbox)
        other = Mock(spec=Bot, self_id="999", call_api=AsyncMock(), send_private_msg=AsyncMock())
        await self.cycle({"999": other})
        pending = self.outbox.get(record.delivery_id)
        self.assertEqual((pending.status, pending.attempts), ("pending", 0))
        other.call_api.assert_not_awaited()

    async def test_missing_receipt_is_ambiguous_and_not_retried(self):
        job = self.ready()
        record = self.store.queue_newcomer_delivery(job, self.outbox)
        self.bot.send_private_msg.return_value = {"message_id": None}
        await self.cycle()
        self.assertEqual(self.outbox.get(record.delivery_id).status, "ambiguous")
        await self.cycle()
        self.bot.send_private_msg.assert_awaited_once()

    async def test_membership_unknown_defers_and_leaving_skips(self):
        job = self.ready()
        self.bot.get_group_member_list.return_value = {"user_id": 8}
        with self.assertRaises(JobDeferred):
            await self.handler.handle_newcomer_job(job)
        self.bot.get_group_member_list.return_value = []
        self.assertEqual((await self.handler.handle_newcomer_job(job))["status"], "skipped")
        self.assertEqual(self.outbox.claim_due(), [])

    async def test_member_leaves_after_enqueue_and_source_group_disabled(self):
        job = self.ready()
        result = await self.handler.handle_newcomer_job(job)
        self.bot.get_group_member_list.return_value = []
        await self.cycle()
        self.assertEqual(self.outbox.get(result["delivery_id"]).status, "failed")
        self.bot.send_private_msg.assert_not_awaited()
        self.services.group_enabled.return_value = False
        with self.assertRaises(BridgePermanentError):
            await self.handler.check_member(self.bot, job.payload)

    async def test_disable_cancels_pending_and_stale_worker_cannot_queue(self):
        job = self.ready()
        record = self.store.queue_newcomer_delivery(job, self.outbox)
        self.enable(expected_version=2, newcomer_enabled=False)
        self.assertEqual(self.outbox.get(record.delivery_id).status, "cancelled")
        with self.assertRaises(WikiLeaseLost):
            self.store.queue_newcomer_delivery(job, self.outbox)
        with self.assertRaises(WikiSuperseded):
            self.store.newcomer_delivery(record)

    async def test_hidden_evidence_and_expiration_prevent_delivery(self):
        job = self.ready()
        record = self.store.queue_newcomer_delivery(job, self.outbox)
        with self.assertRaises(WikiSuperseded):
            self.store.newcomer_delivery(record, now=job.payload["expires_at"])
        self.ledger.hide_history(self.scope)
        await self.cycle()
        self.assertEqual(self.outbox.get(record.delivery_id).status, "failed")
        self.bot.send_private_msg.assert_not_awaited()

    async def test_changed_settings_during_membership_lookup_rejects_send(self):
        job = self.ready()
        record = self.store.queue_newcomer_delivery(job, self.outbox)

        async def member_list(**kwargs):
            await self.db_call(self.enable, expected_version=2, newcomer_enabled=False)
            return [{"user_id": 8}]

        self.bot.get_group_member_list.side_effect = member_list
        await self.cycle()
        self.assertEqual(self.outbox.get(record.delivery_id).status, "failed")
        self.bot.send_private_msg.assert_not_awaited()

    async def test_forged_delivery_and_wrong_bot_rejected(self):
        job = self.ready()
        record = self.store.queue_newcomer_delivery(job, self.outbox)
        for forged in (replace(record, target_native_conversation_id="9"), replace(record, source_scope_key=self.other.key),
                       replace(record, turn_id=1), replace(record, idempotency_key="wiki-newcomer:99999")):
            with self.assertRaises(WikiSuperseded):
                self.store.newcomer_delivery(forged)
        with self.assertRaises(BridgePermanentError):
            await self.handler.validate_newcomer_delivery(SimpleNamespace(self_id="999"), record)

    async def test_private_send_is_literal_and_all_echo_paths_skip_ledger(self):
        job = self.ready()
        record = self.store.queue_newcomer_delivery(job, self.outbox)
        adapter = OneBotIngestAdapter(group_enabled=lambda _: True, canonical_scope=scope_from_event,
            image_cache_key=lambda _: "", voice_cache_key=lambda _: "", ocr_max_images=1, logger=Mock(),
            message_ledger=self.ledger, delivery_store=self.outbox, db_call=self.db_call)
        self.outbox.begin_direct_attempt(record.delivery_id)
        for native_id, delayed in ((900, False), (901, True)):
            if delayed:
                self.outbox._connection.execute("UPDATE deliveries SET status='ambiguous',updated_at=100 WHERE delivery_id=?",
                                                (record.delivery_id,))
                self.outbox._connection.commit()
            echo = event(actor=301, group=None)
            echo.target_id = 8
            echo.message_id = native_id
            echo.time = int(time.time()) + (1000 if delayed else 0)
            echo.message = Message(MessageSegment.text(self.store.newcomer_text(job.scope_key, job.payload["items"])))
            echo.original_message = echo.message
            await adapter.ingest(echo)
            self.assertEqual(self.ledger.visible_message_count_after(record.target_scope, 0), 0)
        self.assertFalse(self.outbox.is_group_brief_echo(self.other, record.body, native_message_id=901))

    async def test_admin_commands_and_expertise_not_in_newcomer_brief(self):
        self.publish(category="expertise")
        normal = event(actor=8, group=100)
        self.assertIn("只有本群管理员", await self.handler.command(normal, "新人简报 开启"))
        self.assertIn("已开启新人", await self.handler.command(event(actor=7, group=100), "新人简报 开启"))
        self.assertIsNone(self.join())
        self.assertIn("已关闭新人", await self.handler.command(event(actor=7, group=100), "新人简报 关闭"))

    async def test_membership_failure_is_retryable_and_cancellation_propagates(self):
        job = self.ready()
        self.bot.get_group_member_list.side_effect = TimeoutError
        with self.assertRaises(BridgeRetryableError):
            await self.handler.check_member(self.bot, job.payload)
        self.bot.get_group_member_list.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.handler.check_member(self.bot, job.payload)
