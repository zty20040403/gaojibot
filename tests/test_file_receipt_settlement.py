from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nonebot.adapters.onebot.v11 import GroupMessageEvent

from tests.test_subagent_v2 import decision, profile
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.background import SubAgentDispatcher
from src.plugins.ai_chat.agent.execution import EntryDecision
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore


class FileReceiptSettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SubAgentStore(self.root / "agents.sqlite3")
        self.addCleanup(lambda: self.store.close())
        self.jobs = DurableJobStore(self.root / "jobs.sqlite3")
        self.addCleanup(self.jobs.close)
        self.outbox = DeliveryStore(self.root / "outbox.sqlite3")
        self.addCleanup(self.outbox.close)
        catalog = ModelCatalog({"qwen-local": profile("qwen-local")}, default_profile="qwen-local")
        coordinator = SubAgentCoordinator(self.store, catalog, logger=Mock())
        event = GroupMessageEvent(time=100, self_id=123, post_type="message", message_type="group",
            sub_type="normal", message_id=42, group_id=1, user_id=2, message="send file",
            raw_message="send file", font=0, sender={"user_id": 2, "nickname": "test"})
        scope = scope_from_event(event).key
        self.task = coordinator.submit(packet=ContextPacket(scope, scope + ":user:2", 2, 3, "send file"),
            decision=EntryDecision.parse(decision("workflow")),
            dispatch={"bot_id": "123", "event": event.model_dump(mode="json"), "profile": "qwen-local"})
        self.dispatcher = SubAgentDispatcher(SimpleNamespace(context=SimpleNamespace(
            subagent_store=self.store, job_store=self.jobs, delivery_store=self.outbox,
            subagent_coordinator=coordinator, logger=Mock())))
        self.payload = {"filename": "task-source.zip", "handle": "s123abc:/workspace/source.zip",
            "ok": False, "state": "unknown", "error": "receipt delayed", "receipt": {"ok": False}}
        self.store.begin_delivery(self.task.task_id, "digest", self.payload)
        self.store.finish_delivery(self.task.task_id, "digest", "acknowledged",
            {**self.payload, "ok": True, "reconciled": True, "file_id": "qq-file-1"})
        self.result = {"answer": "Upload confirmation pending; preview probe failed.",
            "deliveries": [self.payload], "delivery_state": "failed_or_unknown",
            "execution_state": "incomplete", "validation": {"acceptance": {"status": "failed"}}}
        self.store.set_task_state(self.task.task_id, "partial", result=self.result)

    def test_late_receipt_updates_summary_and_payload_without_hiding_preview_failure(self):
        self.assertIn(self.task.task_id, self.store.unsettled_file_receipts())
        delivery = self.dispatcher.settle_file_receipts(self.task.task_id)
        task = self.store.get(self.task.task_id)
        self.assertEqual(task.status, "partial")
        self.assertEqual(task.result["delivery_state"], "acknowledged")
        self.assertEqual(task.result["answer"], self.result["answer"])
        receipt = self.store.deliveries(self.task.task_id)[0]["payload"]
        self.assertEqual(receipt["state"], "acknowledged")
        self.assertEqual(receipt["error"], "")
        self.assertEqual(receipt["receipt"]["file_id"], "qq-file-1")
        self.assertTrue(receipt["receipt"]["ok"])
        self.assertEqual(delivery.idempotency_key, f"subagent-final:{self.task.task_id}:1:file-receipts")
        self.assertEqual(delivery.target_scope.key, self.task.scope_key)
        self.assertNotIn(self.task.task_id, self.store.unsettled_file_receipts())

    def test_restart_after_enqueue_deduplicates_notice(self):
        with patch.object(self.store, "append_checkpoint", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.dispatcher.settle_file_receipts(self.task.task_id)
        first = self.outbox.recent()[0]
        self.store.close()
        self.store = SubAgentStore(self.root / "agents.sqlite3")
        self.dispatcher.store = self.store
        self.assertIn(self.task.task_id, self.store.unsettled_file_receipts())
        second = self.dispatcher.settle_file_receipts(self.task.task_id)
        self.assertEqual(first.delivery_id, second.delivery_id)
        self.assertEqual(len(self.outbox.recent()), 1)

    def test_restart_after_summary_commit_still_queues_notice(self):
        self.store.sync_file_receipt_summary(self.task.task_id, 1)
        self.assertEqual(self.outbox.recent(), [])
        self.assertIsNotNone(self.dispatcher.settle_file_receipts(self.task.task_id))

    def test_only_delivery_failure_can_finish_after_confirmed_receipts(self):
        self.result.update(execution_state="succeeded", validation={"acceptance": {"status": "passed"}})
        self.store.set_task_state(self.task.task_id, "partial", result=self.result)
        self.dispatcher.settle_file_receipts(self.task.task_id)
        self.assertEqual(self.store.get(self.task.task_id).status, "completed")

    def test_draft_receipt_cannot_promote_task_to_completed(self):
        with self.store._transaction() as cursor:
            cursor.execute("DELETE FROM subagent_deliveries WHERE task_id=?", (self.task.task_id,))
        self.store.begin_delivery(self.task.task_id, "digest", self.payload)
        self.store.finish_delivery(self.task.task_id, "digest", "acknowledged",
            {**self.payload, "ok": True, "draft": True, "draft_reason": "价格未核实"})
        self.result.update(execution_state="succeeded", validation={"acceptance": {"status": "passed"}})
        self.store.set_task_state(self.task.task_id, "partial", result=self.result)
        notice = self.dispatcher.settle_file_receipts(self.task.task_id)
        self.assertEqual(self.store.get(self.task.task_id).status, "partial")
        self.assertIn("未完成草稿", str(notice.body))
        self.assertIn("价格未核实", str(notice.body))

    def test_unverified_acceptance_cannot_be_promoted(self):
        self.result.update(execution_state="succeeded", validation={"acceptance": "not_independently_verified"})
        self.store.set_task_state(self.task.task_id, "partial", result=self.result)
        self.dispatcher.settle_file_receipts(self.task.task_id)
        self.assertEqual(self.store.get(self.task.task_id).status, "partial")

    def test_pending_receipt_is_not_success(self):
        self.store.begin_delivery(self.task.task_id, "second-file", {"filename": "other.zip"})
        self.assertNotIn(self.task.task_id, self.store.unsettled_file_receipts())
        self.assertIsNone(self.dispatcher.settle_file_receipts(self.task.task_id))
        self.assertEqual(self.store.get(self.task.task_id).result["delivery_state"], "failed_or_unknown")
        self.assertEqual(self.outbox.recent(), [])

    def test_late_receipt_rebuilds_current_report_without_stale_delivery_claims(self):
        matrix = {"status": "unverified", "criteria": [{"kind": "evidence", "description": "主机数据",
            "status": "unverified", "reason": "仍有数据缺失"}]}
        self.result.update(execution_state="succeeded", report_narrative="全部正常", validation={
            "acceptance": {"status": "passed", "task_outcome": matrix}})
        self.result["answer"] = "文件交付：0/1 个已确认送达。"
        self.store.set_task_state(self.task.task_id, "partial", result=self.result)
        self.dispatcher.settle_file_receipts(self.task.task_id)
        current = self.store.get(self.task.task_id)
        self.assertEqual(current.status, "partial")
        self.assertIn("文件交付：1/1", current.result["answer"])
        self.assertNotIn("全部正常", current.result["answer"])
        self.assertNotIn("0/1 个已确认送达", current.result["answer"])
        self.assertIn("仍有数据缺失", current.result["answer"])

    def test_old_revision_cannot_settle_current_task(self):
        control = self.store.control(self.task.task_id)
        self.store.update_control(self.task.task_id, expected_version=control["version"], revision=2)
        self.assertIsNone(self.store.sync_file_receipt_summary(self.task.task_id, 1))
        self.assertIsNone(self.dispatcher.settle_file_receipts(self.task.task_id))
        self.assertNotIn(self.task.task_id, self.store.unsettled_file_receipts())

    def test_already_reported_success_does_not_send_notice(self):
        self.result["deliveries"] = [{**self.payload, "ok": True}]
        self.store.set_task_state(self.task.task_id, "partial", result=self.result)
        self.assertIsNone(self.dispatcher.settle_file_receipts(self.task.task_id))
        self.assertEqual(self.outbox.recent(), [])
        self.assertNotIn(self.task.task_id, self.store.unsettled_file_receipts())

    def test_nested_file_id_is_preserved(self):
        self.store.begin_delivery(self.task.task_id, "nested-digest", self.payload)
        self.store.finish_delivery(self.task.task_id, "nested-digest", "acknowledged",
            {**self.payload, "ok": True, "receipt": {"ok": True, "file_id": "nested-id"}})
        self.dispatcher.settle_file_receipts(self.task.task_id)
        receipt = next(row["payload"]["receipt"] for row in self.store.deliveries(self.task.task_id)
                       if row["key"] == "nested-digest")
        self.assertEqual(receipt["file_id"], "nested-id")

    def test_changed_owner_cannot_emit_receipt_to_another_group(self):
        control = self.store.control(self.task.task_id)
        control["dispatch"]["event"]["group_id"] = 9
        self.store.update_control(self.task.task_id, expected_version=control["version"], dispatch=control["dispatch"])
        with self.assertRaises(ValueError):
            self.dispatcher.settle_file_receipts(self.task.task_id)
        self.assertEqual(self.outbox.recent(), [])
