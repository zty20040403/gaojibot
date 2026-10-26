from __future__ import annotations

import json
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

os.environ.setdefault("AI_ALLOW_LEGACY_SQLITE", "true")
os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.plugins.ai_chat.subagents import (
    AGENT_SPECS,
    SubAgentCoordinator,
    SubAgentStore,
    TaskStep,
    _validate_plan,
    _worker_prompt,
    parse_profile_overrides,
    route_subagent_request,
)
from src.plugins.ai_chat.agent import AgentResult, ContextPacket, DEFAULT_AGENT_REGISTRY
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.tools import available_tools
from src.plugins.ai_chat.tools.state import ToolContext


class SubAgentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SubAgentStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_task_run_events_and_result_are_durable(self) -> None:
        changed_tasks: list[int] = []
        self.store.set_change_listener(changed_tasks.append)
        task = self.store.create_task(
            scope_key="qq:group:123",
            conversation_id="group:123:user:456",
            requester_user_id=456,
            trigger_message_id=12,
            objective="查资料并写报告",
            max_parallelism=3,
            max_steps=8,
            now=100,
        )
        step = TaskStep(
            key="research",
            role="researcher",
            objective="核实资料",
            deliverable="带来源的事实",
        )
        run = self.store.create_run(
            task.task_id,
            step,
            allowed_tools=["web_search"],
            model_profile="deepseek",
            now=101,
        )
        self.assertTrue(self.store.start_run(run.run_id, now=102))
        self.assertTrue(
            self.store.finish_run(
                run.run_id,
                "succeeded",
                result={"summary": "完成"},
                now=103,
            )
        )
        self.assertTrue(
            self.store.set_task_state(
                task.task_id,
                "completed",
                result={"answer": "报告"},
                now=104,
            )
        )

        stored = self.store.get(task.task_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.result["answer"], "报告")
        self.assertEqual(self.store.runs(task.task_id)[0].status, "succeeded")
        events = self.store.events(task.task_id)
        self.assertGreaterEqual(len(events), 5)
        self.assertIn("run.running", [item["event_type"] for item in events])
        self.assertEqual(changed_tasks, [task.task_id] * len(events))
        self.assertEqual(self.store.events(task.task_id, limit=3), events[-3:])

    def test_cancel_marks_active_task(self) -> None:
        task = self.store.create_task(
            scope_key="qq:group:1",
            conversation_id="group:1:user:2",
            requester_user_id=2,
            trigger_message_id=None,
            objective="长任务",
            max_parallelism=2,
            max_steps=4,
        )
        self.store.set_task_state(task.task_id, "running")
        self.assertTrue(self.store.request_cancel(task.task_id))
        self.assertTrue(self.store.cancellation_requested(task.task_id))
        self.assertEqual(self.store.get(task.task_id).status, "cancelling")  # type: ignore[union-attr]

    def test_restart_preserves_runs_for_checkpoint_recovery(self) -> None:
        task = self.store.create_task(
            scope_key="qq:group:1",
            conversation_id="group:1:user:2",
            requester_user_id=2,
            trigger_message_id=None,
            objective="重启中的任务",
            max_parallelism=2,
            max_steps=4,
        )
        first = self.store.create_run(
            task.task_id,
            TaskStep("first", "researcher", "检索", "资料"),
            allowed_tools=[],
            model_profile="test",
        )
        self.store.create_run(
            task.task_id,
            TaskStep("second", "document", "写报告", "PDF", ("first",)),
            allowed_tools=[],
            model_profile="test",
        )
        self.store.set_task_state(task.task_id, "running")
        self.store.start_run(first.run_id)

        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.get(task.task_id).status, "interrupted")  # type: ignore[union-attr]
        self.assertEqual(
            [run.status for run in self.store.runs(task.task_id)],
            ["interrupted", "pending"],
        )
        checkpoint = self.store.checkpoints(task.task_id)[-1]
        self.assertEqual(checkpoint["phase"], "process_interrupted")
        self.assertEqual(checkpoint["state"]["interrupted_runs"], [first.run_id])

    def test_checkpoints_are_ordered_and_emit_events(self) -> None:
        task = self.store.create_task(
            scope_key="qq:group:1",
            conversation_id="group:1:user:2",
            requester_user_id=2,
            trigger_message_id=None,
            objective="持久任务",
            max_parallelism=2,
            max_steps=4,
        )
        self.store.append_checkpoint(task.task_id, "received", {"value": 1}, now=101)
        self.store.append_checkpoint(task.task_id, "planned", {"value": 2}, now=102)

        checkpoints = self.store.checkpoints(task.task_id)
        self.assertEqual([item["sequence"] for item in checkpoints], [1, 2])
        self.assertEqual([item["state"]["value"] for item in checkpoints], [1, 2])
        self.assertEqual(
            [
                item["event_type"]
                for item in self.store.events(task.task_id)
                if item["event_type"] == "checkpoint.created"
            ],
            ["checkpoint.created", "checkpoint.created"],
        )

    def test_retry_is_blocked_after_non_idempotent_side_effect(self) -> None:
        task = self.store.create_task(
            scope_key="qq:group:1",
            conversation_id="group:1:user:2",
            requester_user_id=2,
            trigger_message_id=None,
            objective="发送文件",
            max_parallelism=1,
            max_steps=1,
        )
        run = self.store.create_run(
            task.task_id,
            TaskStep("send", "document", "发送", "文件"),
            allowed_tools=["send_file_from_sandbox"],
            model_profile="test",
        )
        self.store.append_event(
            task.task_id,
            "agent.tool_finished",
            {
                "idempotency": "non-idempotent",
                "state": "succeeded",
            },
            run_id=run.run_id,
        )
        self.assertFalse(self.store.run_retry_safe(run.run_id))


