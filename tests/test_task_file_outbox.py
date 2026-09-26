from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.agent.file_outbox import attempt_file
from src.plugins.ai_chat.subagents import SubAgentStore


class TaskFileOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "task.sqlite3"
        self.store = SubAgentStore(self.path)
        self.task = self.store.create_task(scope_key="group:1", conversation_id="group:1:user:2",
            requester_user_id=2, trigger_message_id=None, objective="file", max_parallelism=1, max_steps=2)
        self.artifact = {"name": "result.txt", "size": 4, "snapshot": hashlib.sha256(b"test").hexdigest(),
                         "handle": "sandbox#s123abc/result.txt"}
        self.prepare = AsyncMock(return_value=b"test")
        self.send = AsyncMock(return_value={"ok": True, "file_id": "file-1"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def queue(self):
        return self.store.queue_file(self.task.task_id, self.artifact, "test-result.txt")

    async def attempt(self, delivery=None):
        return await attempt_file(self.store, self.task.task_id, delivery or self.queue(),
                                  prepare=self.prepare, send=self.send)

    async def test_restart_before_upload_recovers_manifest_without_recreating_artifact(self):
        self.queue()
        self.store.close()
        self.store = SubAgentStore(self.path)
        result = await self.attempt(self.store.deliveries(self.task.task_id)[0])
        self.assertTrue(result["ok"])
        self.assertEqual(result["artifact"], self.artifact)
        self.send.assert_awaited_once_with(b"test", "test-result.txt")
        await self.attempt()
        self.assertEqual(self.send.await_count, 1)

    async def test_draft_warning_survives_restart_and_delivery(self):
        self.artifact.update(draft=True, draft_reason="价格未核实")
        self.queue()
        self.store.close()
        self.store = SubAgentStore(self.path)
        result = await self.attempt(self.store.deliveries(self.task.task_id)[0])
        self.assertTrue(result["ok"])
        self.assertTrue(result["draft"])
        self.assertEqual(result["draft_reason"], "价格未核实")
        await self.attempt()
        self.send.assert_awaited_once()

    async def test_crash_after_upload_started_requires_receipt_reconciliation_not_resend(self):
        self.send.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.attempt()
        self.store.close()
        self.store = SubAgentStore(self.path)
        row = self.store.deliveries(self.task.task_id)[0]
        self.assertEqual(row["state"], "sending")
        self.send.side_effect = None
        await self.attempt(row)
        self.assertEqual(self.send.await_count, 1)
        self.store.finish_delivery(self.task.task_id, row["key"], "acknowledged",
            {**row["payload"], "ok": True, "file_id": "reconciled"}, revision=1)
        self.assertTrue((await self.attempt())["ok"])
        self.assertEqual(self.send.await_count, 1)

    async def test_ambiguous_response_does_not_retry(self):
        self.send.side_effect = TimeoutError()
        result = await self.attempt()
        self.assertEqual(result["state"], "unknown")
        await self.attempt()
        self.assertEqual(self.send.await_count, 1)

    async def test_late_upload_reply_settles_after_reconciler_marked_unknown(self):
        for reply, expected in (({"ok": True, "file_id": "late"}, "acknowledged"),
                                ({"ok": False, "not_sent": True, "retryable": True}, "queued")):
            with self.subTest(expected=expected):
                control = self.store.control(self.task.task_id)
                self.store.update_control(self.task.task_id, expected_version=control["version"],
                                          revision=control["revision"] + 1)
                row = self.queue()
                claim = self.store.claim_file(self.task.task_id, row)
                self.store.finish_delivery(self.task.task_id, row["key"], "unknown",
                    {**claim, "state": "unknown", "ok": False}, revision=row["revision"])
                result = self.store.settle_file_attempt(self.task.task_id, row, claim, reply)
                stored = next(item for item in self.store.deliveries(self.task.task_id) if item["revision"] == row["revision"])
                self.assertEqual(result["state"], expected)
                self.assertEqual(stored["state"], expected)

    async def test_stale_reconciliation_cannot_erase_successful_receipt(self):
        row = self.queue()
        claim = self.store.claim_file(self.task.task_id, row)
        self.store.settle_file_attempt(self.task.task_id, row, claim, {"ok": True, "file_id": "confirmed"})
        self.store.finish_delivery(self.task.task_id, row["key"], "unknown",
            {**claim, "state": "unknown", "ok": False}, revision=row["revision"])
        stored = self.store.deliveries(self.task.task_id)[0]
        self.assertEqual(stored["state"], "acknowledged")
        self.assertEqual(stored["payload"]["file_id"], "confirmed")

    async def test_late_sender_failure_cannot_override_reconciled_success(self):
        row = self.queue()
        claim = self.store.claim_file(self.task.task_id, row)
        self.store.finish_delivery(self.task.task_id, row["key"], "acknowledged",
            {**claim, "state": "acknowledged", "ok": True, "file_id": "observed"}, revision=row["revision"])
        result = self.store.settle_file_attempt(self.task.task_id, row, claim, {"ok": False, "error": "late timeout"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "acknowledged")
        self.assertEqual(result["file_id"], "observed")

    async def test_old_attempt_and_old_reconciliation_cannot_clobber_new_attempt(self):
        row = self.queue()
        first = self.store.claim_file(self.task.task_id, row)
        retry = self.store.settle_file_attempt(self.task.task_id, row, first,
            {"ok": False, "not_sent": True, "retryable": True})
        with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=retry["next_attempt_at"] + 1):
            second = self.store.claim_file(self.task.task_id, self.store.deliveries(self.task.task_id)[0])
        self.assertEqual(second["attempts"], 2)
        result = self.store.settle_file_attempt(self.task.task_id, row, first, {"ok": False, "error": "old reply"})
        self.assertEqual(result["state"], "sending")
        self.assertEqual(result["attempts"], 2)
        changed = self.store.finish_delivery(self.task.task_id, row["key"], "unknown",
            {**first, "ok": False}, revision=row["revision"], expected_payload=first)
        self.assertFalse(changed)
        current = self.store.deliveries(self.task.task_id)[0]
        self.assertEqual(current["state"], "sending")
        self.assertEqual(current["payload"]["attempts"], 2)

    async def test_explicit_no_upload_retries_with_backoff_and_keeps_original_manifest(self):
        self.send.return_value = {"ok": False, "not_sent": True, "retryable": True, "error": "rejected"}
        result = await self.attempt()
        self.assertEqual(result["state"], "queued")
        await self.attempt()
        self.assertEqual(self.send.await_count, 1)
        self.send.return_value = {"ok": True, "file_id": "file-2"}
        with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=result["next_attempt_at"] + 1):
            result = await self.attempt()
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["artifact"], self.artifact)

    async def test_missing_snapshot_never_uploads_and_eventually_reports_rejection(self):
        self.prepare.side_effect = FileNotFoundError("missing snapshot")
        result = await self.attempt()
        for _ in range(4):
            with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=result["next_attempt_at"] + 1):
                result = await self.attempt()
        self.assertEqual(result["state"], "rejected")
        self.send.assert_not_awaited()

    async def test_offline_wait_survives_restart_without_spending_retry_budget(self):
        self.queue()
        readiness = AsyncMock(return_value="QQ offline")
        for _ in range(8):
            row = self.store.deliveries(self.task.task_id)[0]
            with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=row["payload"]["next_attempt_at"] + 1):
                result = await attempt_file(self.store, self.task.task_id, row,
                    prepare=self.prepare, send=self.send, readiness=readiness)
            self.assertEqual(result["state"], "queued")
            self.assertEqual(result["attempts"], 0)
            self.assertEqual(result["artifact"], self.artifact)
            self.store.close()
            self.store = SubAgentStore(self.path)
        self.prepare.assert_not_awaited()
        self.send.assert_not_awaited()
        self.assertEqual(result["availability_checks"], 8)
        readiness.return_value = None
        with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=result["next_attempt_at"] + 1):
            result = await attempt_file(self.store, self.task.task_id, self.store.deliveries(self.task.task_id)[0],
                prepare=self.prepare, send=self.send, readiness=readiness)
        self.assertTrue(result["ok"])
        self.assertNotIn("blocked_reason", result)
        self.assertFalse(result["not_sent"])
        self.assertEqual(result["attempts"], 1)
        self.send.assert_awaited_once()

    async def test_readiness_backoff_and_stale_wait_cannot_clobber_claim(self):
        row = self.queue()
        blocked = self.store.defer_file_availability(self.task.task_id, row, "offline")
        ready = AsyncMock(return_value=None)
        await attempt_file(self.store, self.task.task_id, self.store.deliveries(self.task.task_id)[0],
            prepare=self.prepare, send=self.send, readiness=ready)
        ready.assert_not_awaited()
        stale = self.store.deliveries(self.task.task_id)[0]
        with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=blocked["next_attempt_at"] + 1):
            claim = self.store.claim_file(self.task.task_id, stale)
        result = self.store.defer_file_availability(self.task.task_id, stale, "late offline reply")
        self.assertEqual(result["state"], "sending")
        self.assertEqual(result["attempts"], claim["attempts"])
        self.assertNotIn("blocked_reason", result)

    async def test_offline_wait_respects_revision_and_cancellation_fences(self):
        old = self.queue()
        self.store.update_control(self.task.task_id, expected_version=0, revision=2)
        self.store.defer_file_availability(self.task.task_id, old, "offline")
        self.assertEqual(self.store.deliveries(self.task.task_id)[0]["payload"], old["payload"])
        row = self.queue()
        self.store.request_cancel(self.task.task_id)
        self.store.defer_file_availability(self.task.task_id, row, "offline")
        current = next(item for item in self.store.deliveries(self.task.task_id) if item["revision"] == 2)
        self.assertEqual(current["payload"], row["payload"])

    async def test_revision_and_cancellation_fence_stale_dispatch(self):
        row = self.queue()
        self.store.update_control(self.task.task_id, expected_version=0, revision=2)
        await self.attempt(row)
        self.send.assert_not_awaited()
        row = self.queue()
        self.store.request_cancel(self.task.task_id)
        await self.attempt(row)
        self.send.assert_not_awaited()

    async def test_concurrent_dispatch_claims_only_one_upload(self):
        row = self.queue()
        results = await asyncio.gather(self.attempt(row), self.attempt(row))
        self.assertTrue(any(item.get("ok") for item in results))
        self.send.assert_awaited_once()

    async def test_permanent_failure_notice_is_persistent_and_not_duplicated(self):
        from types import SimpleNamespace
        from nonebot.adapters.onebot.v11 import GroupMessageEvent
        from src.plugins.ai_chat.agent.background import SubAgentDispatcher
        from src.plugins.ai_chat.delivery import DeliveryStore
        from src.plugins.ai_chat.onebot_codec import scope_from_event
        event = GroupMessageEvent(time=100, self_id=123, post_type="message", message_type="group",
            sub_type="normal", message_id=42, group_id=1, user_id=2, message="生成文件", raw_message="生成文件",
            font=0, sender={"user_id": 2, "nickname": "Test", "role": "member"})
        self.task = self.store.create_task(scope_key=scope_from_event(event).key, conversation_id="group:1:user:2",
            requester_user_id=2, trigger_message_id=None, objective="file", max_parallelism=1, max_steps=2)
        self.store.update_control(self.task.task_id, expected_version=0,
            dispatch={"bot_id": "123", "event": event.model_dump(mode="json")})
        self.send.return_value = {"ok": False, "not_sent": True, "retryable": False}
        await self.attempt()
        outbox = DeliveryStore(Path(self.tmp.name) / "deliveries.sqlite3")
        try:
            dispatcher = object.__new__(SubAgentDispatcher)
            dispatcher.store = self.store
            dispatcher.context = SimpleNamespace(delivery_store=outbox)
            self.assertIn(self.task.task_id, self.store.rejected_file_tasks())
            dispatcher.notify_rejected_files(self.task.task_id)
            dispatcher.notify_rejected_files(self.task.task_id)
            self.assertEqual(len(outbox.recent()), 1)
            self.assertNotIn(self.task.task_id, self.store.rejected_file_tasks())
        finally:
            outbox.close()
