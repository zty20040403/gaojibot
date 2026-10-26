from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat import agent_loop, intent_routing, llm_gateway, tool_policy
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.intent_routing import INTENT_TOOLS, classify_tool_intent
from src.plugins.ai_chat.model_catalog import ModelCatalog


class IntentRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.catalog = ModelCatalog.from_json(json.dumps({"profiles": {
            "cheap": {"model": "local-model", "base_url": "http://model.invalid/v1",
                      "api_key_required": False, "thinking": "enabled",
                      "reasoning_effort": "high", "max_output_tokens": 8192,
                      "fallback_profiles": ["large"]},
            "large": {"model": "large-model", "api_key_required": False},
        }}), default_profile="large", environ={})
        self.options = dict(catalog=self.catalog, profile_name="cheap", allowed_tools=INTENT_TOOLS,
                            media={"image_available": True, "image_attached_now": False})
        self.trace = AgentTrace(model="answer-model", profile="large", messages=[
            {"role": "user", "content": "original"}], input_tokens=7, total_tokens=7)
        overrides = patch.dict(tool_policy._TOOL_ENABLED_OVERRIDES, {}, clear=True)
        overrides.start()
        self.addCleanup(overrides.stop)

    async def test_semantic_choices_are_bounded_and_cannot_use_fallback_profiles(self) -> None:
        async def classify(system, user, **kwargs):
            profile = kwargs["profile"]
            self.assertEqual(profile.name, "cheap")
            self.assertEqual((profile.thinking, profile.reasoning_effort), ("disabled", ""))
            self.assertEqual(profile.fallback_profiles, ())
            self.assertEqual(profile.max_output_tokens, 128)
            self.assertLessEqual(profile.timeout_seconds, 3)
            self.assertFalse(kwargs["recover_empty_response"])
            self.assertEqual(llm_gateway._profile_scope.get(), frozenset({"cheap"}))
            self.assertIn("<untrusted", user)
            self.assertIn("same-conversation", user)
            self.assertIn("current request", user)
            self.assertIn("allowed_tools", user)
            self.assertNotIn("http://model.invalid", user)
            return {"tool": selected}

        loader = AsyncMock(return_value="same-conversation history")
        with patch.object(intent_routing, "ask_deepseek_json", side_effect=classify) as model:
            for selected in (*sorted(INTENT_TOOLS), "auto"):
                with self.subTest(tool=selected):
                    result = await classify_tool_intent("current request", **self.options, load_context=loader)
                    self.assertEqual(result.tool, selected)
                    self.assertEqual(result.reason, "automatic" if selected == "auto" else "semantic")
        self.assertEqual(model.await_count, len(INTENT_TOOLS) + 1)
        self.assertIsNone(llm_gateway._profile_scope.get())
        self.assertEqual(self.catalog.resolve("cheap").thinking, "enabled")

    async def test_unavailable_classification_skips_both_model_and_history(self) -> None:
        unhealthy = Mock()
        unhealthy.unavailable_reason.return_value = ("not_ready", "Not running")
        cases = [({"enabled": False}, "disabled"), ({"profile_name": ""}, "profile_unconfigured"),
                 ({"profile_name": "absent"}, "profile_unconfigured"),
                 ({"allowed_tools": frozenset({"host_reboot"})}, "no_applicable_tools"),
                 ({"local_model": unhealthy}, "profile_unavailable")]
        loader = AsyncMock(return_value="history")
        with patch.object(intent_routing, "ask_deepseek_json", new_callable=AsyncMock) as model:
            for changes, reason in cases:
                with self.subTest(reason=reason):
                    result = await classify_tool_intent("看看", **(self.options | changes), load_context=loader)
                    self.assertEqual((result.tool, result.reason), ("auto", reason))
            result = await classify_tool_intent("a" * 3001, **self.options, load_context=loader)
            self.assertEqual(result.reason, "request_too_large")
        model.assert_not_awaited()
        loader.assert_not_awaited()

    async def test_invalid_schema_unavailable_tools_and_writes_are_not_selected(self) -> None:
        for response in (None, [], {}, {"tool": True}, {"tool": ["view_image"]},
                         {"tool": "host_reboot"}, {"tool": "view_video"},
                         {"tool": "view_image", "approval": "granted"}):
            with self.subTest(response=response), patch.object(
                intent_routing, "ask_deepseek_json", AsyncMock(return_value=response)
            ):
                result = await classify_tool_intent("current", **(self.options | {
                    "allowed_tools": frozenset({"view_image"})}))
                self.assertEqual((result.tool, result.reason), ("auto", "invalid_response"))

    async def test_tool_disable_is_checked_before_and_after_classification(self) -> None:
        tool_policy.set_tool_enabled("view_image", False)
        options = self.options | {"allowed_tools": frozenset({"view_image"})}
        with patch.object(intent_routing, "ask_deepseek_json", new_callable=AsyncMock) as model:
            result = await classify_tool_intent("current", **options)
        model.assert_not_awaited()
        self.assertEqual(result.reason, "no_applicable_tools")

        async def disable(*args, **kwargs):
            tool_policy.set_tool_enabled("view_image", False)
            return {"tool": "view_image"}

        tool_policy.set_tool_enabled("view_image", True)
        with patch.object(intent_routing, "ask_deepseek_json", side_effect=disable):
            result = await classify_tool_intent("current", **options)
        self.assertEqual((result.tool, result.reason), ("auto", "tool_disabled"))

    async def test_health_or_context_errors_leave_main_conversation_usable(self) -> None:
        broken_health = Mock()
        broken_health.unavailable_reason.side_effect = RuntimeError("private-provider-credential")
        for extras in ({"local_model": broken_health},
                       {"load_context": AsyncMock(side_effect=RuntimeError("private-context"))}):
            with self.subTest(extras=tuple(extras)), patch.object(
                intent_routing, "ask_deepseek_json", new_callable=AsyncMock
            ) as model:
                result = await classify_tool_intent("current", **self.options, **extras, trace=self.trace)
                self.assertEqual((result.tool, result.reason), ("auto", "classifier_error"))
                model.assert_not_awaited()
        self.assertNotIn("private-", json.dumps(self.trace.execution_decisions))

    async def test_timeout_cancels_classification_and_user_cancellation_propagates(self) -> None:
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(intent_routing, "ask_deepseek_json", side_effect=slow):
            result = await classify_tool_intent("current", **self.options, timeout_seconds=0.1)
            self.assertEqual((result.tool, result.reason), ("auto", "timeout"))
            self.assertTrue(cancelled.is_set())
            entered.clear()
            cancelled.clear()
            task = asyncio.create_task(classify_tool_intent("current", **self.options, trace=self.trace))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled.is_set())
            self.assertEqual(self.trace.execution_decisions[-1]["reason"], "cancelled")

    async def test_usage_survives_parse_failure_without_polluting_answer_history(self) -> None:
        async def invalid(*args, **kwargs):
            child = kwargs["trace"]
            child.profile, child.model = "cheap", "local-model"
            child.input_tokens, child.output_tokens, child.cached_tokens, child.total_tokens = 30, 5, 10, 35
            child.messages.append({"role": "system", "content": "classifier instruction"})
            raise RuntimeError("private-key-in-provider-error")

        with patch.object(intent_routing, "ask_deepseek_json", side_effect=invalid):
            result = await classify_tool_intent("current", **self.options, trace=self.trace)
        self.assertEqual(result.reason, "classifier_error")
        self.assertEqual((self.trace.input_tokens, self.trace.output_tokens, self.trace.cached_tokens,
                          self.trace.total_tokens), (37, 5, 10, 42))
        self.assertEqual((self.trace.profile, self.trace.model), ("large", "answer-model"))
        self.assertEqual(self.trace.messages, [{"role": "user", "content": "original"}])
        self.assertEqual(self.trace.execution_decisions[-1]["actual_profile"], "cheap")
        self.assertNotIn("private-key", json.dumps(self.trace.execution_decisions))

    async def test_empty_json_completion_has_no_extra_recovery_call(self) -> None:
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
        with (
            patch.object(agent_loop, "_resolve_profile", return_value=self.catalog.resolve("cheap")),
            patch.object(agent_loop, "_completion_once", AsyncMock(return_value=(response, ""))) as once,
            patch.object(agent_loop, "_completion_with_optional_stream", new_callable=AsyncMock) as recovery,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                await agent_loop.ask_deepseek_json("classify", "request", recover_empty_response=False)
        once.assert_awaited_once()
        recovery.assert_not_awaited()
