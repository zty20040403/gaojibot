from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.plugins.ai_chat.agent import AgentResult, ContextPacket, DEFAULT_AGENT_REGISTRY
from src.plugins.ai_chat.agent.execution import (
    EntryDecision,
    normalize_direct_entry_payload,
)
from src.plugins.ai_chat.agent.model_routing import agent_profile_names, choose_agent_profile
from src.plugins.ai_chat.agent.sessions import read_upstream_result
from src.plugins.ai_chat.agent_loop import AgentTrace, ask_deepseek_with_tools
from src.plugins.ai_chat.llm_gateway import LLMGateway, completion_profile_scope
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.subagents import (
    SubAgentCoordinator,
    SubAgentStore,
    TaskStep,
    _normalize_worker_scope_result,
)


def profile(name="test"):
    return ModelProfile(name=name, model=name, provider="test", protocol="openai-chat",
                        base_url="http://127.0.0.1", api_key_required=False)


def decision(mode="direct", answer="你好"):
    return {"mode": mode, "reason": "根据交付内容分工", "answer": answer if mode == "direct" else "",
            "task_type": {"direct": "conversation", "delegate": "execution", "workflow": "project"}[mode],
            "delivery_required": False,
            "objective": "精心写个谷粒商城", "deliverables": [] if mode == "direct" else ["可运行的商城"],
            "constraints": [], "acceptance": [] if mode == "direct" else ["完成集成测试"],
            "outcome_checks": [] if mode == "direct" else [{"criterion_index": 0, "kind": "evidence"}],
            "steps": [] if mode == "direct" else [
                {"id": "frontend", "agent": "coder", "objective": "实现前端", "deliverable": "前端源码", "depends_on": []},
                *([] if mode == "delegate" else [
                    {"id": "backend", "agent": "coder", "objective": "实现后端", "deliverable": "后端源码", "depends_on": []},
                    {"id": "integration", "agent": "coder", "objective": "集成和测试", "deliverable": "集成测试记录", "depends_on": ["frontend", "backend"]},
                ]),
            ]}


def completion(payload=None, content=""):
    calls = [] if payload is None else [SimpleNamespace(
        id="entry-1", type="function", function=SimpleNamespace(name="decide_execution", arguments=json.dumps(payload))) ]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))], usage=None), ""


