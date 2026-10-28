from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.control import LeaseLost, active_job_fence
from src.plugins.ai_chat.agent.execution import EntryDecision
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.model_budget import active_model_budget, ModelBudgetExceeded
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.onebot_codec import decode_onebot_message
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routines import (
    RoutineEffectUncertain,
    RoutineChildPending,
    RoutineLeaseLost,
    RoutineStore,
)
from src.plugins.ai_chat.routine_worker import (
    RoutineDispatcher,
    RoutineExecution,
    routine_event,
)
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore
from src.plugins.ai_chat.routine_subagents import run_child, resume_child
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from tests.postgres_fixture import postgres_for_test
from tests.test_llm_gateway import profile
from tests.handler_fixture import HandlerTestCaseMixin
from tests.test_subagent_v2 import decision


class RoutineWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        self.jobs = self.owned(DurableJobStore(self.db))
        self.outbox = self.owned(DeliveryStore(self.db, recover_interrupted=False))
        self.usage = self.owned(UsageStore(self.db))
        self.journal = self.owned(TurnJournal(self.db))
        self.store = self.owned(
            RoutineStore(self.db, jobs=self.jobs, outbox=self.outbox, usage=self.usage)
        )
        self.scope = ConversationScope("onebot-v11", "group", "101", "201", "301")
        self.profile = profile("routine-model")
        self.bot = SimpleNamespace(
            self_id="301",
            call_api=AsyncMock(return_value={"online": True, "good": True}),
            get_group_member_list=AsyncMock(return_value=[{"user_id": 201}]),
        )
        self.app = SimpleNamespace(
            routine_store=self.store,
            job_store=self.jobs,
            delivery_store=self.outbox,
            turn_journal=self.journal,
            db_call=DatabaseCalls(),
            settings=SimpleNamespace(
                routine_timeout_seconds=30, routine_check_seconds=5
            ),
            logger=Mock(),
            subagent_store=None,
        )
        self.addAsyncCleanup(self.app.db_call.close)
        self.app.job_worker = DurableJobWorker(self.jobs, logger=self.app.logger)
        self.services = SimpleNamespace(
            context=self.app,
            group_enabled=Mock(return_value=True),
            chat=SimpleNamespace(
                _conversation_id=Mock(return_value="group:101:user:201"),
                _preferred_model_profile=Mock(return_value=self.profile),
            ),
            tools=SimpleNamespace(
                _ask_ai=AsyncMock(return_value="Actual evidence summary")
            ),
        )
        self.dispatcher = RoutineDispatcher(self.services)
        self.get_bot = patch(
            "src.plugins.ai_chat.routine_worker.get_bot", return_value=self.bot
        ).start()
        self.addCleanup(patch.stopall)

    def owned(self, store):
        self.addCleanup(store.close)
        return store

    def running(self, **changes):
        routine = self.store.set(
            self.scope,
            name="daily",
            prompt="Summarize the group",
            cron="* * * * *",
            now=int(time.time()) - 65,
            **changes,
        )
        run = self.store.materialize_due()[0]
        job = self.jobs.claim_due("routine-test")[0]
        return routine, run, job

    def reclaim(self, job):
        self.assertTrue(self.jobs.defer(job, delay_seconds=0))
        return self.jobs.claim_due("replacement")[0]

    def child_inputs(self):
        self.app.subagent_store = self.owned(SubAgentStore(self.db))
        self.app.subagent_coordinator = SubAgentCoordinator(
            self.app.subagent_store,
            ModelCatalog({self.profile.name: self.profile}, default_profile=self.profile.name),
            logger=self.app.logger,
        )
        self.app.fleet_client = SimpleNamespace()
        entry = EntryDecision.parse(decision("workflow"))
        packet = ContextPacket(self.scope.key, "group:101:user:201", 201, -1, entry.contract.objective,
                               supporting_context="frozen original evidence")
        return dict(intent={"tool": "run_subagents", "goal": packet.objective}, packet=packet,
                    decision=entry, dispatch={"bot_id": "301", "profile": self.profile.name})

    def child_model_mocks(self):
        async def worker(*args, **kwargs):
            budget = active_model_budget()
            self.assertIsNotNone(budget)
            await budget.before_request()
            _, request = budget.prepare(self.profile, {"messages": [], "max_tokens": 30})
            request.finish({"prompt_tokens": 30, "completion_tokens": 10})
            return json.dumps({"status": "success", "summary": "verified step", "findings": [],
                               "completed": [], "authorization": [], "next_verification": []})

        worker_mock = patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                            new=AsyncMock(side_effect=worker)).start()
        patch("src.plugins.ai_chat.subagents.ask_deepseek",
              new=AsyncMock(return_value="Child evidence summary")).start()
        patch.object(self.app.subagent_coordinator, "_validate_workflow",
                     new=AsyncMock(return_value={"status": "passed", "checks": [], "artifacts": []})).start()
        return worker_mock

    async def test_owned_child_uses_real_workflow_and_reuses_terminal_receipt(self):
        _, run, job = self.running()
        inputs = self.child_inputs()
        worker = self.child_model_mocks()

        async def agent(*args, **kwargs):
            execution = kwargs["routine_execution"]
            options = dict(selected_profile=self.profile, tools=[], execute_tool=AsyncMock())
            first = await execution.tool("run_subagents", inputs["intent"],
                lambda: run_child(execution, **inputs, **options))
            second = await execution.tool("run_subagents", inputs["intent"],
                lambda: run_child(execution, **inputs, **options))
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "completed")
            self.assertIn("Child evidence summary", first["answer"])
            return first["answer"]

        self.services.tools._ask_ai.side_effect = agent
        result = await self.dispatcher.execute(job)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(worker.await_count, 3)
        children = self.store.child_receipts(job)
        self.assertEqual(len(children), 1)
        self.assertEqual(self.app.subagent_store.dispatchable_tasks(), [])
        self.assertEqual(self.app.subagent_store.finalizable_tasks(), [])
        self.assertEqual(self.store.recovery_receipts(job), [])
        self.assertEqual(len(self.outbox.recent()), 1)
        self.assertEqual(self.store.get_run(run.run_id).delivery_id, result["delivery_id"])
        self.assertIsNone(active_job_fence.get())

    async def test_child_preview_receipt_is_in_parent_outbox_even_if_synthesis_omits_it(self):
        from src.plugins.ai_chat.agent.external import ExternalCalls
        from src.plugins.ai_chat.subagents import TaskStep

        _, run, job = self.running()
        inputs = self.child_inputs()
        url = "http://worker.test/previews/preview_" + "a" * 32 + "/"

        async def agent(*args, **kwargs):
            execution = kwargs["routine_execution"]
            child = self.store.prepare_child(job, execution.run.attempt_id,
                subagents=self.app.subagent_store, **inputs, max_parallelism=2, max_steps=4)
            step = self.app.subagent_store.create_run(child.task_id,
                TaskStep("publish", "operator", "publish", "url"), allowed_tools=[], model_profile=self.profile.name)
            tracker = ExternalCalls(self.app.subagent_store, child.task_id, step.run_id)
            tracker.call_id, tracker.tool_name = "preview-call", "cluster_job_submit"
            tracker.arguments = {"kind": "preview.static", "payload": {"artifact_id": "artifact_" + "b" * 32}}

            async def remote(body):
                return {**body, "job_id": "job_preview", "status": "succeeded",
                    "actor_id": "qq:201", "origin_scope": self.scope.key,
                    "payload": {**body["payload"], "preview_id": "preview_" + "a" * 32,
                                "expires_at": int(time.time()) + 3600}, "result": {"public_url": url}}

            await tracker.request("POST", "/v1/jobs", tracker.arguments,
                actor="qq:201", origin=self.scope.key, perform=remote)
            self.app.subagent_store.set_task_state(child.task_id, "partial", result={"answer": "Mobile check pending"})
            return "Preview published, mobile check pending"

        self.services.tools._ask_ai.side_effect = agent
        result = await self.dispatcher.execute(job)
        delivery = self.outbox.recent()[0]
        self.assertIn(url, str(delivery.body))
        self.assertIn("mobile check pending", str(delivery.body))
        self.assertEqual(delivery.target_scope.key, self.scope.key)
        self.assertEqual(self.store.get_run(run.run_id).delivery_id, result["delivery_id"])
        self.assertEqual(len(self.outbox.recent()), 1)

    async def test_takeover_resumes_frozen_child_before_parent_synthesis(self):
        _, run, job = self.running()
        inputs = self.child_inputs()
        worker = self.child_model_mocks()
        attempt = self.store.begin_attempt(job)
        child = self.store.prepare_child(job, attempt.attempt_id, subagents=self.app.subagent_store,
                                        max_parallelism=2, max_steps=4, **inputs)
        replacement = self.reclaim(job)
        order = []

        async def agent(*args, **kwargs):
            if kwargs.get("resume_task_id"):
                order.append("child")
                return await resume_child(kwargs["routine_execution"], kwargs["resume_task_id"],
                    selected_profile=self.profile, tools=[], execute_tool=AsyncMock())
            order.append("parent")
            self.assertIn("Child evidence summary", kwargs["turn_context"])
            self.assertIn('"status": "completed"', kwargs["turn_context"])
            return "Recovered parent answer"

        self.services.tools._ask_ai.side_effect = agent
        await self.dispatcher.execute(replacement)
        self.assertEqual(order, ["child", "parent"])
        self.assertEqual(worker.await_count, 3)
        self.assertTrue(all("frozen original evidence" in call.args[0] for call in worker.await_args_list))
        self.assertEqual(self.store.child_receipts(replacement)[0]["task_id"], child.task_id)
        self.assertEqual(len(self.store.child_receipts(replacement)), 1)
        self.assertEqual(self.store.get_run(run.run_id).status, "enqueued")

    async def test_interrupted_real_child_keeps_completed_steps_on_takeover(self):
        from src.plugins.ai_chat.agent.execution import active_agent_step

        _, _, job = self.running()
        inputs = self.child_inputs()
        original_worker = self.child_model_mocks()
        first_done = asyncio.Event()
        calls = []

        async def interrupt_once(*args, **kwargs):
            step = active_agent_step.get().split("/")[-1]
            calls.append(step)
            if step == "backend" and calls.count(step) == 1:
                await first_done.wait()
                raise asyncio.CancelledError
            answer = await original_worker(*args, **kwargs)
            if step == "frontend":
                first_done.set()
            return answer

        async def agent(*args, **kwargs):
            options = dict(selected_profile=self.profile, tools=[], execute_tool=AsyncMock())
            if kwargs.get("resume_task_id"):
                return await resume_child(kwargs["routine_execution"], kwargs["resume_task_id"], **options)
            if kwargs.get("turn_context"):
                return "Recovered without repeating completed steps"
            return (await run_child(kwargs["routine_execution"], **inputs, **options))["answer"]

        self.services.tools._ask_ai.side_effect = agent
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=AsyncMock(side_effect=interrupt_once)):
            with self.assertRaises(asyncio.CancelledError):
                await self.dispatcher.execute(job)
            child = self.store.child_receipts(job)[0]
            self.assertEqual(child["status"], "interrupted")
            self.assertFalse(self.outbox.recent())
            replacement = self.reclaim(job)
            await self.dispatcher.execute(replacement)
        self.assertEqual(calls.count("frontend"), 1)
        self.assertEqual(calls.count("backend"), 2)
        self.assertEqual(calls.count("integration"), 1)
        self.assertEqual(self.store.child_receipts(replacement)[0]["task_id"], child["task_id"])
        self.assertEqual(self.store.child_receipts(replacement)[0]["status"], "completed")
        self.assertEqual(len(self.outbox.recent()), 1)

    async def test_pending_child_defers_whole_parent_without_unknown_side_effect(self):
        _, run, job = self.running()
        inputs = self.child_inputs()

        async def agent(*args, **kwargs):
            execution = kwargs["routine_execution"]
            return await execution.tool("run_subagents", inputs["intent"],
                lambda: run_child(execution, **inputs, selected_profile=self.profile,
                                  tools=[], execute_tool=AsyncMock()))

        async def wait_external(task_id, **kwargs):
            self.app.subagent_store.set_task_state(task_id, "waiting_external")
            return "waiting"

        self.services.tools._ask_ai.side_effect = agent
        with patch.object(self.app.subagent_coordinator, "resume", new=AsyncMock(side_effect=wait_external)):
            with self.assertRaises(JobDeferred):
                await self.dispatcher.execute(job)
        self.assertEqual(self.store.get_run(run.run_id).status, "running")
        self.assertEqual(self.store.child_receipts(job)[0]["status"], "waiting_external")
        self.assertEqual(self.store.recovery_receipts(job), [])
        self.assertFalse(self.outbox.recent())
        self.assertIsNone(active_job_fence.get())
        turn = self.journal.get_turn_by_id(self.store.get_run(run.run_id).turn_id)
        self.assertEqual(turn.status, "aborted")

    async def test_owned_revision_reuses_task_without_extending_parent_deadline(self):
        from src.plugins.ai_chat.agent.background import SubAgentDispatcher

        _, _, job = self.running()
        inputs = self.child_inputs()
        worker = self.child_model_mocks()
        coordinator = self.app.subagent_coordinator
        coordinator.dispatcher = SubAgentDispatcher(self.services)

        async def agent(*args, **kwargs):
            execution = kwargs["routine_execution"]
            options = dict(selected_profile=self.profile, tools=[], execute_tool=AsyncMock())
            first = await run_child(execution, **inputs, **options)
            control = self.app.subagent_store.control(first["task_id"])
            revised = await self.app.db_call(coordinator.revise, first["task_id"],
                scope_key=self.scope.key, requester_user_id=201, instruction="Refine frontend",
                step_keys=["frontend"], expected_version=control["version"],
                contract=inputs["decision"].contract.as_payload())
            self.assertEqual(revised["dispatch"]["deadline"], execution.run.deadline_at)
            return await resume_child(execution, first["task_id"], **options)

        self.services.tools._ask_ai.side_effect = agent
        await self.dispatcher.execute(job)
        self.assertEqual(worker.await_count, 5)  # Two revised steps, not another three-step task.
        self.assertEqual(len(self.store.child_receipts(job)), 1)
        child_id = self.store.child_receipts(job)[0]["task_id"]
        self.assertEqual(self.app.subagent_store.control(child_id)["revision"], 2)
        self.assertEqual(self.jobs._connection.execute(
            "SELECT count(*) AS n FROM durable_jobs WHERE kind='subagent.workflow'").fetchone()["n"], 0)
        self.assertEqual(len(self.outbox.recent()), 1)

    async def test_external_receipt_wait_does_not_execute_child_again(self):
        _, _, job = self.running()
        inputs = self.child_inputs()

        async def agent(*args, **kwargs):
            execution = kwargs["routine_execution"]
            child = self.store.prepare_child(job, execution.run.attempt_id,
                subagents=self.app.subagent_store, max_parallelism=2, max_steps=4, **inputs)
            with (patch.object(self.app.subagent_store, "external_calls", return_value=[{"status": "pending"}]),
                  patch("src.plugins.ai_chat.routine_subagents.poll_external", new=AsyncMock(return_value=False)) as poll,
                  patch.object(self.app.subagent_coordinator, "resume", new=AsyncMock()) as resume):
                with self.assertRaises(RoutineChildPending):
                    await resume_child(execution, child.task_id)
                poll.assert_awaited_once()
                resume.assert_not_awaited()
            self.assertEqual(self.app.subagent_store.get(child.task_id).status, "waiting_external")
            # Simulate the host suspending the parent instead of synthesizing a final answer.
            raise RoutineChildPending("pending receipt")

        self.services.tools._ask_ai.side_effect = agent
        with self.assertRaises(JobDeferred):
            await self.dispatcher.execute(job)
        self.assertFalse(self.outbox.recent())

    async def test_scheduled_turn_cannot_resume_unowned_task(self):
        _, _, job = self.running()
        inputs = self.child_inputs()
        manual = self.app.subagent_store.prepare_task(**{k: v for k, v in inputs.items() if k != "intent"},
                                                     max_parallelism=2, max_steps=4)

        async def agent(*args, **kwargs):
            with self.assertRaises(PermissionError):
                await resume_child(kwargs["routine_execution"], manual.task_id)
            return "No unauthorized work"

        self.services.tools._ask_ai.side_effect = agent
        await self.dispatcher.execute(job)
        self.assertEqual(self.app.subagent_store.get(manual.task_id).status, "queued")

    async def test_real_queue_to_normal_agent_to_original_scope_outbox(self):
        routine, run, job = self.running()

        async def agent(bot, event, question, **kwargs):
            self.assertEqual(
                (bot.self_id, event.self_id, event.user_id, event.group_id),
                ("301", 301, 201, 101),
            )
            self.assertLess(event.message_id, 0)
            self.assertEqual(question, routine.prompt)
            self.assertEqual(kwargs["routine_execution"].run.run_id, run.run_id)
            budget = active_model_budget()
            await budget.before_request()
            _, request = budget.prepare(
                self.profile, {"messages": [], "max_tokens": 30}
            )
            request.finish({"prompt_tokens": 30, "completion_tokens": 10})
            return Message(MessageSegment.text("Verified answer"))

        self.services.tools._ask_ai.side_effect = agent
        result = await self.dispatcher.execute(job)
        delivery = self.outbox.get(result["delivery_id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(delivery.target_scope.key, self.scope.key)
        self.assertEqual(delivery.reply_to_native_message_id, "")
        self.assertEqual(self.store.get_run(run.run_id).status, "enqueued")
        budget = self.usage.routine_status(
            routine.routine_id, scope_key=self.scope.key, bot_id="301", creator_id="201"
        )
        self.assertEqual(budget["charged_tokens"], 40)
        self.assertIsNone(active_model_budget())

    async def test_quiet_result_survives_takeover_without_message_or_model(self):
        _, run, job = self.running()
        self.services.tools._ask_ai.return_value = "[silence]"
        result = await self.dispatcher.execute(job)
        self.assertEqual(result, {"state": "quiet", "delivery_id": None})
        self.assertEqual(self.store.get_run(run.run_id).status, "quiet")
        replacement = self.reclaim(job)
        self.get_bot.side_effect = KeyError("offline")
        self.assertEqual(await self.dispatcher.execute(replacement), {"state": "quiet", "recovered": True})
        self.services.tools._ask_ai.assert_awaited_once()
        self.assertFalse(self.outbox.recent())

    async def test_completed_turn_and_usage_survive_failure_before_queueing(self):
        routine, run, job = self.running()

        async def agent(*args, **kwargs):
            budget = active_model_budget()
            _, request = budget.prepare(self.profile, {"messages": [], "max_tokens": 30})
            request.finish({"prompt_tokens": 30, "completion_tokens": 10})
            kwargs["turn_trace"].trace_id = "a" * 32
            return "Saved evidence"

        self.services.tools._ask_ai.side_effect = agent
        with patch.object(self.store, "queue_result", side_effect=RuntimeError("outbox interrupted")):
            with self.assertRaisesRegex(RuntimeError, "outbox interrupted"):
                await self.dispatcher.execute(job)
        saved = self.store.get_run(run.run_id)
        self.assertEqual(saved.status, "generated")
        turn = self.journal.get_turn_by_id(saved.turn_id)
        self.assertEqual((turn.status, turn.final_text, turn.total_tokens), ("succeeded", "Saved evidence", 40))
        self.assertEqual(self.journal.recent_trace_summaries()[0]["trace_id"], "a" * 32)
        self.journal.recover_interrupted()
        self.assertEqual(self.journal.recovered_crashed_turns, 0)
        replacement = self.reclaim(job)
        self.get_bot.side_effect = KeyError("offline")
        first = await self.dispatcher.execute(replacement)
        second = await self.dispatcher.execute(replacement)
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(len(self.outbox.recent()), 1)
        self.services.tools._ask_ai.assert_awaited_once()
        usage = self.usage.routine_status(routine.routine_id, scope_key=self.scope.key,
                                         creator_id="201", bot_id="301")
        self.assertEqual((usage["attempts"], usage["charged_tokens"]), (1, 40))

    async def test_failed_execution_is_not_reported_successful_after_delivery_takeover(self):
        _, run, job = self.running()
        self.services.tools._ask_ai.side_effect = ModelBudgetExceeded()
        with patch.object(self.store, "queue_result", side_effect=RuntimeError("queue unavailable")):
            with self.assertRaises(RuntimeError):
                await self.dispatcher.execute(job)
        saved = self.store.get_run(run.run_id)
        self.journal.recover_interrupted()
        replacement = self.reclaim(job)
        await self.dispatcher.execute(replacement)
        turn = self.journal.get_turn_by_id(saved.turn_id)
        self.assertEqual(turn.status, "aborted")
        self.assertIn("预算", turn.final_text)
        self.assertEqual(len(self.outbox.recent()), 1)
        self.services.tools._ask_ai.assert_awaited_once()

    async def test_quiet_commit_survives_lost_response_without_reopening_turn(self):
        _, run, job = self.running()
        self.services.tools._ask_ai.return_value = "[silence]"
        complete = self.store.complete_execution

        def lose_response(*args, **kwargs):
            complete(*args, **kwargs)
            raise RuntimeError("commit response lost")

        with patch.object(self.store, "complete_execution", side_effect=lose_response):
            with self.assertRaisesRegex(RuntimeError, "commit response lost"):
                await self.dispatcher.execute(job)
        saved = self.store.get_run(run.run_id)
        self.assertEqual(saved.status, "quiet")
        self.journal.recover_interrupted()
        replacement = self.reclaim(job)
        self.get_bot.side_effect = KeyError("offline")
        self.assertEqual(await self.dispatcher.execute(replacement), {"state": "quiet", "recovered": True})
        turn = self.journal.get_turn_by_id(saved.turn_id)
        self.assertEqual((turn.status, turn.final_text), ("silence", ""))
        self.assertEqual(self.journal.recovered_crashed_turns, 0)
        self.services.tools._ask_ai.assert_awaited_once()
        self.assertFalse(self.outbox.recent())

    async def test_journal_error_rolls_back_output_and_terminal_status_together(self):
        for answer in ("Saved answer", "[silence]"):
            with self.subTest(answer=answer):
                postgres_for_test(self, reset=True)
                _, run, job = self.running()
                self.services.tools._ask_ai.return_value = answer
                finish = self.journal.finish_turn

                def fail_after_write(*args, **kwargs):
                    self.assertIsNotNone(kwargs.get("transaction_cursor"))
                    finish(*args, **kwargs)
                    raise RuntimeError("journal write interrupted")

                with patch.object(self.journal, "finish_turn", side_effect=fail_after_write):
                    with self.assertRaisesRegex(RuntimeError, "journal write interrupted"):
                        await self.dispatcher.execute(job)
                saved = self.store.get_run(run.run_id)
                self.assertEqual((saved.status, saved.body_json), ("running", ""))
                turn = self.journal.get_turn_by_id(saved.turn_id)
                self.assertEqual((turn.status, turn.final_text), ("running", ""))
                self.assertFalse(self.outbox.recent())
                with self.journal._lock:
                    self.assertEqual(self.journal._connection.execute(
                        "SELECT count(*) AS n FROM turn_archives WHERE turn_id=?", (saved.turn_id,)
                    ).fetchone()["n"], 0)

    async def test_completion_rejects_foreign_turn_and_stale_owner(self):
        _, _, job = self.running()
        attempt = self.store.begin_attempt(job)
        turn = self.journal.start_turn(self.scope, trigger_canonical_message_id=None,
            objective="routine", provider="test", model="test")
        self.store.bind_turn(job, attempt.attempt_id, turn.turn_id)
        foreign = self.journal.start_turn(ConversationScope("onebot-v11", "group", "999"),
            trigger_canonical_message_id=None, objective="other", provider="test", model="test")
        options = dict(journal=self.journal, status="succeeded",
                       body=decode_onebot_message(Message("result")).body)
        with self.assertRaises(ValueError):
            self.store.complete_execution(job, attempt.attempt_id, turn_id=foreign.turn_id, **options)
        replacement = self.reclaim(job)
        for invalid in (job, replace(replacement, scope_key=foreign.scope_key)):
            with self.assertRaises(RoutineLeaseLost):
                self.store.complete_execution(invalid, attempt.attempt_id, turn_id=turn.turn_id, **options)
        self.assertEqual(self.journal.get_turn_by_id(turn.turn_id).status, "running")
        self.assertEqual(self.journal.get_turn_by_id(foreign.turn_id).status, "running")
        self.assertEqual(self.store.get_run(attempt.run_id).status, "running")

    async def test_silence_text_does_not_discard_prepared_media(self):
        _, run, job = self.running()
        self.services.tools._ask_ai.return_value = Message([
            MessageSegment.text("[silence]"), MessageSegment.image("base64://aGVsbG8=")])
        result = await self.dispatcher.execute(job)
        self.assertEqual(result["state"], "completed")
        self.assertIsNotNone(result["delivery_id"])
        self.assertEqual(self.store.get_run(run.run_id).status, "enqueued")

    async def test_departed_creator_disables_routine_without_spending_tokens(self):
        routine, run, job = self.running()
        self.bot.get_group_member_list.return_value = [{"user_id": 999}]
        self.assertEqual(await self.dispatcher.execute(job), {"state": "cancelled", "reason": "creator_left"})
        self.assertEqual(self.store.get_run(run.run_id).status, "cancelled")
        self.assertFalse(self.store.list_owned(self.scope)[0].enabled)
        self.assertEqual(self.usage.routine_status(routine.routine_id, scope_key=self.scope.key,
                         creator_id="201", bot_id="301")["attempts"], 0)
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_unavailable_roster_defers_instead_of_assuming_membership(self):
        _, run, job = self.running()
        self.bot.get_group_member_list.side_effect = TimeoutError
        with self.assertRaises(JobDeferred):
            await self.dispatcher.execute(job)
        self.assertEqual(self.store.get_run(run.run_id).status, "queued")
        self.assertTrue(self.store.list_owned(self.scope)[0].enabled)
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_membership_rechecked_before_side_effect_during_long_run(self):
        _, run, job = self.running()
        action = AsyncMock(return_value="sent")

        async def agent(*args, **kwargs):
            self.bot.get_group_member_list.return_value = []
            execution = kwargs["routine_execution"]
            execution._membership_checked_at = 0
            return await execution.tool("say", {"text": "late"}, action)

        self.services.tools._ask_ai.side_effect = agent
        with self.assertRaises(asyncio.CancelledError):
            await self.dispatcher.execute(job)
        action.assert_not_awaited()
        self.assertEqual(self.store.get_run(run.run_id).status, "cancelled")
        self.assertFalse(self.outbox.recent())

    async def test_saved_result_recovered_without_model_or_online_account(self):
        _, _, job = self.running()
        run = self.store.begin_attempt(job)
        self.store.save_result(
            job, run.attempt_id, decode_onebot_message(Message("saved answer")).body
        )
        replacement = self.reclaim(job)
        self.get_bot.side_effect = KeyError("offline")
        first = await self.dispatcher.execute(replacement)
        second = await self.dispatcher.execute(replacement)
        self.assertTrue(first["recovered"])
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(len(self.outbox.recent()), 1)
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_offline_and_disabled_group_defer_before_budget_reservation(self):
        routine, run, job = self.running()
        self.get_bot.side_effect = KeyError("offline")
        with self.assertRaises(JobDeferred):
            await self.dispatcher.execute(job)
        self.get_bot.side_effect = None
        self.services.group_enabled.return_value = False
        with self.assertRaises(JobDeferred):
            await self.dispatcher.execute(job)
        self.assertEqual(self.store.get_run(run.run_id).status, "queued")
        budget = self.usage.routine_status(
            routine.routine_id, scope_key=self.scope.key, bot_id="301", creator_id="201"
        )
        self.assertEqual(budget["attempts"], 0)

    async def test_connected_but_logged_out_account_does_not_spend_model_budget(self):
        _, run, job = self.running()
        self.bot.call_api.return_value = {"online": False}
        with self.assertRaises(JobDeferred):
            await self.dispatcher.execute(job)
        self.assertEqual(self.store.get_run(run.run_id).attempt_id, "")
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_current_group_and_cancel_are_fenced_before_every_tool(self):
        routine, _, job = self.running()
        run = self.store.begin_attempt(job)
        execution = RoutineExecution(self.services, job, run)
        action = AsyncMock(return_value="done")
        self.services.group_enabled.return_value = False
        with self.assertRaises(LeaseLost):
            await execution.tool("web_search", {}, action)
        self.services.group_enabled.return_value = True
        self.store.cancel(self.scope, routine.routine_id, expected_version=1)
        with self.assertRaises(LeaseLost):
            await execution.tool("web_search", {}, action)
        action.assert_not_awaited()

    async def test_completed_side_effect_not_repeated_after_takeover(self):
        _, _, job = self.running()
        run = self.store.begin_attempt(job)
        action = AsyncMock(return_value='{"ok":true,"receipt":"sent-1"}')
        execution = RoutineExecution(self.services, job, run)
        original = await execution.tool("say", {"text": "hello"}, action)
        replacement = self.reclaim(job)
        next_run = self.store.begin_attempt(replacement)
        next_execution = RoutineExecution(self.services, replacement, next_run)
        repeated = await next_execution.tool("say", {"text": "hello"}, action)
        self.assertEqual(original, repeated)
        action.assert_awaited_once()
        self.assertEqual(len(self.store.recovery_receipts(replacement)), 1)
        with self.assertRaises(RoutineLeaseLost):
            self.store.claim_effect(job, run.attempt_id, "say", {"text": "late"})

    async def test_pending_side_effect_aborts_takeover_without_model_or_repeat(self):
        _, run, job = self.running()
        attempt = self.store.begin_attempt(job)
        self.store.claim_effect(job, attempt.attempt_id, "say", {"text": "unknown"})
        replacement = self.reclaim(job)
        result = await self.dispatcher.execute(replacement)
        self.assertEqual(result["state"], "failed")
        self.services.tools._ask_ai.assert_not_awaited()
        self.assertIn("执行回执", self.store.get_run(run.run_id).body_json)

    async def test_crash_after_effect_before_receipt_is_not_assumed_failed(self):
        _, _, job = self.running()
        run = self.store.begin_attempt(job)
        execution = RoutineExecution(self.services, job, run)
        action = AsyncMock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            await execution.tool("say", {"text": "hello"}, action)
        with self.assertRaises(RoutineEffectUncertain):
            await execution.tool("say", {"text": "hello"}, action)
        action.assert_awaited_once()

    async def test_read_tools_can_repeat_and_do_not_create_effect_claims(self):
        _, _, job = self.running()
        run = self.store.begin_attempt(job)
        execution = RoutineExecution(self.services, job, run)
        action = AsyncMock(side_effect=["first", "second"])
        self.assertEqual(await execution.tool("web_search", {}, action), "first")
        self.assertEqual(await execution.tool("web_search", {}, action), "second")
        self.assertEqual(self.store.recovery_receipts(job), [])

    async def test_budget_and_timeout_produce_honest_durable_failure_reply(self):
        for error, text in ((ModelBudgetExceeded(), "预算"), (TimeoutError(), "超时")):
            with self.subTest(error=type(error).__name__):
                postgres_for_test(self, reset=True)
                _, run, job = self.running()
                self.services.tools._ask_ai.side_effect = error
                result = await self.dispatcher.execute(job)
                self.assertEqual(result["state"], "failed")
                self.assertIn(text, self.store.get_run(run.run_id).body_json)
                self.assertEqual(len(self.outbox.recent()), 1)

    async def test_unknown_model_failure_keeps_attempt_for_durable_worker_retry(self):
        _, run, job = self.running()
        self.services.tools._ask_ai.side_effect = RuntimeError("model unavailable")
        with self.assertRaises(RuntimeError):
            await self.dispatcher.execute(job)
        self.assertEqual(self.store.get_run(run.run_id).status, "running")
        self.assertFalse(self.outbox.recent())
        turn = self.journal.get_turn_by_id(self.store.get_run(run.run_id).turn_id)
        self.assertEqual(turn.status, "crashed")

    async def test_cancelled_attempt_closes_its_turn_without_claiming_completion(self):
        _, run, job = self.running()
        self.services.tools._ask_ai.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.dispatcher.execute(job)
        turn = self.journal.get_turn_by_id(self.store.get_run(run.run_id).turn_id)
        self.assertEqual(turn.status, "aborted")
        self.assertFalse(self.outbox.recent())

    async def test_expired_run_is_not_restarted_even_while_qq_is_offline(self):
        routine, run, job = self.running()
        self.store.begin_attempt(job, now=int(time.time()) - 40, timeout_seconds=30)
        replacement = self.reclaim(job)
        self.get_bot.side_effect = KeyError("offline")
        result = await self.dispatcher.execute(replacement)
        self.assertEqual(result["reason"], "execution_deadline")
        self.get_bot.assert_not_called()
        self.services.tools._ask_ai.assert_not_awaited()
        self.assertIsNotNone(self.store.get_run(run.run_id).delivery_id)
        self.assertEqual(self.usage.routine_status(routine.routine_id, scope_key=self.scope.key,
            bot_id="301", creator_id="201")["attempts"], 1)

    async def test_terminal_job_and_budget_notices_are_idempotent(self):
        _, run, job = self.running(max_tokens=2000, daily_tokens=2000)
        self.store.begin_attempt(job)
        replacement = self.reclaim(job)
        result = await self.dispatcher.execute(replacement)
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(self.store.queue_terminal_notices(), 1)
        self.assertEqual(self.store.queue_terminal_notices(), 0)
        self.assertIsNotNone(self.store.get_run(run.run_id).delivery_id)

    async def test_failed_notice_queue_recovers_without_new_attempt_or_model(self):
        routine, run, job = self.running()
        self.store.begin_attempt(job, now=int(time.time()) - 40, timeout_seconds=30)
        with patch.object(self.store, "queue_terminal_notices", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaises(RuntimeError):
                await self.dispatcher.execute(job)
        self.assertEqual(self.store.get_run(run.run_id).status, "failed")
        replacement = self.reclaim(job)
        self.get_bot.side_effect = KeyError("offline")
        for _ in range(2):
            result = await self.dispatcher.execute(replacement)
            self.assertEqual(result, {"state": "failed", "recovered": True})
        self.assertEqual(len(self.outbox.recent()), 1)
        self.assertEqual(self.usage.routine_status(routine.routine_id, scope_key=self.scope.key,
            bot_id="301", creator_id="201")["attempts"], 1)
        self.get_bot.assert_not_called()
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_budget_skipped_run_recovers_even_with_offline_qq(self):
        _, run, job = self.running(max_tokens=2000, daily_tokens=2000)
        self.store.begin_attempt(job)
        replacement = self.reclaim(job)
        self.store.begin_attempt(replacement)
        self.assertEqual(self.store.get_run(run.run_id).status, "skipped")
        next_job = self.reclaim(replacement)
        self.get_bot.side_effect = KeyError("offline")
        self.assertEqual(await self.dispatcher.execute(next_job), {"state": "skipped", "recovered": True})
        self.assertEqual(len(self.outbox.recent()), 1)
        self.get_bot.assert_not_called()
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_recovery_rejects_wrong_envelope_and_stale_lease_before_notifying(self):
        _, _, job = self.running()
        self.store.begin_attempt(job, now=int(time.time()) - 40, timeout_seconds=30)
        self.store.begin_attempt(job)
        replacement = self.reclaim(job)
        for invalid in (job, replace(replacement, scope_key="onebot-v11:group:999"),
                        replace(replacement, kind="other"), replace(replacement, job_id=999999)):
            with self.subTest(job=invalid):
                with self.assertRaises(RoutineLeaseLost):
                    await self.dispatcher.execute(invalid)
        self.assertFalse(self.outbox.recent())
        self.services.tools._ask_ai.assert_not_awaited()

    async def test_wrong_job_scope_and_old_attempt_cannot_persist_receipts(self):
        _, _, job = self.running()
        run = self.store.begin_attempt(job)
        with self.assertRaises(RoutineLeaseLost):
            self.store.claim_effect(
                replace(job, scope_key="onebot-v11:group:999"),
                run.attempt_id,
                "say",
                {},
            )
        with self.assertRaises(RoutineLeaseLost):
            self.store.claim_effect(job, "wrong-attempt", "say", {})
        key, _ = self.store.claim_effect(job, run.attempt_id, "say", {})
        self.store.finish_effect(job, run.attempt_id, key, "first")
        with self.assertRaises(ValueError):
            self.store.finish_effect(job, run.attempt_id, key, "changed")

    async def test_private_event_has_no_fake_group_and_correct_owner(self):
        self.scope = ConversationScope("onebot-v11", "private", "201", "201", "301")
        _, run, _ = self.running()
        event = routine_event(run)
        self.assertEqual(event.message_type, "private")
        self.assertEqual(event.user_id, 201)
        self.assertFalse(hasattr(event, "group_id"))

    async def test_cached_reply_preparation_restores_images_and_voice(self):
        _, run, _ = self.running()
        context = ToolContext(self.services, routine_event(run), frozenset())
        turn = TurnState(context)
        turn.visual_reply_segment = MessageSegment.image("base64://aGVsbG8=")
        turn.voice_reply_segment = MessageSegment.record("voice-file")
        turn.voice_reply_text = "prepared"
        turn.subagent_task_started = True
        turn.subagent_delegations = 2
        restored = TurnState(context)
        restored.restore_reply_checkpoint(
            json.loads(json.dumps(turn.reply_checkpoint()))
        )
        self.assertEqual(restored.visual_reply_segment, turn.visual_reply_segment)
        self.assertEqual(restored.voice_reply_segment, turn.voice_reply_segment)
        self.assertEqual(restored.voice_reply_text, "prepared")
        self.assertTrue(restored.subagent_task_started)

    async def test_effect_claim_serializes_concurrent_duplicate_writes(self):
        _, _, job = self.running()
        run = self.store.begin_attempt(job)
        execution = RoutineExecution(self.services, job, run)
        started, release = asyncio.Event(), asyncio.Event()

        async def action():
            started.set()
            await release.wait()
            return "sent"

        first = asyncio.create_task(execution.tool("say", {"text": "hello"}, action))
        await started.wait()
        try:
            with self.assertRaises(RoutineEffectUncertain):
                await execution.tool("say", {"text": "hello"}, action)
        finally:
            release.set()
            self.assertEqual(await first, "sent")


class RoutineNormalPipelineTests(
    HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase
):
    async def test_all_delegation_entrypoints_use_parent_owned_execution(self):
        from tests.test_natural_tool_routing import _group_event
        from src.plugins.ai_chat.onebot_codec import scope_from_event
        from src.plugins.ai_chat.agent import DEFAULT_AGENT_REGISTRY

        event = _group_event()
        bot = AsyncMock()
        bot.self_id = str(event.self_id)

        async def guarded(name, arguments, action):
            return await action()

        execution = SimpleNamespace(run=SimpleNamespace(run_id="scheduled-run"),
                                    guard=AsyncMock(), tool=AsyncMock(side_effect=guarded))
        coordinator = SimpleNamespace(
            dispatcher=Mock(), max_steps=8, registry=DEFAULT_AGENT_REGISTRY,
            prepare_entry=AsyncMock(return_value=EntryDecision.parse(decision("workflow"))),
            allowed_model_profiles=Mock(return_value=("test",)), entry_profile=Mock(return_value=None),
            submit=Mock(), delegate=AsyncMock(), run=AsyncMock(), resume=AsyncMock(),
        )
        config = replace(self.app_context.settings, subagent_entry_enabled=False,
                         tool_intent_enabled=False, search_enabled=False, voice_enabled=False, ocr_enabled=False)
        task_store = SimpleNamespace(revisable_tasks=Mock(return_value=[]),
                                     get=Mock(return_value=SimpleNamespace(project_id=None)))
        for name, arguments in (("delegate_agent", {"role": "coder", "objective": "work"}),
                                ("run_subagents", {"goal": "work"}), ("resume_subagent", {"task_id": 7}),
                                ("auto-delegate", {}), ("auto-workflow", {})):
            with self.subTest(entry=name):
                async def model(_text, _history, tools, execute_tool, **kwargs):
                    if name.startswith("auto-"):
                        await kwargs["entry_handler"](EntryDecision.parse(decision(name.removeprefix("auto-"))))
                    else:
                        self.assertIn(name, [tool["function"]["name"] for tool in tools])
                        reply = json.loads(await execute_tool(name, arguments))
                        self.assertTrue(reply["ok"])
                    return "done"

                with (patch.object(self.app_context, "settings", replace(config, subagent_entry_enabled=name.startswith("auto-"))),
                      patch.object(self.app_context, "subagent_coordinator", coordinator),
                      patch.object(self.app_context, "subagent_store", task_store),
                      patch("src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools", new=AsyncMock(side_effect=model)),
                      patch("src.plugins.ai_chat.routine_subagents.run_child", new=AsyncMock(return_value={
                          "task_id": 7, "status": "completed", "answer": "child answer"})) as create,
                      patch("src.plugins.ai_chat.routine_subagents.resume_child", new=AsyncMock(return_value="continued")) as resume):
                    await self.handlers.tools._ask_ai(bot, event, "work", available_image_sources=[],
                                                      routine_execution=execution)
                if name == "resume_subagent":
                    resume.assert_awaited_once()
                    self.assertEqual(resume.await_args.args, (execution, 7))
                    create.assert_not_awaited()
                else:
                    create.assert_awaited_once()
                    packet = create.await_args.kwargs["packet"]
                    self.assertEqual(packet.scope_key, scope_from_event(event).key)
                    self.assertEqual(packet.requester_user_id, event.user_id)
                    self.assertIs(create.await_args.args[0], execution)
                    resume.assert_not_awaited()
        coordinator.submit.assert_not_called()
        coordinator.delegate.assert_not_awaited()
        coordinator.run.assert_not_awaited()
        coordinator.resume.assert_not_awaited()

    async def test_normal_tool_executor_uses_routine_guard_and_restores_cached_reply(
        self,
    ):
        from tests.test_natural_tool_routing import _group_event

        receipts = {}

        async def guarded(name, arguments, action):
            key = (name, json.dumps(arguments, sort_keys=True))
            if key not in receipts:
                receipts[key] = await action()
            return receipts[key]

        execution = SimpleNamespace(
            run=SimpleNamespace(run_id="scheduled-run"),
            tool=AsyncMock(side_effect=guarded),
        )
        self.app_context.routine_store = Mock()
        self.addCleanup(setattr, self.app_context, "routine_store", None)

        async def model(question, history, tools, execute_tool, **kwargs):
            names = {tool["function"]["name"] for tool in tools}
            self.assertNotIn("routine_set", names)
            self.assertNotIn("routine_cancel", names)
            result = json.loads(
                await execute_tool("send_qq_face", {"expression": "微笑"})
            )
            self.assertTrue(result["ok"])
            return "Prepared"

        with patch(
            "src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools",
            new=AsyncMock(side_effect=model),
        ):
            for _ in range(2):
                answer = await self.handlers.tools._ask_ai(
                    AsyncMock(),
                    _group_event(),
                    "Send a face",
                    available_image_sources=[],
                    routine_execution=execution,
                )
                self.assertIsInstance(answer, Message)
                self.assertTrue(any(segment.type == "face" for segment in answer))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(execution.tool.await_count, 2)
