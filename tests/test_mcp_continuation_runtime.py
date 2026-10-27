from __future__ import annotations

from dataclasses import replace
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.bot_storage import DatabaseCalls
from src.bot_security.service import MobileAuthorization
from src.plugins.ai_chat.agent_loop import ask_deepseek_with_tools
from src.plugins.ai_chat.mcp_bridge.continuation_runtime import (
    McpApprovalPending, McpChatCheckpoint, McpContinuationDispatcher,
    McpContinuationExecution, McpDeferred, continuation_event,
)
from src.plugins.ai_chat.mcp_bridge.continuations import McpEffectUncertain
from src.plugins.ai_chat.mcp_bridge.service import McpService
from src.plugins.ai_chat.model_budget import ModelBudgetExceeded
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.tools.mcp import McpTool
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from tests import test_mcp_continuations as fixtures
from tests.handler_fixture import HandlerTestCaseMixin
from tests.test_llm_gateway import profile


def response(content="done", calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,
        tool_calls=[SimpleNamespace(id=item["id"], type="function",
            function=SimpleNamespace(**item["function"])) for item in calls]))], usage=None)


def definition(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


class McpContinuationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.McpContinuationTests.setUp
    setUpCalls = fixtures.McpContinuationTests.setUpCalls
    policy = fixtures.McpContinuationTests.policy
    prepare = fixtures.McpContinuationTests.prepare
    approval = fixtures.McpContinuationTests.approval
    claim = fixtures.McpContinuationTests.claim
    reopen = fixtures.McpContinuationTests.reopen
    complete_remote = fixtures.McpContinuationTests.complete_remote
    ready_job = fixtures.McpContinuationTests.ready_job
    reclaimed = fixtures.McpContinuationTests.reclaimed
    pending_effect = fixtures.McpContinuationTests.pending_effect

    async def asyncSetUp(self):
        self.app = SimpleNamespace(db_call=DatabaseCalls(),
            job_worker=DurableJobWorker(self.jobs, logger=Mock()))
        self.addAsyncCleanup(self.app.db_call.close)
        self.ask = AsyncMock(return_value="saved result")
        selected = profile("test")
        self.profile = replace(selected, provider="test-provider", model="test-model")
        self.app.model_catalog = SimpleNamespace(try_resolve=Mock(return_value=self.profile))
        self.runtime = SimpleNamespace(app=self.app, store=self.store,
            scope_allowed=AsyncMock(return_value=True), handlers=SimpleNamespace(tools=SimpleNamespace(_ask_ai=self.ask)))
        self.app.mcp_runtime = self.runtime
        self.dispatcher = McpContinuationDispatcher(self.runtime)
        bot_patch = patch("src.plugins.ai_chat.mcp_bridge.continuation_runtime.get_bot",
            return_value=SimpleNamespace(self_id=fixtures.BOT))
        availability = patch("src.plugins.ai_chat.mcp_bridge.continuation_runtime.delivery_blocker",
            new=AsyncMock(return_value=None))
        bot_patch.start()
        availability.start()
        self.addCleanup(bot_patch.stop)
        self.addCleanup(availability.stop)

    def execution(self, job):
        state, call = self.store.execution(job)
        return McpContinuationExecution(self.runtime, job, state, call)

    def attach(self, **kwargs):
        kwargs.setdefault("profile", {"name": self.profile.name, "provider": self.profile.provider_identity,
                                      "model": self.profile.model})
        return fixtures.McpContinuationTests.attach(self, **kwargs)

    async def test_tool_approval_receipt_then_restart_uses_same_continuation_and_outbox(self):
        sent = []
        async def sender(*args):
            sent.append(args)
        async def remote(_config, _definition, _arguments, *, before_call):
            await before_call()
            return {"ok": True, "content": "remote receipt"}
        wire = SimpleNamespace(call=AsyncMock(side_effect=remote))
        mobile = MobileAuthorization(self.security, sender=sender, bot_selector=lambda: fixtures.BOT)
        service = McpService(self.store, wire, scope_allowed=self.runtime.scope_allowed, mobile=mobile,
                             db_call=self.app.db_call)
        mobile.executors["mcp_tool"] = service.execute_approved
        self.app.mcp_service = service
        event = continuation_event({"checkpoint": {"message_time": 999, "message_id": 123, "question": "work"}},
            {"scope_key": fixtures.GROUP, "bot_id": fixtures.BOT, "user_id": fixtures.QQ})
        turn = TurnState(ToolContext(SimpleNamespace(context=self.app), event, frozenset({"mcp"})),
                         bot=SimpleNamespace(self_id=fixtures.BOT), user_text="work")
        turn.mcp_checkpoint = McpChatCheckpoint(self.app, turn, self.profile)
        turn.mcp_checkpoint.capture(self.messages)
        server, tool = self.store.resolve("example", "lookup", fixtures.GROUP)
        with self.assertRaises(McpApprovalPending):
            await McpTool(server, tool, fixtures.GROUP).run(turn, {"query": "hello"})
        wire.call.assert_not_awaited()
        self.assertTrue(turn.mcp_checkpoint.prepared)
        approval = self.security.pending(self.admin["account_id"])[0]
        code = re.search(r"一次性口令：(\d{6})", sent[0][2]).group(1)
        self.security.confirm(approval["approval_id"], fixtures.QQ, fixtures.BOT, code)
        self.assertFalse(await mobile.run_once())
        review, code = self.security.request_review(approval["approval_id"], self.peer["qq_id"], fixtures.BOT)
        self.security.delivered(review["approval_id"], review["code_generation"], True)
        self.security.confirm(review["approval_id"], self.peer["qq_id"], fixtures.BOT, code)
        self.assertTrue(await mobile.run_once())
        wire.call.assert_awaited_once()
        self.assertEqual(self.reopen().materialize_continuations(), 1)
        job = self.jobs.claim_due("worker", now=self.now)[0]
        first = await self.dispatcher.execute(job)
        second = await self.dispatcher.execute(self.reclaimed(job))
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.ask.assert_awaited_once()
        wire.call.assert_awaited_once()

    async def test_dispatcher_replays_original_event_and_enqueues_once(self):
        job = self.ready_job()
        first = await self.dispatcher.execute(job)
        delivery = self.outbox.get(first["delivery_id"])
        self.assertEqual(delivery.reply_to_native_message_id, "123")
        event = self.ask.await_args.args[1]
        self.assertEqual((event.message_id, event.user_id, event.self_id),
                         (123, int(fixtures.QQ), int(fixtures.BOT)))
        self.assertEqual(self.ask.await_args.kwargs["_mcp_execution"].state["model_calls"], 0)
        again = await self.dispatcher.execute(self.reclaimed(job))
        self.assertEqual(again["delivery_id"], first["delivery_id"])
        self.ask.assert_awaited_once()

    async def test_offline_and_unverifiable_group_defer_without_model_or_delivery(self):
        job = self.ready_job()
        self.runtime.scope_allowed.return_value = False
        with self.assertRaises(JobDeferred):
            await self.dispatcher.execute(job)
        self.ask.assert_not_awaited()
        self.assertEqual(self.store.execution(job)[0]["status"], "queued")
        self.runtime.scope_allowed.return_value = True
        with patch("src.plugins.ai_chat.mcp_bridge.continuation_runtime.get_bot", side_effect=KeyError()):
            with self.assertRaises(JobDeferred):
                await self.dispatcher.execute(job)
        self.ask.assert_not_awaited()

    async def test_defer_escapes_the_ordinary_tool_error_handler(self):
        job = self.ready_job()
        self.pending_effect(job)
        execution = self.execution(job)
        self.runtime.scope_allowed.return_value = False
        action = AsyncMock()
        with self.assertRaises(McpDeferred):
            await execution.tool("send_file", {"query": "hello"}, action)
        action.assert_not_awaited()

    async def test_unknown_effect_stops_and_queues_truthful_incomplete_message(self):
        job = self.ready_job()
        self.pending_effect(job)
        self.store.begin_effect(job, "next", "send_file", {"query": "hello"}, idempotency="non-idempotent")
        result = await self.dispatcher.execute(self.reclaimed(job))
        self.assertTrue(result["incomplete"])
        self.assertIn("缺少执行回执", str(self.outbox.get(result["delivery_id"]).body))
        self.ask.assert_not_awaited()

    async def test_model_change_is_not_silently_applied_to_approved_task(self):
        job = self.ready_job()
        self.app.model_catalog.try_resolve.return_value = replace(self.profile, model="different")
        result = await self.dispatcher.execute(job)
        self.assertTrue(result["incomplete"])
        self.ask.assert_not_awaited()

    async def test_budget_stop_and_second_approval_are_not_generic_failures(self):
        job = self.ready_job()
        self.ask.side_effect = McpApprovalPending()
        with self.assertRaises(JobDeferred):
            await self.dispatcher.execute(job)
        self.assertEqual(self.store.execution(job)[0]["status"], "queued")
        self.ask.side_effect = ModelBudgetExceeded("budget")
        result = await self.dispatcher.execute(job)
        self.assertTrue(result["incomplete"])

    async def test_reply_preparation_is_restored_without_repeating_effect_after_restart(self):
        job = self.ready_job()
        messages, _ = self.pending_effect(job)
        execution = self.execution(job)
        turn = TurnState(ToolContext(SimpleNamespace(context=self.app), continuation_event(execution.state, execution.call), frozenset()))
        execution.turn = turn
        async def send():
            turn.voice_reply_text = "caption retained"
            return '{"sent":true}'
        action = AsyncMock(side_effect=send)
        result = await execution.tool("send_file", {"query": "hello"}, action)
        recovered = self.execution(self.reclaimed(job))
        restored = TurnState(turn.context)
        recovered.turn = restored
        self.assertEqual(await recovered.tool("send_file", {"query": "hello"}, action), result)
        self.assertEqual(restored.voice_reply_text, "caption retained")
        action.assert_awaited_once()
        await recovered.checkpoint([*messages, {"role": "tool", "tool_call_id": "next", "content": result}])
        self.assertEqual(recovered.state["reply"]["voice_text"], "caption retained")

    async def test_receipt_write_failure_does_not_allow_further_mutations(self):
        job = self.ready_job()
        self.pending_effect(job)
        action = AsyncMock(return_value="sent")
        with patch.object(self.store, "finish_effect", side_effect=RuntimeError("database unavailable")):
            with self.assertRaises(McpEffectUncertain):
                await self.execution(job).tool("send_file", {"query": "hello"}, action)
        self.assertEqual(self.store.uncertain_effects(job), ["send_file"])

    async def test_keyed_action_is_not_assumed_repeatable_without_its_receipt(self):
        job = self.ready_job()
        self.pending_effect(job, "delegate_agent")
        action = AsyncMock(return_value="child receipt")
        await self.execution(job).tool("delegate_agent", {"query": "hello"}, action)
        await self.execution(self.reclaimed(job)).tool("delegate_agent", {"query": "hello"}, action)
        action.assert_awaited_once()

    async def test_real_loop_resumes_pending_tool_without_new_model_decision(self):
        job = self.ready_job()
        self.pending_effect(job)
        action = AsyncMock(return_value='{"sent":true}')
        async def ordinary_loop(_bot, _event, question, **kwargs):
            execution = kwargs["_mcp_execution"]
            async def execute(name, arguments):
                return await execution.tool(name, arguments, action)
            return await ask_deepseek_with_tools(question, [], [definition("send_file")], execute,
                profile=self.profile, resume_messages=execution.state["messages"], checkpoint_sink=execution.checkpoint)
        self.ask.side_effect = ordinary_loop
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock(return_value=response())) as model:
            result = await self.dispatcher.execute(job)
        action.assert_awaited_once()
        model.assert_awaited_once()
        self.assertIn('"sent":true', model.await_args.kwargs["messages"][-1]["content"])
        self.assertEqual(self.store.execution(job)[0]["messages"][-1]["content"], "done")
        self.assertIsNotNone(self.outbox.get(result["delivery_id"]))


class McpResumeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_turn_tool_calls_do_not_consume_this_turn_or_block_new_request(self):
        saved = [{"role": "user", "content": "yesterday"},
                 {"role": "assistant", "tool_calls": [fixtures.tool_call("old", "send_file")]},
                 {"role": "tool", "tool_call_id": "old", "content": '{"sent":true}'},
                 {"role": "assistant", "content": "sent yesterday"},
                 {"role": "user", "content": "send again today"}]
        execute = AsyncMock(return_value='{"sent":true}')
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock(side_effect=[
                response("", [fixtures.tool_call("new", "send_file")]), response()])):
            await ask_deepseek_with_tools("send again today", [], [definition("send_file")], execute,
                profile=profile("test"), resume_messages=saved, resume_history_length=5)
        execute.assert_awaited_once()

    async def test_final_checkpoint_is_returned_without_model_or_tools(self):
        execute = AsyncMock()
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock()) as model:
            answer = await ask_deepseek_with_tools("question", [], [], execute, profile=profile("test"),
                resume_messages=[{"role": "user", "content": "original"}, {"role": "assistant", "content": "saved final"}])
        self.assertEqual(answer, "saved final")
        execute.assert_not_awaited()
        model.assert_not_awaited()

    async def test_deferred_batch_item_is_not_mistaken_for_executed_side_effect(self):
        saved = [{"role": "user", "content": "work"}, {"role": "assistant", "tool_calls": [fixtures.tool_call("old", "send_file")]},
                 {"role": "tool", "tool_call_id": "old", "content": '{"code":"deferred_batch_not_executed","executed":false}'}]
        execute = AsyncMock(return_value='{"sent":true}')
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock(side_effect=[
                response("", [fixtures.tool_call("new", "send_file")]), response()])):
            await ask_deepseek_with_tools("work", [], [definition("send_file")], execute,
                profile=profile("test"), resume_messages=saved)
        execute.assert_awaited_once()

    async def test_model_cannot_reissue_completed_mutation_with_new_call_id(self):
        saved = [{"role": "user", "content": "work"}, {"role": "assistant", "tool_calls": [fixtures.tool_call("old", "send_file")]},
                 {"role": "tool", "tool_call_id": "old", "content": '{"sent":true}'}]
        execute = AsyncMock()
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock(side_effect=[
                response("", [fixtures.tool_call("new", "send_file")]), response()])):
            await ask_deepseek_with_tools("work", [], [definition("send_file")], execute,
                profile=profile("test"), resume_messages=saved)
        execute.assert_not_awaited()

    async def test_checkpoint_failure_prevents_tool_dispatch(self):
        execute = AsyncMock()
        saved = [{"role": "user", "content": "work"}, {"role": "assistant", "tool_calls": [fixtures.tool_call("pending", "send_file")]}]
        with self.assertRaises(RuntimeError):
            await ask_deepseek_with_tools("work", [], [definition("send_file")], execute,
                profile=profile("test"), resume_messages=saved, checkpoint_sink=AsyncMock(side_effect=RuntimeError("disk")))
        execute.assert_not_awaited()


class McpNormalPipelineTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def test_resume_skips_entry_replanning_and_preserves_reply_and_checkpoint_hooks(self):
        from tests.test_natural_tool_routing import _group_event

        messages = [{"role": "user", "content": "original question"}]
        execution = SimpleNamespace(state={"reply": {}, "messages": messages,
            "checkpoint": {"system_context": "original stable context", "history_length": 0}}, checkpoint=AsyncMock())
        async def guarded(name, arguments, action):
            return await action()
        execution.tool = AsyncMock(side_effect=guarded)

        async def model(_question, _history, _tools, execute, **kwargs):
            self.assertIsNone(kwargs["entry_handler"])
            self.assertEqual(kwargs["system_context"], "original stable context")
            self.assertIs(kwargs["checkpoint_sink"], execution.checkpoint)
            self.assertEqual(kwargs["resume_messages"], messages)
            self.assertEqual(kwargs["resume_history_length"], 0)
            kwargs["transcript_sink"](messages)
            result = json.loads(await execute("send_qq_face", {"expression": "微笑"}))
            self.assertTrue(result["ok"])
            return "done"

        with (patch.object(self.app_context, "mcp_runtime", SimpleNamespace(store=Mock())),
              patch("src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools", new=AsyncMock(side_effect=model)),
              patch.object(self.app_context.memory, "append_turn") as memory):
            answer = await self.handlers.tools._ask_ai(AsyncMock(), _group_event(), "work",
                available_image_sources=[], _mcp_execution=execution)
        self.assertTrue(any(item.type == "face" for item in answer))
        self.assertEqual(execution.turn.mcp_checkpoint.messages, messages)
        execution.tool.assert_awaited_once()
        memory.assert_not_called()

    async def test_initial_pause_is_a_waiting_reply_not_a_model_error(self):
        from tests.test_natural_tool_routing import _group_event

        with patch("src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools", new=AsyncMock(side_effect=McpApprovalPending("approval saved"))):
            answer = await self.handlers.tools._ask_ai(AsyncMock(), _group_event(), "work", available_image_sources=[])
        self.assertIn("approval saved", str(answer))

    async def test_checkpoint_frame_commits_before_marking_prepared(self):
        event = SimpleNamespace(group_id=123, self_id=456, user_id=789, message_id=10, time=20)
        store = SimpleNamespace(attach_checkpoint=Mock())
        app = SimpleNamespace(mcp_runtime=SimpleNamespace(store=store), db_call=AsyncMock(side_effect=OSError("offline")))
        turn = TurnState(ToolContext(SimpleNamespace(context=app), event, frozenset()), user_text="question")
        frame = McpChatCheckpoint(app, turn, profile("test"))
        frame.capture([{"role": "user", "content": "question"}])
        with self.assertRaises(OSError):
            await frame.prepare({"call_id": "call"})
        self.assertFalse(frame.prepared)

    async def test_checkpoint_origin_stays_at_first_capture_despite_new_feedback(self):
        app = SimpleNamespace()
        turn = TurnState(ToolContext(SimpleNamespace(context=app), Mock(), frozenset()))
        frame = McpChatCheckpoint(app, turn, profile("test"))
        frame.capture([{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"},
                       {"role": "user", "content": "current"}])
        self.assertEqual(frame.history_length, 3)
        frame.capture([*frame.messages, {"role": "assistant", "content": "answer"},
                       {"role": "user", "content": "host feedback"}])
        self.assertEqual(frame.history_length, 3)


if __name__ == "__main__":
    unittest.main()
