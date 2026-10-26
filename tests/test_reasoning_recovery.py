from __future__ import annotations

import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.llm_gateway import LLMEmptyResponseError, LLMGateway
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile

agent = importlib.import_module("src.plugins.ai_chat.agent_loop")


def completion(content="", *, reasoning="thinking", finish="length", calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=content, reasoning_content=reasoning, tool_calls=calls,
            ),
            finish_reason=finish,
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=8, total_tokens=18),
    )


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def create_completion(self, profile, **kwargs):
        self.calls.append((profile.name, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        pass


class ReasoningRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.qwen = ModelProfile(
            name="qwen-local", provider="qwen", protocol="openai-chat",
            model="qwen-test", base_url="http://model.test/v1", api_key="test",
            thinking="enabled", fallback_profiles=("luna",),
        )
        self.luna = ModelProfile(
            name="luna", provider="cliproxy", protocol="openai-chat",
            model="luna-test", base_url="http://model.test/v1", api_key="test",
        )
        self.catalog = ModelCatalog(
            {"qwen-local": self.qwen, "luna": self.luna},
            default_profile="qwen-local",
        )

    def runtime(self, responses):
        provider = ScriptedProvider(responses)
        gateway = LLMGateway({"openai-chat": provider}, catalog=self.catalog)
        self.addAsyncCleanup(gateway.close)
        return provider, patch.object(agent, "_runtime", return_value=(self.catalog, gateway))

    async def test_reasoning_only_repairs_once_and_accounts_both_requests(self):
        provider, runtime = self.runtime([completion(), completion("323", reasoning="", finish="stop")])
        trace = agent.AgentTrace()
        with runtime:
            answer = await agent.ask_deepseek("17 * 19", [], profile=self.qwen, trace=trace)
        self.assertEqual(answer, "323")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0][1]["extra_body"], {"enable_thinking": True})
        self.assertEqual(provider.calls[1][1]["extra_body"], {"enable_thinking": False})
        self.assertEqual(provider.calls[1][1]["max_tokens"], 4096)
        self.assertEqual(trace.output_tokens, 16)
        self.assertEqual(trace.model_routing[0]["reason_code"], "reasoning_budget_exhausted")
        self.assertEqual(trace.model_routing[0]["actual_profile"], "")
        self.assertEqual(trace.model_routing[-1]["actual_profile"], "qwen-local")

    async def test_second_empty_excludes_qwen_and_uses_fallback(self):
        provider, runtime = self.runtime([completion(), completion(), completion("answer", reasoning="", finish="stop")])
        trace = agent.AgentTrace()
        with runtime:
            answer = await agent.ask_deepseek("question", [], profile=self.qwen, trace=trace)
        self.assertEqual(answer, "answer")
        self.assertEqual([name for name, _ in provider.calls], ["qwen-local", "qwen-local", "luna"])
        self.assertNotIn("excluded_profiles", provider.calls[-1][1])
        self.assertEqual(trace.output_tokens, 24)
        self.assertEqual(trace.model_routing[-1]["actual_profile"], "luna")

    async def test_all_empty_stops_after_two_recovery_requests(self):
        provider, runtime = self.runtime([completion(), completion(), completion()])
        trace = agent.AgentTrace()
        with runtime, self.assertRaises(LLMEmptyResponseError):
            await agent.ask_deepseek("question", [], profile=self.qwen, trace=trace)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(trace.output_tokens, 24)
        self.assertEqual(trace.model_routing[-1]["actual_profile"], "")

    async def test_completed_tools_are_not_executed_again(self):
        call = SimpleNamespace(id="read-1", type="function", function=SimpleNamespace(name="get_current_time", arguments="{}"))
        provider, runtime = self.runtime([
            completion(reasoning="", finish="tool_calls", calls=[call]),
            completion(), completion("done", reasoning="", finish="stop"),
        ])
        execute = AsyncMock(return_value="2026-09-03")
        tools = [{"type":"function", "function":{"name":"get_current_time", "description":"Read time", "parameters":{"type":"object", "properties":{}}}}]
        with runtime:
            answer = await agent.ask_deepseek_with_tools("time", [], tools, execute, profile=self.qwen)
        self.assertEqual(answer, "done")
        execute.assert_awaited_once()
        self.assertEqual(provider.calls[-1][1]["tool_choice"], "none")
        self.assertTrue(any(m["role"] == "tool" for m in provider.calls[-1][1]["messages"]))

    async def test_partial_answer_and_content_filter_do_not_retry(self):
        for initial in (completion("partial"), completion(finish="content_filter")):
            with self.subTest(finish=initial.choices[0].finish_reason):
                provider, runtime = self.runtime([initial])
                with runtime:
                    await agent.ask_deepseek("question", [], profile=self.qwen)
                self.assertEqual(len(provider.calls), 1)

    async def test_cancellation_does_not_start_fallback(self):
        provider, runtime = self.runtime([completion(), asyncio.CancelledError()])
        with runtime, self.assertRaises(asyncio.CancelledError):
            await agent.ask_deepseek("question", [], profile=self.qwen)
        self.assertEqual(len(provider.calls), 2)

    async def test_streamed_reasoning_is_not_sent_and_stream_is_closed(self):
        class ReasoningStream:
            closed = False

            def __aiter__(self):
                async def chunks():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content="private thinking"), finish_reason=None)], usage=None)
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=None, finish_reason="length")], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=8, total_tokens=18))
                return chunks()

            async def close(self):
                self.closed = True

        stream = ReasoningStream()
        provider, runtime = self.runtime([stream, completion("323", reasoning="", finish="stop")])
        sink = AsyncMock()
        trace = agent.AgentTrace()
        with runtime, patch.object(agent, "settings", SimpleNamespace(stream_enabled=True)):
            response, emitted = await agent._completion_with_optional_stream(sink, self.qwen, trace=trace, messages=[{"role":"user", "content":"17 * 19"}])
        self.assertEqual(response.choices[0].message.content, "323")
        self.assertEqual(emitted, "")
        sink.assert_not_awaited()
        self.assertTrue(stream.closed)
        self.assertTrue(provider.calls[0][1]["stream"])
        self.assertNotIn("stream", provider.calls[1][1])


if __name__ == "__main__":
    unittest.main()
