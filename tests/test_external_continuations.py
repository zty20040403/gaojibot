from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from tests.test_subagent_v2 import decision, profile
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.background import SubAgentDispatcher
from src.plugins.ai_chat.agent.execution import EntryDecision
from src.plugins.ai_chat.agent.external import ExternalCalls, ExternalPending, active_external, poll_external
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.fleet_client import FleetControlError
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, TaskStep, StepOutcome
from src.plugins.ai_chat.deepseek import DeepSeekTrace
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred


class ExternalContinuationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SubAgentStore(self.root / "agents.sqlite3")
        self.jobs = DurableJobStore(self.root / "jobs.sqlite3", lease_seconds=10)
        self.outbox = DeliveryStore(self.root / "deliveries.sqlite3")
        self.catalog = ModelCatalog({"qwen-local": profile("qwen-local")}, default_profile="qwen-local")
        self.coordinator = SubAgentCoordinator(self.store, self.catalog, logger=Mock())
        self.event = GroupMessageEvent(time=int(time.time()), self_id=123, post_type="message",
            message_type="group", sub_type="normal", message_id=42, group_id=1, user_id=2,
            message="检查磁盘", raw_message="检查磁盘", font=0, sender={"user_id": 2, "nickname": "test"})
        self.scope = scope_from_event(self.event).key
        self.packet = ContextPacket(self.scope, self.scope + ":user:2", 2, 3, "检查磁盘")
        self.task = self.coordinator.submit(packet=self.packet, decision=EntryDecision.parse(decision("workflow")),
            dispatch={"bot_id": "123", "event": self.event.model_dump(mode="json"), "profile": "qwen-local"})
        self.step = TaskStep("disk", "operator", "检查磁盘", "报告")
        self.run = self.store.create_run(self.task.task_id, self.step, allowed_tools=[], model_profile="qwen-local")
        self.client = SimpleNamespace(_request=AsyncMock())
        self.services = SimpleNamespace(context=SimpleNamespace(subagent_store=self.store, job_store=self.jobs,
            subagent_coordinator=self.coordinator, delivery_store=self.outbox, fleet_client=self.client, logger=Mock()),
            group_enabled=lambda _: True, tools=SimpleNamespace(_ask_ai=AsyncMock()))
        self.dispatcher = SubAgentDispatcher(self.services)

    def tearDown(self):
        self.store.close()
        self.jobs.close()
        self.outbox.close()
        self.tmp.cleanup()

    def tracker(self):
        tracker = ExternalCalls(self.store, self.task.task_id, self.run.run_id)
        tracker.call_id = "call1"
        tracker.tool_name = "ops_call"
        tracker.arguments = {"operation": "exec.run", "host_id": "h610"}
        return tracker

    async def test_failed_reconciliation_does_not_cancel_execution_loop(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def execution_loop():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(self.dispatcher, "reconcile_once", new=AsyncMock(side_effect=ConnectionError("offline"))), \
                patch.object(self.dispatcher.worker, "run_forever", side_effect=execution_loop):
            running = asyncio.create_task(self.dispatcher.run_forever())
            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(0.02)
                self.assertFalse(running.done())
                self.assertFalse(stopped.is_set())
                self.services.context.logger.error.assert_called_once()
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        self.assertTrue(stopped.is_set())

    async def test_one_bad_task_does_not_block_queue_reconciliation(self):
        enqueue = self.dispatcher.enqueue

        def enqueue_with_bad_record(task_id):
            if task_id == -1:
                raise ValueError("invalid dispatch")
            return enqueue(task_id)

        with patch.object(self.store, "dispatchable_tasks", return_value=[-1, self.task.task_id]), \
                patch.object(self.dispatcher, "enqueue", side_effect=enqueue_with_bad_record), \
                patch.object(self.dispatcher, "prune_workspaces", new=AsyncMock()):
            await self.dispatcher.reconcile_once()
            await self.dispatcher.reconcile_once()
        jobs = self.jobs.claim_due("worker")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].payload["task_id"], self.task.task_id)

    async def submit_remote(self, perform=None):
        tracker = self.tracker()
        perform = perform or AsyncMock(return_value={"operation": {"operation_id": "op_test", "status": "running"}})
        response = await tracker.request("POST", "/v1/ops/call", tracker.arguments,
            actor="qq:2", origin=self.scope, perform=perform)
        return tracker, response

    def session(self, response=None):
        messages = [{"role": "assistant", "tool_calls": [{"id": "call1", "type": "function",
            "function": {"name": "ops_call", "arguments": json.dumps(self.tracker().arguments)}}]}]
        if response is not None:
            messages.append({"role": "tool", "tool_call_id": "call1", "content": json.dumps(response)})
        self.store.save_agent_session(self.task.task_id, self.run.run_id, messages,
            scope_key=self.scope, requester_user_id=2, model_profile="qwen-local", expected_version=0)

    async def test_restart_polls_same_handle_and_repairs_receipt_gap(self):
        tracker, response = await self.submit_remote()
        self.session()
        with self.assertRaises(ExternalPending):
            tracker.pause()
        self.store.close()
        self.store = SubAgentStore(self.root / "agents.sqlite3")
        final = {"operation_id": "op_test", "status": "succeeded", "result": {"bytes": 1234}}
        self.client._request.return_value = final
        self.assertTrue(await poll_external(self.store, self.task, self.client))
        args = self.client._request.call_args
        self.assertEqual(args.args, ("GET", "/v1/operations/op_test"))
        self.assertEqual(args.kwargs, {"actor": "qq:2", "origin": self.scope})
        hydrated = self.store.hydrate_external_session(self.task.task_id, self.run.run_id)
        hydrated_result = json.loads(hydrated["messages"][-1]["content"])
        evidence_ref = hydrated_result.pop("_task_evidence")
        self.assertEqual(hydrated_result, final)
        evidence = next(item for item in self.store.task_evidence(self.task.task_id) if item["evidence_id"] == evidence_ref["ref"])
        self.assertEqual(evidence["payload"], final)
        self.assertEqual(evidence["run_id"], self.run.run_id)
        self.tracker().pause()

    async def test_service_verification_waits_then_delivers_verified_summary_to_agent(self):
        tracker, response = await self.submit_remote()
        self.session(response)
        self.client._request.return_value = {"operation_id": "op_test", "status": "reconciling",
            "result": {"phase": "verifying_service", "verification": {"verified": False}}}
        self.assertFalse(await poll_external(self.store, self.task, self.client))
        with self.assertRaises(ExternalPending):
            tracker.pause()
        summary = "h610 的 test.service 已重启，实例编号已变化，当前 PID 234，两次复查均正常。"
        self.client._request.return_value = {"operation_id": "op_test", "status": "succeeded",
            "result": {"phase": "verified", "summary": summary, "verification": {"verified": True}}}
        self.assertTrue(await poll_external(self.store, self.task, self.client))
        hydrated = self.store.hydrate_external_session(self.task.task_id, self.run.run_id)
        self.assertEqual(json.loads(hydrated["messages"][-1]["content"])["result"]["summary"], summary)
        self.assertTrue(all(call.args == ("GET", "/v1/operations/op_test") for call in self.client._request.call_args_list))

    async def test_lost_submission_receipt_replays_host_owned_key(self):
        perform = AsyncMock(side_effect=FleetControlError("timeout", "lost receipt", retryable=True))
        tracker, response = await self.submit_remote(perform)
        self.assertTrue(response["pending"])
        submitted = perform.call_args.args[0]
        self.assertTrue(submitted["idempotency_key"].startswith("subagent:"))
        self.client._request.return_value = {"operation_id": "op_test", "status": "succeeded"}
        self.assertTrue(await poll_external(self.store, self.task, self.client))
        self.assertEqual(self.client._request.call_args.args[2], submitted)
        await tracker.request("POST", "/v1/ops/call", tracker.arguments,
            actor="qq:2", origin=self.scope, perform=perform)
        self.assertEqual(perform.await_count, 1)

    async def test_pending_releases_lease_without_llm_or_failure_attempt(self):
        await self.submit_remote()
        self.store.finish_run(self.run.run_id, "waiting_external", result={"status": "waiting"})
        self.store.set_task_state(self.task.task_id, "waiting_external")
        self.client._request.return_value = {"operation_id": "op_test", "status": "running"}
        self.dispatcher.enqueue(self.task.task_id)
        job = self.jobs.claim_due("worker")[0]
        with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=Mock()):
            await self.dispatcher.worker._execute(job)
        self.assertEqual(self.jobs.get(job.job_id).status, "pending")
        self.assertEqual(self.jobs.get(job.job_id).attempts, 0)
        self.assertEqual(self.store.get(self.task.task_id).status, "waiting_external")
        self.services.tools._ask_ai.assert_not_awaited()
        self.assertEqual(self.outbox.recent(), [])

    async def test_cancel_and_deadline_do_not_replay_remote_commands(self):
        await self.submit_remote()
        self.store.finish_run(self.run.run_id, "waiting_external")
        self.store.request_cancel(self.task.task_id)
        self.assertFalse(await poll_external(self.store, self.store.get(self.task.task_id), self.client))
        self.dispatcher.enqueue(self.task.task_id)
        job = self.jobs.claim_due("worker")[0]
        await self.dispatcher.execute(job)
        self.client._request.assert_not_awaited()
        self.assertEqual(self.store.get(self.task.task_id).status, "cancelled")
        self.assertNotEqual(self.store.runs(self.task.task_id)[0].status, "waiting_external")
        self.assertEqual(len(self.outbox.recent()), 1)

    async def test_expired_deadline_settles_even_while_qq_offline(self):
        dispatch = self.store.control(self.task.task_id)["dispatch"]
        dispatch["deadline"] = time.time() - 1
        self.store.update_control(self.task.task_id, expected_version=1, dispatch=dispatch)
        self.dispatcher.enqueue(self.task.task_id)
        job = self.jobs.claim_due("worker")[0]
        with patch("src.plugins.ai_chat.agent.background.get_bot", side_effect=KeyError):
            await self.dispatcher.execute(job)
        self.assertEqual(self.store.get(self.task.task_id).status, "failed")
        self.assertEqual(len(self.outbox.recent()), 1)

    async def test_cancel_during_status_poll_cannot_resurrect_waiting_task(self):
        await self.submit_remote()
        self.store.finish_run(self.run.run_id, "waiting_external", result={"status": "waiting"})
        self.store.set_task_state(self.task.task_id, "waiting_external")
        async def status(*_args, **_kwargs):
            self.coordinator.cancel(self.task.task_id)
            return {"operation_id": "op_test", "status": "running"}
        self.client._request.side_effect = status
        self.dispatcher.enqueue(self.task.task_id)
        job = self.jobs.claim_due("worker")[0]
        with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=Mock()):
            await self.dispatcher.execute(job)
        self.assertEqual(self.store.get(self.task.task_id).status, "cancelled")
        self.assertEqual(self.store.runs(self.task.task_id)[0].status, "cancelled")
        self.services.tools._ask_ai.assert_not_awaited()
        self.assertEqual(len(self.outbox.recent()), 1)

    async def test_offline_does_not_consume_attempts(self):
        self.dispatcher.enqueue(self.task.task_id)
        job = self.jobs.claim_due("worker")[0]
        with patch("src.plugins.ai_chat.agent.background.get_bot", side_effect=KeyError):
            with self.assertRaises(JobDeferred):
                await self.dispatcher.execute(job)
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_final_enqueue_crash_gap_is_idempotent(self):
        self.store.set_task_state(self.task.task_id, "completed", result={"answer": "磁盘已查清，没有执行删除。"})
        self.assertEqual(self.store.finalizable_tasks(), [self.task.task_id])
        with patch.object(self.store, "mark_final_queued", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.dispatcher.enqueue_final(self.task.task_id)
        self.assertEqual(self.store.finalizable_tasks(), [self.task.task_id])
        self.dispatcher.enqueue_final(self.task.task_id)
        self.assertEqual(self.store.finalizable_tasks(), [])
        self.assertEqual(len(self.outbox.recent()), 1)
        self.assertEqual(self.outbox.claim_due(exclude_platforms=("onebot-v11",)), [])
        claimed = self.outbox.claim_due()[0]
        self.assertTrue(self.outbox.defer_unsent(claimed, "offline", delay_seconds=0))
        self.assertEqual(self.outbox.claim_due()[0].attempts, 1)

    async def test_lost_final_receipt_matches_only_correct_bot_group_body(self):
        self.store.set_task_state(self.task.task_id, "completed", result={"answer": "完成"})
        delivery = self.dispatcher.enqueue_final(self.task.task_id)
        self.outbox.claim_due()
        self.outbox.park_interrupted_attempts()
        raw = {"user_id": 999, "time": delivery.created_at, "group_id": 1, "message_id": 101,
            "message": [{"type": "reply", "data": {"id": "42"}},
                        {"type": "text", "data": {"text": f"{self.task.handle}\n完成"}}]}
        bot = SimpleNamespace(call_api=AsyncMock(return_value={"messages": [raw]}))
        with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=bot):
            self.assertEqual(await self.dispatcher.reconcile_final_messages(), 0)
            raw["user_id"] = 123
            raw["group_id"] = 2
            self.assertEqual(await self.dispatcher.reconcile_final_messages(), 0)
            raw["group_id"] = 1
            self.assertEqual(await self.dispatcher.reconcile_final_messages(), 1)
        self.assertEqual(self.outbox.get(delivery.delivery_id).status, "committed")
        self.assertEqual(self.outbox.get(delivery.delivery_id).native_message_id, "101")

    async def test_restart_does_not_exhaust_checkpointed_job_retry_budget(self):
        job, _ = self.jobs.enqueue(kind="checkpointed", idempotency_key="restart", max_attempts=1,
            resume_on_lease_loss=True)
        now = int(time.time())
        first = self.jobs.claim_due("old", now=now)[0]
        for i in range(1, 8):
            self.jobs.recover_expired_leases(now=now + i * 10)
            claimed = self.jobs.claim_due(f"new-{i}", now=now + i * 10)[0]
            self.assertEqual(claimed.attempts, 1)
        self.assertFalse(self.jobs.defer(first, now=now + 70))

    async def test_external_receipt_does_not_cover_other_side_effects(self):
        tracker, response = await self.submit_remote()
        self.client._request.return_value = {"operation_id": "op_test", "status": "succeeded"}
        await poll_external(self.store, self.task, self.client)
        self.store.append_event(self.task.task_id, "agent.tool_started", {"call_id": "call1", "tool_name": "ops_call",
            "arguments": tracker.arguments, "idempotency": "non-idempotent"}, run_id=self.run.run_id)
        self.assertTrue(self.store.run_resume_safe(self.run.run_id))
        self.store.append_event(self.task.task_id, "agent.tool_started", {"call_id": "call1", "tool_name": "sandbox_exec",
            "arguments": {}, "idempotency": "non-idempotent"}, run_id=self.run.run_id)
        self.assertFalse(self.store.run_resume_safe(self.run.run_id))
        with self.assertRaises(PermissionError):
            await tracker.request("POST", "/v1/ops/call", {}, actor="qq:99", origin=self.scope, perform=AsyncMock())

    async def test_waiting_dependency_is_not_partial_and_does_not_start_child(self):
        child = TaskStep("report", "analyst", "整理结论", "报告", ("disk",))
        child_run = self.store.create_run(self.task.task_id, child, allowed_tools=[], model_profile="qwen-local")
        worker = AsyncMock(return_value=(StepOutcome(self.step, self.run, {}, DeepSeekTrace(), "waiting"), None))
        completed = {}
        with self.assertRaises(ExternalPending):
            await self.coordinator._schedule_workflow(self.task, pending={"disk": self.step, "report": child}, completed=completed,
                runs={"disk": self.run, "report": child_run}, in_flight={}, run_step=worker, parent_trace=None, progress=None)
        self.assertEqual(worker.await_count, 1)
        self.assertEqual(completed, {})

    async def test_workflow_resumes_only_unfinished_steps_then_queues_final(self):
        from src.plugins.ai_chat.deepseek import AgentLoopEvent
        baseline = self.store.create_run(self.task.task_id, TaskStep("baseline", "operator", "已做检查", "证据"),
            allowed_tools=[], model_profile="qwen-local")
        self.store.finish_run(baseline.run_id, "succeeded", result={"status": "success", "summary": "服务正常"})
        child = TaskStep("report", "analyst", "整理结论", "报告", ("disk", "baseline"))
        self.store.create_run(self.task.task_id, child, allowed_tools=[], model_profile="qwen-local")
        visited = []

        async def model(text, history, *_args, **kwargs):
            tracker = active_external.get()
            visited.append(tracker.run_id)
            if tracker.run_id == self.run.run_id and not history:
                kwargs["transcript_sink"]([{"role": "assistant", "tool_calls": [{"id": "call1", "type": "function",
                    "function": {"name": "ops_call", "arguments": json.dumps(self.tracker().arguments)}}]}])
                await kwargs["event_sink"](AgentLoopEvent(kind="tool_started", sequence=1,
                    tool_name="ops_call", call_id="call1", arguments=self.tracker().arguments, idempotency="non-idempotent"))
                await tracker.request("POST", "/v1/ops/call", tracker.arguments, actor="qq:2", origin=self.scope,
                    perform=AsyncMock(return_value={"operation_id": "op_test", "status": "running"}))
                kwargs["after_tool_round"]()
                self.fail("Must suspend before returning a partial result")
            if tracker.run_id == self.run.run_id:
                self.assertEqual(json.loads(history[-1]["content"])["status"], "succeeded")
            return '{"status":"success","summary":"磁盘结果已核对","confidence":1}'

        async def ask(_bot, _event, _question, **_kwargs):
            return await self.coordinator.resume(self.task.task_id, scope_key=self.scope, requester_user_id=2,
                selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock())

        self.services.tools._ask_ai = ask
        self.dispatcher.enqueue(self.task.task_id)
        with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=Mock()), \
             patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model), \
             patch.object(self.coordinator, "_supervisor_text", new=AsyncMock(return_value="检查已完成，没有执行删除。")):
            first = self.jobs.claim_due("worker")[0]
            await self.dispatcher.worker._execute(first)
            self.assertEqual(self.store.get(self.task.task_id).status, "waiting_external")
            self.assertEqual(self.outbox.recent(), [])
            self.store.close()
            self.store = SubAgentStore(self.root / "agents.sqlite3")
            self.coordinator.store = self.store
            self.dispatcher.store = self.store
            self.services.context.subagent_store = self.store
            self.client._request.return_value = {"operation_id": "op_test", "status": "succeeded", "result": "磁盘实测完成"}
            self.dispatcher.worker.worker_id = "new-worker"
            resumed = self.jobs.claim_due("new-worker", now=int(time.time()) + 16)[0]
            await self.dispatcher.worker._execute(resumed)
        self.assertEqual(self.store.get(self.task.task_id).status, "completed")
        self.assertEqual(self.jobs.get(first.job_id).status, "succeeded")
        self.assertEqual(len(self.outbox.recent()), 1)
        self.assertNotIn(baseline.run_id, visited)
        self.assertEqual(visited.count(self.run.run_id), 2)

    async def test_tool_plan_is_saved_before_execution_and_receipt_before_suspension(self):
        from src.plugins.ai_chat.deepseek import ask_deepseek_with_tools
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[
            SimpleNamespace(id="clock", type="function", function=SimpleNamespace(name="noop", arguments="{}"))]))])
        snapshots = []

        def persist(messages):
            snapshots.append(json.loads(json.dumps(messages)))

        async def execute(_name, _arguments):
            self.assertEqual(snapshots[-1][-1]["role"], "assistant")
            self.assertEqual(snapshots[-1][-1]["tool_calls"][0]["id"], "clock")
            return '{"ok":true}'

        def suspend():
            self.assertEqual(snapshots[-1][-1]["role"], "tool")
            raise ExternalPending()

        with patch("src.plugins.ai_chat.deepseek._create_completion", new=AsyncMock(return_value=response)) as llm:
            with self.assertRaises(ExternalPending):
                await ask_deepseek_with_tools("noop", [], [{"type": "function", "function": {
                    "name": "noop", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
                    execute, transcript_sink=persist, after_tool_round=suspend)
        self.assertEqual(llm.await_count, 1)
