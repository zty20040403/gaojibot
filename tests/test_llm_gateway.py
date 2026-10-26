from __future__ import annotations

import asyncio
import importlib
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot

nonebot.init()

from src.plugins.ai_chat.llm_gateway import (
    AnthropicMessagesProvider,
    LLMConfigError,
    LLMConnectionError,
    LLMGateway,
    LLMProviderError,
    LLMRateLimitError,
    ModelCircuit,
    OpenAIChatProvider,
    _anthropic_request,
    _anthropic_response,
    _request_for_profile,
)
from src.plugins.ai_chat.model_catalog import (
    ModelCatalog,
    ModelCapabilities,
    ModelProfile,
)


def profile(
    name: str,
    *,
    protocol: str = "openai-chat",
    provider: str = "test",
    api_key: str = "key",
    base_url: str = "https://example.test/v1",
    thinking: str = "auto",
    capabilities: ModelCapabilities | None = None,
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        protocol=protocol,
        model=f"model-{name}",
        base_url=base_url,
        api_key=api_key,
        thinking=thinking,
        max_output_tokens=1024 if protocol == "anthropic-messages" else 0,
        capabilities=capabilities or ModelCapabilities.defaults_for(protocol),
    )


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[ModelProfile, dict[str, object]]] = []
        self.closed = False

    async def create_completion(self, selected, **kwargs):
        self.calls.append((selected, kwargs))
        return "ok"

    async def list_models(self, selected):
        self.calls.append((selected, {"operation": "list"}))
        return [selected.model]

    async def close(self):
        self.closed = True


def stream_chunk(content: str = "", finish: str | None = None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=[]), finish_reason=finish,
        )],
        usage=None,
    )


class FakeStream:
    def __init__(self, *items):
        self.items = iter(items)
        self.close_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = next(self.items, None)
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.close_count += 1


class LLMGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_forced_tool_choice_rejection_retries_same_profile_with_auto(self):
        selected = profile("primary")
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(
            side_effect=[
                LLMProviderError(
                    "test",
                    400,
                    "tool_choice_not_supported: tool_choice for function 'reply' requires that exact function",
                ),
                "ok",
            ]
        )
        gateway = LLMGateway({"openai-chat": provider})
        forced = {"type": "function", "function": {"name": "reply"}}

        result = await gateway.create_completion_with_profile(
            selected,
            messages=[],
            tools=[{"type": "function"}],
            tool_choice=forced,
        )

        self.assertEqual(result.response, "ok")
        self.assertEqual(provider.create_completion.await_count, 2)
        calls = provider.create_completion.await_args_list
        self.assertEqual(calls[0].kwargs["tool_choice"], forced)
        self.assertEqual(calls[1].kwargs["tool_choice"], "auto")
        self.assertEqual(result.routing["reason_code"], "tool_choice_compatibility")
        self.assertEqual(gateway.health_snapshot()[selected.name]["total_failures"], 0)

    async def test_unrelated_bad_request_does_not_retry_as_auto(self):
        selected = profile("primary")
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(
            side_effect=LLMProviderError("test", 400, "invalid messages payload")
        )
        gateway = LLMGateway({"openai-chat": provider})

        with self.assertRaises(LLMProviderError):
            await gateway.create_completion(
                selected,
                messages=[],
                tools=[{"type": "function"}],
                tool_choice={"type": "function", "function": {"name": "reply"}},
            )

        provider.create_completion.assert_awaited_once()

    async def test_half_open_probe_is_released_on_cancel_or_unexpected_error(self):
        selected = profile("primary")
        for error in (asyncio.CancelledError(), ValueError("bad request")):
            with self.subTest(error=type(error).__name__):
                provider = RecordingProvider()
                provider.create_completion = AsyncMock(side_effect=error)
                gateway = LLMGateway({"openai-chat": provider})
                gateway.health._circuits[selected.name] = ModelCircuit(
                    opened_until=time.monotonic() - 1,
                )
                with self.assertRaises(type(error)):
                    await gateway.create_completion(selected, messages=[])
                circuit = gateway.health._circuits[selected.name]
                self.assertFalse(circuit.probe_in_flight)
                self.assertTrue(gateway.health.acquire(selected.name))
                self.assertEqual(circuit.total_successes, 0)
                self.assertEqual(circuit.request_count, 1)

    async def test_stream_success_is_recorded_after_consumption_once(self):
        selected = profile("primary")
        raw = FakeStream(stream_chunk("hello"), stream_chunk(finish="stop"))
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(return_value=raw)
        gateway = LLMGateway({"openai-chat": provider})
        gateway.health._circuits[selected.name] = ModelCircuit(opened_until=time.monotonic() - 1)
        routes = []
        with patch("src.plugins.ai_chat.llm_gateway.telemetry.observe_model") as observe:
            result = await gateway.create_completion_with_profile(
                selected, messages=[], stream=True, route_sink=routes.append,
            )
            circuit = gateway.health._circuits[selected.name]
            self.assertEqual(circuit.total_successes, 0)
            self.assertEqual(circuit.request_count, 0)
            self.assertTrue(circuit.probe_in_flight)
            self.assertFalse(routes)
            observe.assert_not_called()
            await asyncio.sleep(0.01)
            chunks = [chunk async for chunk in result.response]
            await result.response.close()
            self.assertEqual(len(chunks), 2)
            self.assertEqual(circuit.total_successes, 1)
            self.assertEqual(circuit.request_count, 1)
            self.assertGreaterEqual(circuit.request_seconds, 0.01)
            self.assertFalse(circuit.probe_in_flight)
            self.assertEqual(raw.close_count, 1)
            self.assertEqual(len(routes), 1)
            self.assertEqual(result.routing["outcomes"][-1]["status"], "succeeded")
            observe.assert_called_once()
            self.assertEqual(observe.call_args.kwargs["status"], "succeeded")

    async def test_stream_failure_before_first_chunk_uses_backup(self):
        primary, backup = profile("primary"), profile("backup")
        catalog = ModelCatalog({p.name: p for p in (primary, backup)}, default_profile=primary.name)
        broken = FakeStream(stream_chunk(), httpx.ReadError("disconnected"))
        good = FakeStream(stream_chunk("answer", "stop"))
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(side_effect=[broken, good])
        gateway = LLMGateway({"openai-chat": provider}, catalog=catalog, failure_threshold=1)
        result = await gateway.create_completion_with_profile(primary, messages=[], stream=True)
        self.assertEqual(result.profile.name, backup.name)
        self.assertEqual(broken.close_count, 1)
        self.assertEqual([c.choices[0].delta.content async for c in result.response], ["answer"])
        self.assertEqual(gateway.health_snapshot()[primary.name]["total_failures"], 1)
        self.assertEqual(gateway.health_snapshot()[backup.name]["fallback_uses"], 1)

    async def test_mid_stream_failure_is_not_retried_or_counted_successful(self):
        primary, backup = profile("primary"), profile("backup")
        catalog = ModelCatalog({p.name: p for p in (primary, backup)}, default_profile=primary.name)
        raw = FakeStream(stream_chunk("already emitted"), httpx.ReadError("disconnected"))
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(return_value=raw)
        gateway = LLMGateway({"openai-chat": provider}, catalog=catalog)
        routes = []
        result = await gateway.create_completion_with_profile(
            primary, messages=[], stream=True, route_sink=routes.append,
        )
        await anext(result.response)
        with self.assertRaises(LLMConnectionError):
            await anext(result.response)
        await result.response.close()
        circuit = gateway.health._circuits[primary.name]
        self.assertEqual((circuit.total_successes, circuit.total_failures, circuit.request_count), (0, 1, 1))
        provider.create_completion.assert_awaited_once()
        self.assertEqual(raw.close_count, 1)
        self.assertEqual(routes[0]["outcomes"][-1]["reason_code"], "network")

    async def test_stream_eof_without_finish_reason_is_a_failure(self):
        selected = profile("primary")
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(return_value=FakeStream(stream_chunk("incomplete")))
        gateway = LLMGateway({"openai-chat": provider})
        stream = await gateway.create_completion(selected, messages=[], stream=True)
        with self.assertRaises(LLMConnectionError):
            _ = [chunk async for chunk in stream]
        self.assertEqual(gateway.health._circuits[selected.name].total_successes, 0)
        self.assertEqual(gateway.health._circuits[selected.name].total_failures, 1)

    async def test_stream_cancellation_and_early_close_release_probe(self):
        selected = profile("primary")
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                raw = FakeStream(stream_chunk("partial"), asyncio.CancelledError())
                provider = RecordingProvider()
                provider.create_completion = AsyncMock(return_value=raw)
                gateway = LLMGateway({"openai-chat": provider})
                gateway.health._circuits[selected.name] = ModelCircuit(opened_until=time.monotonic() - 1)
                stream = await gateway.create_completion(selected, messages=[], stream=True)
                if cancel:
                    await anext(stream)
                    with self.assertRaises(asyncio.CancelledError):
                        await anext(stream)
                await stream.close()
                circuit = gateway.health._circuits[selected.name]
                self.assertFalse(circuit.probe_in_flight)
                self.assertEqual((circuit.total_successes, circuit.total_failures), (0, 0))
                self.assertEqual(circuit.request_count, 1)
                self.assertEqual(raw.close_count, 1)

    async def test_cancellation_while_waiting_for_first_content_closes_stream(self):
        selected = profile("primary")
        waiting = asyncio.Event()

        class BlockingStream(FakeStream):
            async def __anext__(self):
                waiting.set()
                await asyncio.Event().wait()

        raw = BlockingStream()
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(return_value=raw)
        gateway = LLMGateway({"openai-chat": provider})
        gateway.health._circuits[selected.name] = ModelCircuit(opened_until=time.monotonic() - 1)
        task = asyncio.create_task(gateway.create_completion(selected, messages=[], stream=True))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(gateway.health.acquire(selected.name))
        self.assertEqual(raw.close_count, 1)

    async def test_chat_stream_uses_backup_and_records_final_route(self):
        agent = importlib.import_module("src.plugins.ai_chat.agent_loop")
        primary, backup = profile("primary"), profile("backup")
        catalog = ModelCatalog({p.name: p for p in (primary, backup)}, default_profile=primary.name)
        provider = RecordingProvider()
        provider.create_completion = AsyncMock(side_effect=[
            FakeStream(stream_chunk(), httpx.ReadError("disconnected")),
            FakeStream(stream_chunk(), stream_chunk("answer\n\n", "stop")),
        ])
        gateway = LLMGateway({"openai-chat": provider}, catalog=catalog)
        sink, trace = AsyncMock(), agent.AgentTrace()
        with (
            patch.object(agent, "_model_catalog", catalog),
            patch.object(agent, "_llm_gateway", gateway),
        ):
            response, emitted = await agent._create_streaming_completion(
                sink, primary, trace=trace, messages=[],
            )
            self.assertEqual(agent._actual_profile(primary).name, backup.name)
        self.assertEqual(response.choices[0].message.content, "answer\n\n")
        self.assertEqual(emitted, "answer\n\n")
        sink.assert_awaited_once_with("answer\n\n")
        self.assertEqual(len(trace.model_routing), 1)
        self.assertEqual(trace.model_routing[0]["actual_profile"], backup.name)
        self.assertEqual(trace.model_routing[0]["outcomes"][-1]["status"], "succeeded")

    def test_qwen_thinking_uses_native_switch(self) -> None:
        agent = importlib.import_module("src.plugins.ai_chat.agent_loop")
        for mode, expected in (
            ("enabled", {"enable_thinking": True}),
            ("disabled", {"enable_thinking": False}),
            ("auto", None),
        ):
            with self.subTest(thinking=mode):
                selected = profile("qwen-local", provider="qwen", thinking=mode)
                kwargs = agent._completion_kwargs([], selected)
                self.assertEqual(kwargs["extra_body"], expected)
                request = _request_for_profile(
                    selected, {**kwargs, "reasoning_effort": "xhigh"}
                )
                self.assertEqual(request.get("extra_body"), expected)
                self.assertNotIn("reasoning_effort", request)

    def test_profile_without_forced_tool_choice_uses_auto(self) -> None:
        forced = {
            "type": "function",
            "function": {"name": "decide_execution"},
        }
        qwen = profile(
            "qwen-local",
            provider="qwen",
            capabilities=ModelCapabilities(forced_tool_choice=False),
        )
        qwen_request = _request_for_profile(
            qwen,
            {"messages": [], "tools": [{}], "tool_choice": forced},
        )
        self.assertEqual(qwen_request["tool_choice"], "auto")

        gpt = profile("gpt", provider="cliproxy")
        gpt_request = _request_for_profile(
            gpt,
            {"messages": [], "tools": [{}], "tool_choice": forced},
        )
        self.assertEqual(gpt_request["tool_choice"], forced)

    async def test_fallback_does_not_leak_qwen_thinking_switch(self) -> None:
        primary = profile("qwen-local", provider="qwen", thinking="enabled")
        backup = profile("deepseek", provider="deepseek", thinking="disabled")
        catalog = ModelCatalog(
            {primary.name: primary, backup.name: backup},
            default_profile=primary.name,
        )

        class FailingQwenProvider(RecordingProvider):
            async def create_completion(self, selected, **kwargs):
                self.calls.append((selected, kwargs))
                if selected.name == primary.name:
                    raise LLMConnectionError("unavailable")
                return "ok"

        provider = FailingQwenProvider()
        gateway = LLMGateway({"openai-chat": provider}, catalog=catalog)
        result = await gateway.create_completion_with_profile(
            primary, messages=[], extra_body={"enable_thinking": True}
        )
        self.assertEqual(result.profile.name, backup.name)
        self.assertEqual(provider.calls[0][1]["extra_body"], {"enable_thinking": True})
        self.assertEqual(
            provider.calls[1][1]["extra_body"], {"thinking": {"type": "disabled"}}
        )
        await gateway.close()

    async def test_openai_profile_forwards_reasoning_effort(self) -> None:
        selected = profile("reasoning", provider="cliproxy")
        selected = selected.with_reasoning_effort("high")

        request = _request_for_profile(
            selected,
            {"messages": [{"role": "user", "content": "hello"}]},
        )

        self.assertEqual(request["reasoning_effort"], "high")

    async def test_retryable_failure_uses_fallback_and_opens_circuit(self) -> None:
        primary = profile("primary", provider="same-provider")
        backup = profile("backup", provider="same-provider")
        primary = ModelProfile(
            **{
                **primary.__dict__,
                "fallback_profiles": ("backup",),
            }
        )
        catalog = ModelCatalog(
            {primary.name: primary, backup.name: backup},
            default_profile=primary.name,
        )

        class SelectiveProvider(RecordingProvider):
            async def create_completion(self, selected, **kwargs):
                self.calls.append((selected, kwargs))
                if selected.name == "primary":
                    raise LLMConnectionError("temporary network failure")
                return selected.name

        provider = SelectiveProvider()
        gateway = LLMGateway(
            {"openai-chat": provider},
            catalog=catalog,
            failure_threshold=1,
        )

        messages = [
            {"role": "tool", "tool_call_id": "call-1", "content": "done"}
        ]
        first = await gateway.create_completion_with_profile(
            primary,
            messages=messages,
        )
        second = await gateway.create_completion_with_profile(
            primary,
            messages=[],
        )

        self.assertEqual(first.response, "backup")
        self.assertEqual(first.profile.name, "backup")
        self.assertEqual(provider.calls[1][1]["messages"], messages)
        self.assertEqual(second.profile.name, "backup")
        self.assertEqual(
            [item[0].name for item in provider.calls],
            ["primary", "backup", "backup"],
        )
        health = gateway.health_snapshot()
        self.assertEqual(health["primary"]["status"], "open")
        self.assertEqual(health["backup"]["fallback_uses"], 2)

    async def test_content_policy_failure_does_not_try_another_model(self) -> None:
        primary = profile("primary")
        backup = profile("backup")
        catalog = ModelCatalog(
            {primary.name: primary, backup.name: backup},
            default_profile=primary.name,
        )

        class RefusingProvider(RecordingProvider):
            async def create_completion(self, selected, **kwargs):
                self.calls.append((selected, kwargs))
                raise LLMProviderError(
                    selected.provider,
                    400,
                    "content_filter: safety policy",
                )

        provider = RefusingProvider()
        gateway = LLMGateway(
            {"openai-chat": provider},
            catalog=catalog,
        )

        with self.assertRaises(LLMProviderError):
            await gateway.create_completion(primary, messages=[])
        self.assertEqual(
            [item[0].name for item in provider.calls],
            ["primary"],
        )

    async def test_concurrent_requests_keep_their_own_profiles(self) -> None:
        first = profile("first")
        second = profile(
            "second",
            protocol="anthropic-messages",
            provider="anthropic",
        )
        catalog = ModelCatalog(
            {first.name: first, second.name: second},
            default_profile=first.name,
        )

        class ConcurrentGateway:
            def __init__(self):
                self.seen = []

            async def create_completion(self, selected, **kwargs):
                await asyncio.sleep(0)
                self.seen.append((selected.name, kwargs["model"]))
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=selected.name)
                        )
                    ],
                    usage=None,
                )

        gateway = ConcurrentGateway()
        agent = importlib.import_module("src.plugins.ai_chat.agent_loop")
        with (
            patch.object(agent, "_model_catalog", catalog),
            patch.object(agent, "_llm_gateway", gateway),
        ):
            answers = await asyncio.gather(
                agent.ask_deepseek("one", [], profile=first),
                agent.ask_deepseek("two", [], profile=second),
            )

        self.assertEqual(answers, ["first", "second"])
        self.assertCountEqual(
            gateway.seen,
            [("first", "model-first"), ("second", "model-second")],
        )

    async def test_gateway_routes_by_protocol_and_closes_providers(self) -> None:
        openai_provider = RecordingProvider()
        anthropic_provider = RecordingProvider()
        gateway = LLMGateway(
            {
                "openai-chat": openai_provider,
                "anthropic-messages": anthropic_provider,
            }
        )

        await gateway.create_completion(profile("one"), messages=[])
        await gateway.create_completion(
            profile("two", protocol="anthropic-messages"),
            messages=[],
        )
        await gateway.close()

        self.assertEqual(len(openai_provider.calls), 1)
        self.assertEqual(len(anthropic_provider.calls), 1)
        self.assertTrue(openai_provider.closed)
        self.assertTrue(anthropic_provider.closed)

    async def test_gateway_enforces_declared_capabilities(self) -> None:
        provider = RecordingProvider()
        gateway = LLMGateway({"openai-chat": provider})
        text_only = profile(
            "text",
            capabilities=ModelCapabilities(
                tools=False,
                streaming=False,
                json_mode=False,
                model_listing=False,
            ),
        )

        with self.assertRaisesRegex(LLMConfigError, "tool calls"):
            await gateway.create_completion(
                text_only,
                messages=[],
                tools=[{"type": "function"}],
            )
        with self.assertRaisesRegex(LLMConfigError, "streaming"):
            await gateway.create_completion(text_only, messages=[], stream=True)
        with self.assertRaisesRegex(LLMConfigError, "JSON mode"):
            await gateway.create_completion(
                text_only,
                messages=[],
                response_format={"type": "json_object"},
            )
        await gateway.close()

    async def test_openai_clients_are_separated_by_endpoint_and_secret(self) -> None:
        first_client = AsyncMock()
        second_client = AsyncMock()
        provider = OpenAIChatProvider()
        first = profile("first", api_key="key-one")
        same_connection = profile("second", api_key="key-one")
        other_secret = profile("third", api_key="key-two")

        with patch(
            "src.plugins.ai_chat.llm_gateway.AsyncOpenAI",
            side_effect=[first_client, second_client],
        ) as constructor:
            self.assertIs(provider._client(first), first_client)
            self.assertIs(provider._client(same_connection), first_client)
            self.assertIs(provider._client(other_secret), second_client)

        self.assertEqual(constructor.call_count, 2)
        await provider.close()

    async def test_anthropic_provider_translates_tool_round_trip(self) -> None:
        selected = profile(
            "claude",
            protocol="anthropic-messages",
            provider="anthropic",
            base_url="https://api.anthropic.test",
        )
        payload = {
            "id": "msg_1",
            "model": selected.model,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 11, "output_tokens": 7},
            "content": [
                {"type": "text", "text": "我先查一下。"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "lookup",
                    "input": {"query": "hello"},
                },
            ],
        }
        response = httpx.Response(200, json=payload)
        client = SimpleNamespace(
            post=AsyncMock(return_value=response),
            aclose=AsyncMock(),
        )
        provider = AnthropicMessagesProvider()
        provider._clients[selected.client_cache_key] = client
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look something up",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ]

        result = await provider.create_completion(
            selected,
            model=selected.model,
            messages=[
                {"role": "system", "content": "system rule"},
                {"role": "user", "content": "hello"},
            ],
            tools=tools,
            tool_choice="auto",
        )

        request = client.post.await_args.kwargs
        self.assertEqual(request["json"]["system"], [{"type": "text", "text": "system rule", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(request["json"]["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(request["headers"]["x-api-key"], "key")
        self.assertEqual(result.choices[0].message.content, "我先查一下。")
        call = result.choices[0].message.tool_calls[0]
        self.assertEqual(call.function.name, "lookup")
        self.assertEqual(json.loads(call.function.arguments), {"query": "hello"})
        self.assertEqual(result.usage.total_tokens, 18)
        await provider.close()

    async def test_anthropic_rate_limit_is_normalized(self) -> None:
        selected = profile(
            "claude",
            protocol="anthropic-messages",
            provider="anthropic",
        )
        client = SimpleNamespace(
            post=AsyncMock(
                return_value=httpx.Response(
                    429,
                    json={"error": {"type": "rate_limit_error"}},
                )
            ),
            aclose=AsyncMock(),
        )
        provider = AnthropicMessagesProvider()
        provider._clients[selected.client_cache_key] = client

        with self.assertRaises(LLMRateLimitError):
            await provider.create_completion(
                selected,
                messages=[{"role": "user", "content": "hello"}],
            )
        await provider.close()


class AnthropicConversionTests(unittest.TestCase):
    def test_tool_results_and_forced_choice_use_anthropic_shapes(self) -> None:
        selected = profile(
            "claude",
            protocol="anthropic-messages",
            provider="anthropic",
            thinking="enabled",
        )
        request = _anthropic_request(
            selected,
            {
                "messages": [
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "question"},
                    {
                        "role": "assistant",
                        "content": "checking",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"query":"x"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": '{"ok":true}',
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "lookup"},
                },
            },
        )

        self.assertEqual(request["tool_choice"], {"type": "tool", "name": "lookup"})
        self.assertNotIn("thinking", request)
        assistant = request["messages"][1]
        self.assertEqual(assistant["content"][1]["type"], "tool_use")
        tool_result = request["messages"][2]
        self.assertEqual(tool_result["content"][0]["type"], "tool_result")

    def test_response_adapter_emits_openai_style_message(self) -> None:
        response = _anthropic_response(
            {
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
                "stop_reason": "end_turn",
            }
        )

        self.assertEqual(response.choices[0].message.content, "done")
        self.assertEqual(response.choices[0].message.tool_calls, [])
        self.assertEqual(response.usage.total_tokens, 5)


if __name__ == "__main__":
    unittest.main()