TOOL = {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object", "properties": {}}}}


class EntryContractTests(unittest.TestCase):
    def test_three_modes_and_same_role_steps(self):
        for mode in ("direct", "delegate", "workflow"):
            parsed = EntryDecision.parse(decision(mode))
            self.assertEqual(parsed.mode, mode)
        self.assertEqual([s["agent"] for s in EntryDecision.parse(decision("workflow")).steps], ["coder"] * 3)

    def test_invalid_plans_never_downgrade_to_direct(self):
        invalid = []
        raw = decision("workflow"); raw["steps"][0]["depends_on"] = ["integration"]; invalid.append(raw)
        raw = decision("workflow"); raw["steps"][0]["id"] = "backend"; invalid.append(raw)
        raw = decision("workflow"); raw["steps"][2]["depends_on"] = ["missing"]; invalid.append(raw)
        raw = decision("direct"); raw["deliverables"] = ["源码"]; invalid.append(raw)
        raw = decision("direct"); raw["task_type"] = "project"; invalid.append(raw)
        raw = decision("delegate"); raw["acceptance"] = []; invalid.append(raw)
        invalid.append({})
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                EntryDecision.parse(raw)



    def test_progress_is_runtime_capability_not_planned_delivery(self):
        guide = DEFAULT_AGENT_REGISTRY.planning_tools()
        self.assertNotIn("say", guide["shared_tools"])
        for role in DEFAULT_AGENT_REGISTRY.worker_roles:
            self.assertNotIn("say", guide["role_tools"][role])
            self.assertIn("say", DEFAULT_AGENT_REGISTRY.worker(role).allowed_tools)
        raw = decision("delegate")
        raw["steps"][0]["required_tools"] = ["say"]
        with self.assertRaisesRegex(ValueError, "say is progress-only"):
            EntryDecision.parse(raw)
        persisted = {**raw, "contract": {"version": 3}}
        restored = EntryDecision.from_payload(persisted)
        self.assertEqual(restored.steps[0]["required_tools"], ["say"])
        raw["steps"][0]["required_tools"] = ["say", "not_a_tool"]
        with self.assertRaisesRegex(ValueError, "cannot use not_a_tool"):
            EntryDecision.from_payload({**raw, "contract": {"version": 3}})

    def test_new_entry_cannot_omit_checks_but_legacy_checkpoint_can_resume(self):
        raw = decision("workflow")
        del raw["outcome_checks"]
        with self.assertRaisesRegex(ValueError, "outcome_checks"):
            EntryDecision.parse(raw)
        legacy = dict(raw, contract={"version": 1})
        self.assertEqual(EntryDecision.from_payload(legacy).contract.outcome_checks[0]["kind"], "evidence")
        with self.assertRaisesRegex(ValueError, "outcome_checks"):
            EntryDecision.from_payload(dict(raw, contract={"version": 2}))

    def test_direct_branch_normalization_only_discards_inapplicable_contract(self):
        noisy = decision("direct")
        noisy.update({
            "objective": "旧任务残留",
            "deliverables": ["压缩包"],
            "constraints": ["发送到群里"],
            "acceptance": ["文件可下载"],
            "delivery_required": True,
        })
        normalized = normalize_direct_entry_payload(noisy)
        self.assertIsNotNone(normalized)
        parsed = EntryDecision.parse(normalized or {})
        self.assertEqual(parsed.mode, "direct")
        self.assertEqual(parsed.answer, "你好")
        self.assertEqual(parsed.contract.deliverables, ())

        project = dict(noisy, task_type="project")
        self.assertIsNone(normalize_direct_entry_payload(project))

    def test_missing_result_status_is_not_success(self):
        self.assertEqual(AgentResult.from_payload({}).status, "partial")
        self.assertEqual(AgentResult.from_payload({"summary": "done"}).status, "partial")
        self.assertEqual(AgentResult.from_payload({"status": "success", "summary": ""}).status, "partial")

    def test_downstream_handoff_does_not_make_completed_step_partial(self):
        result = AgentResult.from_payload({
            "status": "success",
            "summary": "分析方案已完成",
            "unresolved": [],
            "handoff": ["前端和后端按方案继续实现", "宿主验收后发送文件"],
        })
        self.assertEqual(result.status, "success")
        self.assertEqual(result.handoff, ("前端和后端按方案继续实现", "宿主验收后发送文件"))
        self.assertEqual(result.as_payload()["handoff"], list(result.handoff))

    def test_host_moves_obvious_downstream_work_out_of_unresolved(self):
        result = _normalize_worker_scope_result({
            "status": "partial",
            "summary": "商城分析和接口方案已经完成",
            "unresolved": ["尚需后续前后端步骤按方案实现，并由宿主发送压缩包"],
            "handoff": [],
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(len(result["handoff"]), 1)

    def test_host_keeps_real_step_gap_as_partial(self):
        result = _normalize_worker_scope_result({
            "status": "partial",
            "summary": "只写了一半",
            "unresolved": ["仍缺少接口字段定义"],
        })
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unresolved"], ["仍缺少接口字段定义"])

    def test_handoff_is_valid_json_and_can_read_tail_without_cross_task_access(self):
        upstream = {"frontend": {"status": "success", "summary": "长结果" * 6000,
                                 "facts": [f"fact-{i}" for i in range(100)], "handoff": ["接入后端"]}}
        packet = ContextPacket("group:1", "group:1:user:2", 2, 3, "集成", supporting_context="正文" * 5000)
        rendered = packet.for_agent(DEFAULT_AGENT_REGISTRY.worker("coder"), upstream=upstream).rendered_context
        capsule = json.loads(rendered.split("[上游结构化结果索引]\n")[1])
        self.assertEqual(capsule["frontend"]["sections"]["facts"], 100)
        tail = json.loads(read_upstream_result(upstream, {"step_id": "frontend", "section": "facts", "offset": 99}))
        self.assertEqual(tail["data"], ["fact-99"])
        denied = json.loads(read_upstream_result(upstream, {"step_id": "other-task", "section": "facts"}))
        self.assertFalse(denied["ok"])
        handoff = json.loads(read_upstream_result(upstream, {"step_id": "frontend", "section": "handoff"}))
        self.assertEqual(handoff["data"], ["接入后端"])


class EntryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_uses_one_model_call_and_current_context(self):
        trace, handler = AgentTrace(), AsyncMock(return_value=None)
        with patch("src.plugins.ai_chat.agent_loop._completion_with_optional_stream", new=AsyncMock(return_value=completion(decision()))) as model:
            result = await ask_deepseek_with_tools("你好", [], [TOOL], AsyncMock(), profile=profile(),
                                                  group_context="本群当前话题", entry_handler=handler, trace=trace)
        self.assertEqual(result, "你好")
        self.assertEqual(model.await_count, 1)
        self.assertIn("本群当前话题", str(model.call_args.kwargs["messages"]))
        self.assertEqual(trace.execution_decisions[0]["mode"], "direct")

    async def test_noisy_direct_contract_does_not_crash_the_turn(self):
        noisy = decision("direct", answer="活着呢")
        noisy.update({
            "objective": "旧任务残留",
            "deliverables": ["压缩包"],
            "acceptance": ["文件可下载"],
            "delivery_required": True,
        })
        handler = AsyncMock(return_value=None)
        with patch(
            "src.plugins.ai_chat.agent_loop._completion_with_optional_stream",
            new=AsyncMock(return_value=completion(noisy)),
        ) as model:
            result = await ask_deepseek_with_tools(
                "活着吗",
                [],
                [TOOL],
                AsyncMock(),
                profile=profile(),
                entry_handler=handler,
            )
        self.assertEqual(result, "活着呢")
        self.assertEqual(model.await_count, 1)
        self.assertEqual(handler.call_args.args[0].contract.deliverables, ())

    async def test_workflow_uses_host_result_without_redundant_main_call(self):
        handler = AsyncMock(return_value="task#1 成品")
        execute = AsyncMock()
        with patch("src.plugins.ai_chat.agent_loop._completion_with_optional_stream", new=AsyncMock(return_value=completion(decision("workflow")))) as model:
            result = await ask_deepseek_with_tools("精心写个谷粒商城", [], [TOOL], execute,
                                                  profile=profile(), entry_handler=handler)
        self.assertEqual(result, "task#1 成品")
        self.assertEqual(model.await_count, 1)
        self.assertEqual(handler.call_args.args[0].mode, "workflow")
        execute.assert_not_awaited()

    async def test_invalid_entry_falls_back_to_normal_loop_without_side_effects(self):
        handler, execute = AsyncMock(), AsyncMock()
        model = AsyncMock(
            side_effect=[completion({}), completion({}), completion(content="入口失败，未执行项目")]
        )
        with patch(
            "src.plugins.ai_chat.agent_loop._completion_with_optional_stream",
            new=model,
        ):
            result = await ask_deepseek_with_tools(
                "做商城",
                [],
                [TOOL],
                execute,
                profile=profile(),
                entry_handler=handler,
            )
        self.assertEqual(result, "入口失败，未执行项目")
        self.assertEqual(model.await_count, 3)
        handler.assert_not_awaited(); execute.assert_not_awaited()

    async def test_entry_rejects_worker_tool_disabled_in_current_turn(self):
        payload = decision("workflow")
        payload["steps"][0].update(agent="operator", required_tools=["ops_call"])
        handler, execute = AsyncMock(), AsyncMock()
        model = AsyncMock(side_effect=[completion(payload), completion(payload), completion(content="工具未开启，未执行")])
        with patch("src.plugins.ai_chat.agent_loop._completion_with_optional_stream", new=model):
            result = await ask_deepseek_with_tools("检查服务器", [], [TOOL], execute,
                profile=profile(), entry_handler=handler)
        self.assertEqual(result, "工具未开启，未执行")
        self.assertEqual(model.await_count, 3)
        self.assertIn("operator cannot use ops_call", model.call_args_list[1].kwargs["messages"][-1]["content"])
        handler.assert_not_awaited()
        execute.assert_not_awaited()

    async def test_delegate_returns_control_to_parent(self):
        with patch("src.plugins.ai_chat.agent_loop._completion_with_optional_stream", new=AsyncMock(side_effect=[
            completion(decision("delegate")), completion(content="脚本运行通过"),
        ])) as model:
            result = await ask_deepseek_with_tools("运行脚本", [], [TOOL], AsyncMock(), profile=profile(),
                                                  entry_handler=AsyncMock(return_value='{"status":"success"}'))
        self.assertEqual(result, "脚本运行通过")
        self.assertEqual(model.await_count, 2)


class ModelPolicyTests(unittest.TestCase):
    def test_roles_select_different_models_and_sol_is_not_a_hidden_fallback(self):
        profiles = {name: profile(name) for name in ("qwen-local", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "codex-auto-review")}
        catalog = ModelCatalog(profiles, default_profile="qwen-local")
        self.assertEqual(choose_agent_profile("coder", catalog.default, catalog, {}).name, "gpt-5.6-terra")
        self.assertEqual(choose_agent_profile("researcher", catalog.default, catalog, {}).name, "gpt-5.6-luna")
        names = agent_profile_names(catalog, {})
        self.assertNotIn("gpt-5.6-sol", names); self.assertNotIn("codex-auto-review", names)
        gateway = LLMGateway(catalog=catalog)
        with completion_profile_scope(names):
            self.assertNotIn("gpt-5.6-sol", {p.name for p in gateway._completion_candidates(catalog.default, {})})
        self.assertIn("gpt-5.6-sol", {p.name for p in gateway._completion_candidates(catalog.default, {})})
        override = {"coder": "gpt-5.6-sol"}
        self.assertEqual(choose_agent_profile("coder", catalog.default, catalog, override).name, "gpt-5.6-sol")
        self.assertFalse(profiles["qwen-local"].thinking == "disabled")


class SessionAndSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SubAgentStore(postgres_for_test(self))
        self.profile = profile()
        self.coordinator = SubAgentCoordinator(self.store, ModelCatalog({"test": self.profile}, default_profile="test"), logger=Mock())

    def tearDown(self):
        self.store.close()

    def task(self):
        return self.store.create_task(scope_key="group:1", conversation_id="group:1:user:2", requester_user_id=2,
                                      trigger_message_id=3, objective="demo", max_parallelism=3, max_steps=8)

    async def test_reuses_entry_plan_and_same_role_workers_really_overlap(self):
        started, barrier = set(), asyncio.Event()
        async def worker(text, history, tools, execute_tool, **kwargs):
            current = "acceptance" if "你是独立验收人" in text else "frontend" if "实现前端" in text else "backend" if "实现后端" in text else "integration"
            self.assertEqual(history, [])
            if current in {"frontend", "backend"}:
                started.add(current)
                if len(started) == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), timeout=1)
            else:
                self.assertTrue(barrier.is_set())
            kwargs["transcript_sink"]([{"role": "user", "content": current}])
            tool = json.loads(await execute_tool("web_search", {"query": current}))
            ref = tool["_task_evidence"]["ref"]
            return json.dumps({"status": "success", "summary": current,
                "findings": [], "completed": [{"description": current, "evidence_refs": [ref]}],
                "authorization": [], "next_verification": [],
                "metadata": {"criterion_reviews": [{"criterion_index": 0, "status": "passed",
                    "reason": "测试替身提供独立验收回执", "evidence_refs": [ref]}]} if current == "acceptance" else {}})
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_json", new=AsyncMock()) as planner, \
             patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=worker), \
             patch("src.plugins.ai_chat.subagents.ask_deepseek", new=AsyncMock(return_value="已集成")):
            result = await self.coordinator.run(scope_key="group:1", conversation_id="group:1:user:2", requester_user_id=2,
                trigger_message_id=3, objective="商城", context="", selected_profile=self.profile,
                tools=[TOOL], execute_tool=AsyncMock(return_value='{"ok":true,"content":"test evidence"}'), entry_decision=EntryDecision.parse(decision("workflow")))
        planner.assert_not_awaited()
        self.assertIn("已集成", result)
        task = self.store.recent(limit=1)[0]
        histories = [self.store.agent_session(task.task_id, run.run_id, scope_key="group:1", requester_user_id=2)["messages"] for run in self.store.runs(task.task_id)]
        self.assertEqual([items[0]["content"] for items in histories], ["frontend", "backend", "integration", "acceptance"])
        self.assertEqual(task.result["validation"]["acceptance"]["task_outcome"]["status"], "passed")

    async def test_sessions_are_versioned_and_scoped(self):
        task = self.task()
        run = self.store.create_run(task.task_id, TaskStep("code", "coder", "code", "files"), allowed_tools=[], model_profile="test")
        kwargs = dict(scope_key="group:1", requester_user_id=2)
        self.store.save_agent_session(task.task_id, run.run_id, [{"role": "user", "content": "first"}],
                                     model_profile="test", expected_version=0, **kwargs)
        with self.assertRaises(RuntimeError):
            self.store.save_agent_session(task.task_id, run.run_id, [], model_profile="test", expected_version=0, **kwargs)
        with self.assertRaises(PermissionError):
            self.store.agent_session(task.task_id, run.run_id, scope_key="group:2", requester_user_id=2)
        with self.assertRaises(PermissionError):
            self.store.agent_session(task.task_id, run.run_id, scope_key="group:1", requester_user_id=9)
        self.assertEqual(self.store.agent_session(task.task_id, run.run_id, **kwargs)["version"], 1)

    async def test_retry_keeps_its_own_transcript(self):
        attempts = 0
        async def worker(text, history, tools, execute_tool, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                kwargs["transcript_sink"]([{"role": "user", "content": "own saved work"}])
                raise ConnectionError("temporary model outage")
            self.assertEqual(history, [{"role": "user", "content": "own saved work"}])
            return '{"status":"success","summary":"continued"}'
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=worker):
            result = await self.coordinator.delegate(role="coder", scope_key="group:1", conversation_id="group:1:user:2",
                requester_user_id=2, trigger_message_id=3, objective="code", context="", selected_profile=self.profile,
                tools=[], execute_tool=AsyncMock())
        self.assertEqual(attempts, 2)
        self.assertEqual(result["result"]["summary"], "continued")

    async def test_foreign_context_is_rejected_before_creating_task(self):
        with self.assertRaises(PermissionError):
            await self.coordinator.delegate(role="coder", scope_key="group:1", conversation_id="group:1:user:2",
                requester_user_id=2, trigger_message_id=3, objective="code", context="", selected_profile=self.profile,
                tools=[], execute_tool=AsyncMock(), context_packet=ContextPacket("group:9", "group:9:user:2", 2, 3, "foreign"))
        self.assertEqual(self.store.recent(), [])

    async def test_required_delivery_without_artifact_cannot_complete(self):
        payload = decision("delegate")
        payload["delivery_required"] = True
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=AsyncMock(return_value='{"status":"success","summary":"done"}')):
            result = await self.coordinator.delegate(role="coder", scope_key="group:1", conversation_id="group:1:user:2",
                requester_user_id=2, trigger_message_id=3, objective="make it", context="", selected_profile=self.profile,
                tools=[], execute_tool=AsyncMock(), entry_decision=EntryDecision.parse(payload))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["delivery_state"], "failed_or_unknown")
        self.assertEqual(result["deliveries"][0]["state"], "missing_artifact")

    async def test_transcript_survives_store_reopen(self):
        store = SubAgentStore(postgres_for_test(self))
        task = store.create_task(
            scope_key="group:1",
            conversation_id="group:1:user:2",
            requester_user_id=2,
            trigger_message_id=3,
            objective="test",
            max_parallelism=1,
            max_steps=1,
        )
        run = store.create_run(
            task.task_id,
            TaskStep("code", "coder", "code", "files"),
            allowed_tools=[],
            model_profile="test",
        )
        store.save_agent_session(
            task.task_id,
            run.run_id,
            [{"role": "assistant", "content": "durable"}],
            scope_key="group:1",
            requester_user_id=2,
            model_profile="test",
            expected_version=0,
        )
        store.close()
        reopened = SubAgentStore(postgres_for_test(self))
        try:
            session = reopened.agent_session(
                task.task_id, run.run_id, scope_key="group:1", requester_user_id=2
            )
            self.assertEqual(session["messages"][0]["content"], "durable")
        finally:
            reopened.close()
