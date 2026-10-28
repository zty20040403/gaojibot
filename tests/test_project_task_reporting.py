"""Real project/Outbox receipts and CAS; platform, models and kernels are fixtures."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from src.project_update import ARTIFACT_NAME
from src.plugins.ai_chat.agent.background import SubAgentDispatcher
from src.plugins.ai_chat.agent.workspaces import prune_acknowledged_artifacts
from src.plugins.ai_chat.bridges import BridgeOutcomeUnknown, BridgePermanentError, BridgeRetryableError
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.project_task_reconciliation import ProjectTaskReconciler
from src.plugins.ai_chat.project_task_reporting import latest_report, record_deferred, validate_report_delivery
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, _delivery_outcomes, _outcome_from_run
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests import test_project_task_completion as completion


class ReportFixture:
    def setup_reports(self, database):
        self.outbox = DeliveryStore(database)
        self.addCleanup(self.outbox.close)
        event = GroupMessageEvent(time=int(time.time()), self_id=301, post_type="message", message_type="group",
            sub_type="normal", message_id=42, group_id=101, user_id=201,
            message=[{"type": "text", "data": {"text": "update project"}}], raw_message="update project", font=0,
            sender={"user_id": 201, "nickname": "Test", "role": "member"})
        control = self.tasks.control(self.task.task_id)
        self.tasks.update_control(self.task.task_id, expected_version=control["version"],
            dispatch={"bot_id": "301", "event": event.model_dump(mode="json"), "profile": "fixture"})
        self.coordinator = SubAgentCoordinator(self.tasks, Mock(), logger=Mock())
        self.manager = SimpleNamespace(list=AsyncMock(return_value=[]), stop_owned=AsyncMock())
        self.app = SimpleNamespace(subagent_store=self.tasks, subagent_coordinator=self.coordinator,
            job_store=self.jobs, delivery_store=self.outbox, state_dir=self.state_dir, sandbox_manager=self.manager,
            settings=SimpleNamespace(subagent_retention_seconds=3600), logger=Mock())
        self.services = SimpleNamespace(context=self.app)
        self.dispatcher = SubAgentDispatcher(self.services)

    def report(self):
        return self.dispatcher.enqueue_final(self.task.task_id)

    def text(self, delivery):
        return self.sql.execute("SELECT body_json FROM deliveries WHERE delivery_id=?",
                                (delivery.delivery_id,)).fetchone()[0]

    def state(self):
        return latest_report(self.tasks, self.task.task_id, self.tasks.control(self.task.task_id)["revision"])

    def add_pdf(self, *, required=True):
        result = next(run.result for run in self.tasks.runs(self.task.task_id) if run.run_id == self.producer.run_id)
        pdf = {"name": "report.pdf", "handle": "s123456:/workspace/report.pdf", "snapshot": "b" * 64, "size": 10}
        result["artifacts"].append(pdf)
        self.tasks.finish_run(self.producer.run_id, "succeeded", result=result)
        task = self.tasks.get(self.task.task_id)
        self.tasks.set_task_state(task.task_id, task.status, result=task.result,
                                 plan={**task.plan, "contract": {"version": 2, "delivery_required": required}})
        return pdf

    def acknowledge(self, queued):
        claimed = self.tasks.claim_file(self.task.task_id, queued)
        self.assertIsNotNone(claimed)
        return self.tasks.settle_file_attempt(self.task.task_id, queued, claimed, {"ok": True, "file_id": "fixture"})


class ProjectTaskReportTests(ReportFixture, completion.reconciliation.ReleaseFixture, unittest.IsolatedAsyncioTestCase):
    accepted = completion.ProjectTaskCompletionTests.accepted
    event = completion.ProjectTaskCompletionTests.event

    async def asyncSetUp(self):
        await completion.ProjectTaskCompletionTests.asyncSetUp(self)
        self.setup_reports(self.db)

    async def test_model_success_never_overrides_receipts_and_restart_replays_outbox(self):
        task = self.tasks.get(self.task.task_id)
        self.tasks.set_task_state(task.task_id, "completed", result={**task.result, "answer": "everything deployed and sent"})
        first = self.report()
        self.assertIn("项目任务尚未全部完成", self.text(first))
        self.assertNotIn("everything deployed", self.text(first))
        self.assertNotIn(task.task_id, self.tasks.finalizable_tasks())
        self.assertEqual(self.state()["project"]["code"], "pending")
        self.dispatcher = SubAgentDispatcher(self.services)
        await self.dispatcher.reconcile_project_reports()
        self.assertEqual(self.report().delivery_id, first.delivery_id)
        self.assertEqual(self.count("deliveries"), 1)
        await self.worker.run_once()
        await self.dispatcher.reconcile_project_reports()
        self.assertEqual(self.state()["project"]["code"], "restoring")
        self.assertEqual(self.outbox.get(first.delivery_id).status, "cancelled")
        self.assertEqual(self.count("deliveries"), 2)
        await self.dispatcher.reconcile_project_reports()
        self.assertEqual(self.count("deliveries"), 2)

    async def test_missing_bundle_diagnostic_survives_restart_without_repeated_notifications(self):
        self.path.unlink()
        for _ in range(3):
            await ProjectTaskReconciler(self.runtime, self.tasks).run_once()
            self.report()
        self.assertEqual(self.state()["project"]["code"], "artifact")
        self.assertIn("缺失或不可读取", self.text(self.report()))
        self.assertEqual(self.count("deliveries"), 1)
        self.assertEqual(self.sql.execute("SELECT count(*) FROM subagent_checkpoints "
            "WHERE phase='project_reconcile_deferred'").fetchone()[0], 1)
        self.path.write_bytes(self.content)
        await self.worker.run_once()
        self.report()
        self.assertEqual(self.state()["project"]["code"], "restoring")

    async def test_failed_restore_and_retry_report_real_transitions(self):
        await self.worker.run_once()
        pending = self.report()
        job_id = self.sql.execute("SELECT job_id FROM project_task_materializations").fetchone()[0]
        self.sql.execute("UPDATE durable_jobs SET status='failed' WHERE job_id=?", (job_id,))
        failed = self.report()
        self.assertIn("恢复失败或已停止", self.text(failed))
        self.assertNotEqual(pending.delivery_id, failed.delivery_id)
        self.assertTrue(self.jobs.requeue(job_id))
        retried = self.report()
        self.assertNotIn(retried.delivery_id, (pending.delivery_id, failed.delivery_id))
        self.assertEqual(self.outbox.get(retried.delivery_id).status, "pending")
        self.assertEqual(self.report().delivery_id, retried.delivery_id)

    async def test_report_checkpoint_and_outbox_commit_atomically(self):
        before = self.count("subagent_checkpoints")
        with patch.object(self.tasks, "append_checkpoint", side_effect=OSError("commit failed")):
            with self.assertRaises(OSError):
                self.report()
        self.assertEqual(self.count("deliveries"), 0)
        self.assertEqual(self.count("subagent_checkpoints"), before)
        self.assertIn(self.task.task_id, self.tasks.finalizable_tasks())
        self.assertIsNotNone(self.report())

    async def test_concurrent_processes_enqueue_once(self):
        stores = [self.owned(SubAgentStore(self.db)) for _ in range(2)]
        dispatchers = [SubAgentDispatcher(SimpleNamespace(context=SimpleNamespace(**{
            **vars(self.app), "subagent_store": store}))) for store in stores]
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda d: d.enqueue_final(self.task.task_id).delivery_id, dispatchers))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.count("deliveries"), 1)

    async def test_ambiguous_previous_report_is_not_cancelled_or_blindly_reuploaded(self):
        first = self.report()
        self.assertIsNotNone(self.outbox.begin_direct_attempt(first.delivery_id))
        self.assertTrue(self.outbox.mark_ambiguous(first.delivery_id, "lost response"))
        await self.worker.run_once()
        later = self.report()
        self.assertEqual(self.outbox.get(first.delivery_id).status, "ambiguous")
        self.assertNotEqual(later.delivery_id, first.delivery_id)
        self.assertEqual(self.report().delivery_id, later.delivery_id)

    async def test_file_receipts_and_internal_bundle_are_independent_of_project_updates(self):
        pdf = self.add_pdf()
        before = self.report()
        self.assertEqual(self.state()["files"]["items"][0]["state"], "not_queued")
        queued = self.tasks.queue_file(self.task.task_id, pdf, "report.pdf")
        self.report()
        self.acknowledge(queued)
        current = self.report()
        self.assertIn("1/1 个已确认送达", self.text(current))
        self.assertIn("项目任务尚未全部完成", self.text(current))
        self.assertNotEqual(before.delivery_id, current.delivery_id)
        self.assertEqual(len(self.state()["files"]["items"]), 1)
        with self.assertRaisesRegex(ValueError, "Internal project"):
            self.tasks.queue_file(self.task.task_id, self.artifact, ARTIFACT_NAME)
        self.assertEqual(self.count("subagent_deliveries"), 1)

    async def test_internal_bundle_cannot_be_reclaimed_before_publication(self):
        pdf = self.add_pdf()
        queued = self.tasks.queue_file(self.task.task_id, pdf, "report.pdf")
        self.acknowledge(queued)
        task = self.tasks.get(self.task.task_id)
        self.assertEqual(self.coordinator._artifact_retention_state(task), (False, ()))
        self.snapshots._mark_for_retention(task.task_id, self.digest, int(time.time()) - 7200)
        can_prune = lambda tid, digest: digest in self.coordinator._artifact_retention_state(self.tasks.get(tid))[1]
        self.assertEqual(prune_acknowledged_artifacts(self.snapshots.root, can_prune=can_prune)[0], 0)
        self.assertEqual(self.path.read_bytes(), self.content)
        await self.worker.run_once()
        ready, digests = self.coordinator._artifact_retention_state(self.tasks.get(task.task_id))
        self.assertTrue(ready)
        self.assertIn(self.digest, digests)
        self.assertEqual(prune_acknowledged_artifacts(self.snapshots.root, can_prune=can_prune)[0], 1)
        self.assertEqual(self.store.read_file(self.access, self.pid, 2, "index.html"), b"updated")

    async def test_cleanup_is_scheduled_after_cas_not_repeated_after_restart(self):
        await self.dispatcher.schedule_project_cleanup(self.task.task_id)
        self.assertFalse(self.snapshots._retention_marker(self.task.task_id, self.digest).exists())
        await self.worker.run_once()
        await self.dispatcher.schedule_project_cleanup(self.task.task_id)
        marker = self.snapshots._retention_marker(self.task.task_id, self.digest)
        payload = json.loads(marker.read_text())
        self.assertLessEqual(payload["delete_after"] - payload["acknowledged_at"], 3600)
        marker.unlink()
        await SubAgentDispatcher(self.services).schedule_project_cleanup(self.task.task_id)
        self.assertFalse(marker.exists())

    async def test_unrequested_sidecar_does_not_require_qq_receipt_for_cleanup(self):
        self.add_pdf(required=False)
        await self.worker.run_once()
        ready, digests = self.coordinator._artifact_retention_state(self.tasks.get(self.task.task_id))
        self.assertTrue(ready)
        self.assertEqual(set(digests), {self.digest, "b" * 64})

    async def test_send_time_rechecks_account_scope_revision_and_live_membership(self):
        delivery = self.report()
        self.runtime.ready, self.runtime.closed = True, False
        self.runtime.resolve_access = AsyncMock(return_value=self.access)
        self.app.project_runtime = self.runtime
        bot = SimpleNamespace(self_id="301")
        await validate_report_delivery(self.app, bot, delivery)
        for changed in (replace(delivery, target_native_conversation_id="999"),
                        replace(delivery, source_scope_key="onebot-v11:group:999")):
            with self.assertRaises(BridgePermanentError):
                await validate_report_delivery(self.app, bot, changed)
        with self.assertRaises(BridgePermanentError):
            await validate_report_delivery(self.app, SimpleNamespace(self_id="302"), delivery)
        self.runtime.resolve_access.side_effect = PermissionError("left group")
        with self.assertRaises(BridgePermanentError):
            await validate_report_delivery(self.app, bot, delivery)
        self.runtime.resolve_access.side_effect = JobDeferred("offline", 30)
        with self.assertRaises(BridgeRetryableError):
            await validate_report_delivery(self.app, bot, delivery)
        self.runtime.resolve_access.side_effect = None
        await self.worker.run_once()
        newer = self.report()
        with self.assertRaises(BridgePermanentError):
            await validate_report_delivery(self.app, bot, delivery)
        await validate_report_delivery(self.app, bot, newer)

    async def test_late_old_revision_failure_does_not_pollute_new_report(self):
        self.sql.execute("UPDATE subagent_controls SET revision=2 WHERE task_id=?", (self.task.task_id,))
        record_deferred(self.tasks, self.task.task_id, 1, PermissionError("old secret"))
        self.report()
        self.assertEqual(self.state()["project"]["code"], "pending")
        self.assertNotIn("secret", self.text(self.report()))

    async def test_delivery_selection_keeps_upstream_pdf_when_sink_is_internal_only(self):
        pdf = self.add_pdf()
        original = next(run for run in self.tasks.runs(self.task.task_id) if run.run_id == self.producer.run_id)
        from src.plugins.ai_chat.subagents import TaskStep

        integration = self.tasks.create_run(self.task.task_id,
            TaskStep("project_integrate", "coder", "integrate", "update", dependencies=(original.step_key,)),
            allowed_tools=[], model_profile="fixture")
        self.tasks.finish_run(original.run_id, "succeeded", result={"artifacts": [pdf]})
        self.tasks.finish_run(integration.run_id, "succeeded", result={"artifacts": [self.artifact]})
        task = self.tasks.get(self.task.task_id)
        completed = {run.step_key: _outcome_from_run(task, run) for run in self.tasks.runs(task.task_id)}
        visible = _delivery_outcomes(task, completed, include_project_updates=False)
        self.assertEqual([a["name"] for item in visible for a in item.result["artifacts"]], ["report.pdf"])
        acceptance = _delivery_outcomes(task, completed)
        self.assertEqual([a["name"] for item in acceptance for a in item.result["artifacts"]], [ARTIFACT_NAME])
        ordinary = replace(task, project_id=None)
        self.assertEqual(_delivery_outcomes(ordinary, completed, include_project_updates=False),
                         _delivery_outcomes(ordinary, completed))

    async def test_real_delivery_handler_sends_pdf_but_never_internal_update(self):
        from src.plugins.ai_chat.subagents import AgentExecutionHooks

        pdf = self.add_pdf()
        task = self.tasks.get(self.task.task_id)
        completed = {run.step_key: _outcome_from_run(task, run) for run in self.tasks.runs(task.task_id)}
        executor = SimpleNamespace(send_file_content=AsyncMock(return_value={"ok": True, "file_id": "pdf"}),
                                   file_delivery_blocker=AsyncMock(return_value=None))
        workspaces = SimpleNamespace(executor=executor, prepare_delivery=AsyncMock(return_value=b"pdf fixture"))
        with patch("src.plugins.ai_chat.subagents.tool_enabled", return_value=True):
            deliveries = await self.coordinator._deliver_requested_artifacts(task, completed,
                execute_tool=AsyncMock(), delivered_artifacts=set(), progress=None,
                hooks=AgentExecutionHooks(workspaces=workspaces),
                validation={"artifacts": [{"artifact_key": pdf["snapshot"], "status": "passed"}]})
        self.assertEqual(len(deliveries), 1)
        self.assertTrue(deliveries[0]["ok"])
        self.assertEqual(self.tasks.deliveries(task.task_id)[0]["key"], pdf["snapshot"])
        self.assertEqual(workspaces.prepare_delivery.await_args.args[1]["name"], "report.pdf")
        executor.send_file_content.assert_awaited_once()

    async def test_independent_validator_checks_both_internal_update_and_visible_pdf(self):
        from src.plugins.ai_chat.subagents import AgentExecutionHooks, TaskStep

        pdf = self.add_pdf()
        original = next(run for run in self.tasks.runs(self.task.task_id) if run.run_id == self.producer.run_id)
        integration = self.tasks.create_run(self.task.task_id,
            TaskStep("project_integrate", "coder", "integrate", "update", dependencies=(original.step_key,)),
            allowed_tools=[], model_profile="fixture")
        self.tasks.finish_run(original.run_id, "succeeded", result={"artifacts": [pdf]})
        self.tasks.finish_run(integration.run_id, "succeeded", result={"artifacts": [self.artifact]})
        task = self.tasks.get(self.task.task_id)
        completed = {run.step_key: _outcome_from_run(task, run) for run in self.tasks.runs(task.task_id)}
        workspaces = SimpleNamespace(manager=SimpleNamespace(backend="oci"), validate=AsyncMock(return_value={"ok": True}))
        reviewer = AsyncMock(side_effect=RuntimeError("reviewer boundary"))
        with patch("src.plugins.ai_chat.subagents.tool_enabled", return_value=True), \
                patch.object(self.coordinator, "_run_step_reliably", reviewer), \
                patch.object(self.coordinator, "_profile_for", return_value=SimpleNamespace(name="fixture")):
            with self.assertRaisesRegex(RuntimeError, "reviewer boundary"):
                await self.coordinator._validate_workflow(task, completed, context=Mock(), selected_profile=Mock(),
                    tools_by_name={}, execute_tool=AsyncMock(), hooks=AgentExecutionHooks(workspaces=workspaces),
                    parent_trace=None, progress=None)
        self.assertEqual({call.args[1]["name"] for call in workspaces.validate.await_args_list},
                         {ARTIFACT_NAME, "report.pdf"})
        self.assertEqual(workspaces.validate.await_count, 2)

    async def test_failed_release_is_reported_without_claiming_project_updated(self):
        self.tasks.set_task_state(self.task.task_id, "failed", error="worker failed")
        self.report()
        self.releases.release_failed(self.access, self.task.task_id)
        self.report()
        self.assertEqual(self.state()["project"]["code"], "released")
        self.assertIn("本任务未更新项目", self.text(self.report()))

    async def test_progress_has_project_stage_not_just_completed_execution(self):
        from src.plugins.ai_chat.agent.progress import task_progress

        delivery = self.report()
        task = self.tasks.get(self.task.task_id)
        progress = task_progress(task, self.tasks.runs(task.task_id), [], [], [], delivery,
                                 revision=1, project_completion=self.state())
        stages = {stage["key"]: stage for stage in progress["stages"]}
        self.assertEqual(stages["execution"]["status"], "completed")
        self.assertEqual(stages["project"]["status"], "waiting")
        self.assertEqual(progress["stages"][-1]["key"], "delivery")
        await self.worker.run_once()
        self.report()
        progress = task_progress(task, [], [], [], [], self.report(), revision=1, project_completion=self.state())
        self.assertIn("尚未恢复", next(stage for stage in progress["stages"] if stage["key"] == "project")["detail"])

    async def test_admin_detail_exposes_latest_project_receipt_not_missing_legacy_key(self):
        import httpx
        from fastapi import FastAPI
        from src.plugins.ai_chat.admin import AdminServices
        from tests.admin_session_fixture import ApprovedClient, register_admin

        self.report()
        await self.worker.run_once()
        latest = self.report()
        app = FastAPI()
        register_admin(app, AdminServices(version="test", started_at=1,
            subagent_store=self.tasks, delivery_store=self.outbox), test_case=self, token="test-token")
        async with ApprovedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/bot-admin/api/subagents/{self.task.task_id}", headers={"X-Test-Login": "admin"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["final_delivery"]["delivery_id"], latest.delivery_id)
        self.assertEqual(payload["project_completion"]["project"]["code"], "restoring")
        self.assertEqual(next(s for s in payload["progress"]["stages"] if s["key"] == "project")["status"], "waiting")
        self.assertNotIn("dispatch", payload["control"])

    async def test_actual_onebot_sender_requires_live_access_and_native_message_receipt(self):
        from src.plugins.ai_chat.onebot_delivery import OneBotDelivery

        delivery = self.report()
        self.runtime.ready, self.runtime.closed = True, False
        self.runtime.resolve_access = AsyncMock(return_value=self.access)
        self.app.project_runtime = self.runtime
        self.app.mirror_state = self.app.message_ledger = None
        self.services.group_enabled = lambda _: True
        self.services.replies = SimpleNamespace(_sent_message_id=lambda response: response.get("message_id"))
        bot = SimpleNamespace(self_id="301", send_group_msg=AsyncMock(return_value={}))
        sender = OneBotDelivery(self.services)
        self.runtime.resolve_access.side_effect = PermissionError("revoked")
        with self.assertRaises(BridgePermanentError):
            await sender._deliver_onebot_outbox(bot, delivery)
        bot.send_group_msg.assert_not_awaited()
        self.runtime.resolve_access.side_effect = None
        claimed = self.outbox.begin_direct_attempt(delivery.delivery_id)
        with self.assertRaises(BridgeOutcomeUnknown):
            await sender._deliver_onebot_outbox(bot, claimed)
        self.assertEqual(self.outbox.get(delivery.delivery_id).status, "sending")
        # Emulate the production loop's ambiguous branch, never retry the upload.
        self.assertTrue(self.outbox.mark_ambiguous(delivery.delivery_id, "no native receipt"))
        self.assertEqual(self.report().delivery_id, delivery.delivery_id)
        bot.send_group_msg.assert_awaited_once()


class ProjectTaskMaterializedReportTests(ReportFixture, completion.materialization.MaterializationMixin,
                                       unittest.IsolatedAsyncioTestCase):
    request = completion.restore.OciRestoreTests.request
    wake = completion.restore.OciRestoreTests.wake
    claim = completion.restore.OciRestoreTests.claim

    async def asyncSetUp(self):
        await completion.ProjectTaskCompletedWorkspaceTests.asyncSetUp(self)
        self.setup_reports(self.projects.database)

    async def test_final_success_requires_physical_workspace_receipt_and_separate_attachment_receipt(self):
        pdf = self.add_pdf()
        self.report()
        await ProjectTaskReconciler(self.runtime, self.tasks).run_once()
        self.report()
        self.assertIn("尚未完成更新", self.text(self.report()))
        self.path.unlink()
        await self.executor(self.claim())
        self.report()
        self.assertEqual(self.state()["project"]["code"], "materialized")
        self.assertIn("项目任务尚未全部完成", self.text(self.report()))
        queued = self.tasks.queue_file(self.task.task_id, pdf, "report.pdf")
        self.acknowledge(queued)
        await SubAgentDispatcher(self.services).reconcile_project_reports()
        final = self.report()
        self.assertIn("项目任务已完成", self.text(final))
        self.assertIn("1/1 个已确认送达", self.text(final))
        current = self.volumes.binding(self.scope.key, self.pid, generation=2)
        self.assertEqual((current / "index.html").read_bytes(), b"updated")
        self.assertFalse((current / "obsolete.txt").exists())
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "obsolete.txt"), b"old")
        self.assertEqual(self.state()["delivery_id"], final.delivery_id)


if __name__ == "__main__":
    unittest.main()