class SubAgentPlanTests(unittest.TestCase):
    def test_validates_fixed_role_dag(self) -> None:
        steps = _validate_plan(
            {
                "steps": [
                    {
                        "id": "read",
                        "agent": "media",
                        "depends_on": [],
                        "objective": "读取视频",
                        "deliverable": "字幕",
                    },
                    {
                        "id": "report",
                        "agent": "document",
                        "depends_on": ["read"],
                        "objective": "生成报告",
                        "deliverable": "PDF",
                    },
                ]
            },
            "分析视频并生成报告",
            8,
        )
        self.assertEqual([step.role for step in steps], ["media", "document"])
        self.assertEqual(steps[1].dependencies, ("read",))

    def test_rejects_cycle(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "循环依赖"):
            _validate_plan(
                {
                    "steps": [
                        {
                            "id": "a",
                            "agent": "researcher",
                            "depends_on": ["b"],
                            "objective": "A",
                        },
                        {
                            "id": "b",
                            "agent": "analyst",
                            "depends_on": ["a"],
                            "objective": "B",
                        },
                    ]
                },
                "循环",
                8,
            )

    def test_profiles_only_accept_known_roles(self) -> None:
        self.assertEqual(
            parse_profile_overrides('{"coder":"gpt","media":"luna"}'),
            {"coder": "gpt", "media": "luna"},
        )
        with self.assertRaisesRegex(ValueError, "Unknown Sub-Agent role"):
            parse_profile_overrides('{"random":"gpt"}')

    def test_first_version_has_seven_fixed_roles(self) -> None:
        self.assertEqual(
            set(AGENT_SPECS),
            {
                "supervisor",
                "researcher",
                "coder",
                "document",
                "media",
                "analyst",
                "operator",
            },
        )

    def test_operator_prompt_separates_readonly_scope_from_execution_permissions(self) -> None:
        prompt = _worker_prompt(DEFAULT_AGENT_REGISTRY.worker("operator"))
        for requirement in ("UID、PATH", "必需程序和目标目录可读性", "本任务审批和执行次数约束",
                            "du -B1", "du -b", "不能当作目录总量"):
            self.assertIn(requirement, prompt)

    def test_automatic_subagent_tool_is_exposed_on_request(self) -> None:
        tools = available_tools(ToolContext(
            SimpleNamespace(), SimpleNamespace(), frozenset({"subagents"}),
        ))
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("delegate_agent", names)
        self.assertIn("run_subagents", names)
        self.assertIn("resume_subagent", names)

    def test_agent_manifest_is_versioned_and_policy_aware(self) -> None:
        researcher = next(
            item
            for item in DEFAULT_AGENT_REGISTRY.manifest()
            if item["role"] == "researcher"
        )
        self.assertEqual(researcher["version"], 1)
        self.assertEqual(researcher["model_policy"], "fast")
        self.assertEqual(researcher["risk_level"], "read-only")

    def test_context_packet_is_bounded_and_scope_explicit(self) -> None:
        packet = ContextPacket(
            scope_key="qq:group:123",
            conversation_id="group:123:user:456",
            requester_user_id=456,
            trigger_message_id=12,
            objective="核实资料",
            conversation_context="群聊" * 5000,
            memory_context="记忆" * 5000,
            supporting_context="补充" * 5000,
            evidence_handles=("msg#12",),
        )
        rendered = packet.render_for_worker("researcher", upstream={}, max_chars=7000)
        self.assertLessEqual(len(rendered), 7000)
        self.assertIn("qq:group:123", rendered)
        self.assertIn("msg#12", rendered)

    def test_each_role_receives_an_independent_minimal_context(self) -> None:
        packet = ContextPacket(
            scope_key="qq:group:123",
            conversation_id="group:123:user:456",
            requester_user_id=456,
            trigger_message_id=12,
            objective="核实资料并写程序",
            conversation_context="当前群聊",
            memory_context="不应下发的个人记忆",
            supporting_context="任务补充材料",
            evidence_handles=("msg#12",),
            artifact_handles=("sandbox#1:/workspace/input.csv",),
        )
        researcher = packet.for_agent(
            DEFAULT_AGENT_REGISTRY.worker("researcher"),
            upstream={},
        )
        coder = packet.for_agent(
            DEFAULT_AGENT_REGISTRY.worker("coder"),
            upstream={"research": {"summary": "已核实"}},
        )

        self.assertNotEqual(researcher.context_hash, coder.context_hash)
        self.assertNotIn("不应下发的个人记忆", researcher.rendered_context)
        self.assertNotIn("不应下发的个人记忆", coder.rendered_context)
        self.assertNotIn("sandbox#1", researcher.rendered_context)
        self.assertIn("sandbox#1", coder.rendered_context)
        self.assertNotIn("已核实", researcher.rendered_context)
        self.assertIn("已核实", coder.rendered_context)

    def test_agent_result_keeps_structured_evidence(self) -> None:
        result = AgentResult.from_payload(
            {
                "status": "completed",
                "summary": "完成",
                "facts": [{"claim": "事实", "evidence_ids": ["web#1"]}],
                "confidence": 2,
            }
        ).as_payload()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["facts"][0]["evidence_ids"], ["web#1"])
        self.assertEqual(result["confidence"], 1.0)

    def test_routes_multi_source_media_report_automatically(self) -> None:
        decision = route_subagent_request(
            "查一下这款产品的详细参数，对比三个来源，再整理成 PDF 发到群里",
            has_media=True,
        )
        self.assertTrue(decision.delegate)
        self.assertIn("multi_source_artifact", decision.reasons)
        self.assertEqual(
            set(decision.domains),
            {"analysis", "document", "media", "research"},
        )

    def test_does_not_route_single_tool_requests(self) -> None:
        for request, has_media in (
            ("看看这张图片", True),
            ("查一下今天天气", False),
            ("分析这段话", False),
            ("帮我写一份 PDF", False),
        ):
            with self.subTest(request=request):
                self.assertFalse(
                    route_subagent_request(request, has_media=has_media).delegate
                )


class SubAgentCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_child_runs_before_slow_sibling_finishes(self) -> None:
        store = SubAgentStore(":memory:")
        self.addCleanup(store.close)
        profile = ModelProfile(name="test", provider="test", protocol="openai-chat", model="test", base_url="http://127.0.0.1", api_key_required=False)
        coordinator = SubAgentCoordinator(store, ModelCatalog({"test": profile}, default_profile="test"), logger=Mock(), max_parallelism=2)
        slow_started, child_started = asyncio.Event(), asyncio.Event()
        order, active, peak = [], 0, 0
        plan = {"steps": [
            {"id": "fast", "agent": "researcher", "objective": "FAST", "depends_on": []},
            {"id": "slow", "agent": "researcher", "objective": "SLOW", "depends_on": []},
            {"id": "child", "agent": "analyst", "objective": "CHILD", "depends_on": ["fast"]},
        ]}

        async def work(text, *_args, **_kwargs):
            nonlocal active, peak
            name = text.split("[你的步骤]\n", 1)[1].split("\n\n", 1)[0]
            active += 1
            peak = max(peak, active)
            order.append(name + " start")
            try:
                if name == "FAST":
                    await slow_started.wait()
                elif name == "SLOW":
                    slow_started.set()
                    await child_started.wait()
                else:
                    child_started.set()
                return '{"status":"success","summary":"done"}'
            finally:
                active -= 1
                order.append(name + " end")

        with (
            patch("src.plugins.ai_chat.subagents.ask_deepseek_json", new=AsyncMock(return_value=plan)),
            patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=work),
            patch("src.plugins.ai_chat.subagents.ask_deepseek", new=AsyncMock(return_value="done")),
        ):
            await asyncio.wait_for(coordinator.run(
                scope_key="qq:group:1", conversation_id="group:1:user:2",
                requester_user_id=2, trigger_message_id=3, objective="compare",
                context="", selected_profile=profile, tools=[], execute_tool=AsyncMock(),
            ), timeout=2)
        self.assertLess(order.index("CHILD start"), order.index("SLOW end"))
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        task = store.recent(limit=1)[0]
        self.assertEqual(task.status, "completed")
        self.assertTrue(any(c["phase"] == "step_completed" for c in store.checkpoints(task.task_id)))

    async def test_parent_cancellation_awaits_all_in_flight_workers(self) -> None:
        store = SubAgentStore(":memory:")
        self.addCleanup(store.close)
        profile = ModelProfile(name="test", provider="test", protocol="openai-chat", model="test", base_url="http://127.0.0.1", api_key_required=False)
        coordinator = SubAgentCoordinator(store, ModelCatalog({"test": profile}, default_profile="test"), logger=Mock(), max_parallelism=2)
        started, active, stopped = asyncio.Event(), 0, 0
        plan = {"steps": [
            {"id": name, "agent": "researcher", "objective": name, "depends_on": []}
            for name in ("a", "b")
        ]}
        async def work(*_args, **_kwargs):
            nonlocal active, stopped
            active += 1
            if active == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                stopped += 1
        with (
            patch("src.plugins.ai_chat.subagents.ask_deepseek_json", new=AsyncMock(return_value=plan)),
            patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=work),
        ):
            task = asyncio.create_task(coordinator.run(
                scope_key="qq:group:1", conversation_id="group:1:user:2",
                requester_user_id=2, trigger_message_id=3, objective="compare",
                context="", selected_profile=profile, tools=[], execute_tool=AsyncMock(),
            ))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((active, stopped), (0, 2))

    async def test_failed_step_gets_one_bounded_adaptive_repair(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        coordinator = SubAgentCoordinator(
            store,
            ModelCatalog({"test": profile}, default_profile="test"),
            logger=AsyncMock(),
        )
        plan = {
            "goal": "核实数据",
            "steps": [
                {
                    "id": "research",
                    "agent": "researcher",
                    "depends_on": [],
                    "objective": "读取来源",
                    "deliverable": "事实",
                }
            ],
        }
        repair = {
            "action": "repair",
            "role": "analyst",
            "objective": "根据失败证据换一种方式核对",
            "deliverable": "可核验结论",
            "reason": "原来源不可用",
        }
        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_json",
                new=AsyncMock(side_effect=[plan, repair]),
            ),
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(
                    side_effect=[
                        '{"status":"failed","summary":"来源不可用"}',
                        '{"status":"success","summary":"换源核实完成"}',
                    ]
                ),
            ) as worker,
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek",
                new=AsyncMock(return_value="已换源核实"),
            ),
        ):
            answer = await coordinator.run(
                scope_key="qq:group:1",
                conversation_id="group:1:user:2",
                requester_user_id=2,
                trigger_message_id=3,
                objective="核实数据",
                context="必要上下文",
                selected_profile=profile,
                tools=[],
                execute_tool=AsyncMock(return_value='{"ok":true}'),
            )

        self.assertIn("已换源核实", answer)
        self.assertEqual(worker.await_count, 2)
        task = store.recent(limit=1)[0]
        runs = store.runs(task.task_id)
        self.assertEqual([run.role for run in runs], ["researcher", "analyst"])
        self.assertEqual([run.status for run in runs], ["failed", "succeeded"])
        self.assertEqual(task.status, "completed")
        self.assertEqual(len(store.run_contexts(task.task_id)), 2)
        self.assertIn("adaptive_steps", task.plan)
        store.close()

    async def test_transient_worker_failure_retries_with_same_context(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        coordinator = SubAgentCoordinator(
            store,
            ModelCatalog({"test": profile}, default_profile="test"),
            logger=AsyncMock(),
        )
        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(
                    side_effect=[
                        RuntimeError("network timeout"),
                        '{"status":"success","summary":"恢复正常"}',
                    ]
                ),
            ) as worker,
            patch(
                "src.plugins.ai_chat.subagents.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await coordinator.delegate(
                role="researcher",
                scope_key="qq:group:1",
                conversation_id="group:1:user:2",
                requester_user_id=2,
                trigger_message_id=3,
                objective="核实资料",
                context="冻结上下文",
                selected_profile=profile,
                tools=[],
                execute_tool=AsyncMock(return_value='{"ok":true}'),
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(worker.await_count, 2)
        task = store.recent(limit=1)[0]
        self.assertEqual(store.runs(task.task_id)[0].attempt, 2)
        self.assertEqual(len(store.run_contexts(task.task_id)), 1)
        self.assertIn(
            "run.retry_scheduled",
            [item["event_type"] for item in store.events(task.task_id)],
        )
        store.close()

    async def test_resume_reuses_frozen_context_and_skips_completed_steps(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        coordinator = SubAgentCoordinator(
            store,
            ModelCatalog({"test": profile}, default_profile="test"),
            logger=AsyncMock(),
        )
        task = store.create_task(
            scope_key="qq:group:1",
            conversation_id="group:1:user:2",
            requester_user_id=2,
            trigger_message_id=3,
            objective="先检索再分析",
            max_parallelism=2,
            max_steps=2,
        )
        first_step = TaskStep("research", "researcher", "检索", "事实")
        second_step = TaskStep(
            "analysis",
            "analyst",
            "分析",
            "结论",
            ("research",),
        )
        plan = {
            "goal": task.objective,
            "mode": "workflow",
            "steps": [
                {"id": "research", "agent": "researcher"},
                {"id": "analysis", "agent": "analyst"},
            ],
        }
        store.set_task_state(task.task_id, "running", plan=plan)
        packet = ContextPacket.from_legacy(
            scope_key=task.scope_key,
            conversation_id=task.conversation_id,
            requester_user_id=task.requester_user_id,
            trigger_message_id=task.trigger_message_id,
            objective=task.objective,
            context="重启前冻结的上下文",
        )
        store.append_checkpoint(
            task.task_id,
            "plan_ready",
            {"mode": "workflow", "plan": plan, "context_packet": packet.as_payload()},
        )
        first = store.create_run(
            task.task_id,
            first_step,
            allowed_tools=[],
            model_profile="test",
        )
        store.start_run(first.run_id)
        store.finish_run(
            first.run_id,
            "succeeded",
            result={"status": "success", "summary": "已检索"},
        )
        second = store.create_run(
            task.task_id,
            second_step,
            allowed_tools=[],
            model_profile="test",
        )
        frozen = packet.for_agent(
            DEFAULT_AGENT_REGISTRY.worker("analyst"),
            upstream={"research": {"status": "success", "summary": "已检索"}},
        )
        store.save_run_context(task.task_id, second.run_id, frozen)
        store.start_run(second.run_id)
        self.assertEqual(store.recover_interrupted(), 1)

        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(
                    return_value='{"status":"success","summary":"已分析"}'
                ),
            ) as worker,
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek",
                new=AsyncMock(return_value="恢复完成"),
            ),
        ):
            answer = await coordinator.resume(
                task.task_id,
                scope_key=task.scope_key,
                requester_user_id=task.requester_user_id,
                selected_profile=profile,
                tools=[],
                execute_tool=AsyncMock(return_value='{"ok":true}'),
            )

        self.assertIn("恢复完成", answer)
        self.assertEqual(worker.await_count, 1)
        self.assertIn("重启前冻结的上下文", worker.await_args.args[0])
        runs = store.runs(task.task_id)
        self.assertEqual([run.attempt for run in runs], [1, 2])
        self.assertEqual([run.status for run in runs], ["succeeded", "succeeded"])
        self.assertEqual(store.get(task.task_id).status, "completed")  # type: ignore[union-attr]
        store.close()

    async def test_single_agent_delegation_skips_planner_and_synthesizer(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        catalog = ModelCatalog({"test": profile}, default_profile="test")
        coordinator = SubAgentCoordinator(store, catalog, logger=AsyncMock())
        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(
                    return_value=json.dumps(
                        {
                            "status": "success",
                            "summary": "查到一手来源",
                            "facts": [
                                {"claim": "参数已确认", "evidence_ids": ["web#1"]}
                            ],
                            "citations": ["https://example.com/source"],
                        },
                        ensure_ascii=False,
                    )
                ),
            ) as worker,
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_json",
                new=AsyncMock(),
            ) as planner,
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek",
                new=AsyncMock(),
            ) as synthesizer,
        ):
            result = await coordinator.delegate(
                role="researcher",
                scope_key="qq:group:1",
                conversation_id="group:1:user:2",
                requester_user_id=2,
                trigger_message_id=3,
                objective="核实产品参数",
                context="必要上下文",
                selected_profile=profile,
                tools=[],
                execute_tool=AsyncMock(return_value='{"ok":true}'),
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mode"], "delegate")
        self.assertEqual(result["role"], "researcher")
        self.assertEqual(worker.await_count, 1)
        planner.assert_not_awaited()
        synthesizer.assert_not_awaited()
        task = store.recent(limit=1)[0]
        self.assertEqual(task.plan["mode"], "delegate")
        self.assertEqual(store.runs(task.task_id)[0].role, "researcher")
        store.close()

    async def test_plans_runs_and_synthesizes_fixed_agents(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        catalog = ModelCatalog({"test": profile}, default_profile="test")
        coordinator = SubAgentCoordinator(store, catalog, logger=AsyncMock())
        plan = {
            "goal": "完成报告",
            "steps": [
                {
                    "id": "research",
                    "agent": "researcher",
                    "depends_on": [],
                    "objective": "查资料",
                    "deliverable": "事实",
                },
                {
                    "id": "write",
                    "agent": "document",
                    "depends_on": ["research"],
                    "objective": "写报告",
                    "deliverable": "报告",
                },
            ],
        }
        worker_result = '{"status":"success","summary":"完成","artifacts":[]}'
        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_json",
                new=AsyncMock(return_value=plan),
            ),
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(return_value=worker_result),
            ) as worker,
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek",
                new=AsyncMock(return_value="最终报告"),
            ),
        ):
            answer = await coordinator.run(
                scope_key="qq:group:1",
                conversation_id="group:1:user:2",
                requester_user_id=2,
                trigger_message_id=3,
                objective="完成报告",
                context="必要上下文",
                selected_profile=profile,
                tools=[],
                execute_tool=AsyncMock(return_value='{"ok":true}'),
            )
        self.assertIn("最终报告", answer)
        self.assertEqual(worker.await_count, 2)
        task = store.recent(limit=1)[0]
        self.assertEqual(task.status, "completed")
        self.assertEqual([run.role for run in store.runs(task.task_id)], ["researcher", "document"])
        contexts = store.run_contexts(task.task_id)
        self.assertEqual([item["role"] for item in contexts], ["researcher", "document"])
        self.assertEqual(len({item["context_hash"] for item in contexts}), 2)
        self.assertNotIn("已核实", contexts[0]["context"]["rendered_context"])
        self.assertIn("完成", contexts[1]["context"]["rendered_context"])
        store.close()

    async def test_explicit_group_delivery_is_completed_after_worker_rounds(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        catalog = ModelCatalog({"test": profile}, default_profile="test")
        coordinator = SubAgentCoordinator(store, catalog, logger=AsyncMock())
        plan = {
            "goal": "生成并发送 PDF",
            "steps": [
                {
                    "id": "write",
                    "agent": "document",
                    "depends_on": [],
                    "objective": "生成 PDF",
                    "deliverable": "已发送的 PDF",
                }
            ],
        }
        worker_result = json.dumps(
            {
                "status": "partial",
                "summary": "PDF 已生成但工具轮次用完",
                "facts": [],
                "artifacts": [
                    {
                        "handle": "s123abc:/workspace/output.pdf",
                        "kind": "file",
                        "name": "中文报告.pdf",
                    }
                ],
                "warnings": [],
            },
            ensure_ascii=False,
        )
        execute_tool = AsyncMock(return_value='{"ok":true,"uploaded":true}')
        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_json",
                new=AsyncMock(return_value=plan),
            ),
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(return_value=worker_result),
            ),
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek",
                new=AsyncMock(return_value="PDF 已发到群里。"),
            ),
            patch.object(coordinator, "_validate_workflow", new=AsyncMock(return_value={
                "status": "failed", "artifacts": [{
                    "artifact_key": "s123abc:/workspace/output.pdf", "status": "passed",
                }],
            })),
        ):
            answer = await coordinator.run(
                scope_key="qq:group:1",
                conversation_id="group:1:user:2",
                requester_user_id=2,
                trigger_message_id=3,
                objective="整理成 PDF 发到群里",
                context="必要上下文",
                selected_profile=profile,
                tools=[],
                execute_tool=execute_tool,
            )

        self.assertIn("PDF 已发到群里", answer)
        execute_tool.assert_awaited_once_with(
            "send_file_from_sandbox",
            {
                "sandbox_id": "s123abc",
                "path": "/workspace/output.pdf",
                "filename": "中文报告.pdf",
            },
        )
        task = store.recent(limit=1)[0]
        self.assertEqual(task.status, "partial")
        self.assertEqual(store.runs(task.task_id)[0].status, "partial")
        self.assertTrue(task.result["deliveries"][0]["ok"])
        store.close()

    async def test_failed_worker_blocks_dependent_step_but_still_synthesizes(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        catalog = ModelCatalog({"test": profile}, default_profile="test")
        coordinator = SubAgentCoordinator(store, catalog, logger=AsyncMock())
        plan = {
            "steps": [
                {
                    "id": "research",
                    "agent": "researcher",
                    "depends_on": [],
                    "objective": "查资料",
                    "deliverable": "事实",
                },
                {
                    "id": "write",
                    "agent": "document",
                    "depends_on": ["research"],
                    "objective": "写报告",
                    "deliverable": "PDF",
                },
            ]
        }
        worker_result = json.dumps(
            {"status": "failed", "summary": "来源不可访问", "warnings": []},
            ensure_ascii=False,
        )
        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_json",
                new=AsyncMock(return_value=plan),
            ),
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(return_value=worker_result),
            ) as worker,
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek",
                new=AsyncMock(return_value="资料获取失败，未生成报告。"),
            ),
        ):
            await coordinator.run(
                scope_key="qq:group:1",
                conversation_id="group:1:user:2",
                requester_user_id=2,
                trigger_message_id=3,
                objective="查资料并写报告",
                context="",
                selected_profile=profile,
                tools=[],
                execute_tool=AsyncMock(return_value='{"ok":true}'),
            )

        task = store.recent(limit=1)[0]
        self.assertEqual(task.status, "partial")
        self.assertEqual([run.status for run in store.runs(task.task_id)], ["failed", "skipped"])
        self.assertEqual(worker.await_count, 1)
        store.close()

    async def test_progress_failure_does_not_fail_the_task(self) -> None:
        store = SubAgentStore(":memory:")
        profile = ModelProfile(
            name="test",
            provider="test",
            protocol="openai-chat",
            model="test-model",
            base_url="http://127.0.0.1",
            api_key_required=False,
        )
        catalog = ModelCatalog({"test": profile}, default_profile="test")
        logger = Mock()
        coordinator = SubAgentCoordinator(store, catalog, logger=logger)
        with (
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_json",
                new=AsyncMock(return_value={"steps": [{"id": "research", "agent": "researcher", "objective": "查资料", "deliverable": "资料", "depends_on": []}]}),
            ),
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                new=AsyncMock(return_value='{"status":"success","summary":"完成"}'),
            ),
            patch(
                "src.plugins.ai_chat.subagents.ask_deepseek",
                new=AsyncMock(return_value="完成"),
            ),
        ):
            await coordinator.run(
                scope_key="qq:group:1",
                conversation_id="group:1:user:2",
                requester_user_id=2,
                trigger_message_id=3,
                objective="查资料",
                context="",
                selected_profile=profile,
                tools=[],
                execute_tool=AsyncMock(return_value='{"ok":true}'),
                progress=AsyncMock(side_effect=RuntimeError("QQ offline")),
            )

        self.assertEqual(store.recent(limit=1)[0].status, "completed")
        self.assertTrue(logger.warning.called)
        store.close()


if __name__ == "__main__":
    unittest.main()
