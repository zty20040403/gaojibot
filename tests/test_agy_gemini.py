"""Gemini on the agy gateway: signatures survive a tool round and the background relay routes to it."""
import json
import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from src.plugins.ai_chat.agent_loop import (
    _assistant_final_message,
    _assistant_tool_message,
    _create_streaming_completion,
)
from src.plugins.ai_chat.job_gateway import JobGateway
from src.plugins.ai_chat.llm_gateway import OpenAIChatProvider, _leading_system_only
from src.plugins.ai_chat.model_catalog import ModelCapabilities, ModelCatalog
from src.plugins.ai_chat.pi_engine import background_route
from tests.test_job_gateway import free_port
from tests.test_llm_gateway import profile

AGY_PARTS = {"agy": {"model": "gemini-4-argon-high", "parts": ["TEST_NATIVE_PART"]}}
CALL_SIGNATURE = {"google": {"thought_signature": "TEST_SIGNATURE"}, **AGY_PARTS}


def agy_profile(name="gemini-4-argon-high", **overrides):
    return profile(
        name, provider="agy", base_url="https://agy.test/agy/v1", api_key="TEST_AGY_KEY",
        capabilities=ModelCapabilities.defaults_for("openai-chat", provider="agy", model=name),
        **overrides,
    )


class AgyCapabilityTests(unittest.TestCase):
    def test_agy_gemini_takes_video_but_no_late_system_messages(self):
        caps = ModelCapabilities.defaults_for("openai-chat", provider="agy", model="gemini-4-argon-high")
        self.assertTrue(caps.video)
        self.assertFalse(caps.mid_system)
        self.assertTrue(ModelCapabilities.defaults_for("openai-chat", provider="cliproxy").mid_system)

    def test_late_system_notes_become_labelled_user_turns(self):
        messages = [
            {"role": "system", "content": "TEST_PERSONA"},
            {"role": "user", "content": "TEST_QUESTION"},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "system", "content": "TEST_WRAP_UP"},
        ]
        folded = _leading_system_only(messages)
        self.assertEqual(folded[0], messages[0])
        self.assertEqual(folded[3], {"role": "user", "content": "[系统提示]\nTEST_WRAP_UP"})
        self.assertEqual(messages[3]["role"], "system")

    def test_catalog_profile_keeps_mid_system_flag(self):
        catalog = ModelCatalog.from_json(json.dumps({"default": "gemini-4-neon-low", "profiles": {
            "gemini-4-neon-low": {"provider": "agy", "model": "gemini-4-neon-low",
                                  "base_url": "https://agy.test/agy/v1", "api_key_env": "TEST_AGY_ENV"},
        }}), default_profile="", environ={"TEST_AGY_ENV": "TEST_AGY_KEY"})
        self.assertFalse(catalog.resolve("gemini-4-neon-low").capabilities.mid_system)


class AgyChatRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_agy_request_never_sends_a_late_system_message(self):
        provider = OpenAIChatProvider()
        seen = {}

        class Completions:
            async def create(self, **request):
                seen.update(request)
                return "TEST_RESPONSE"

        class Client:
            chat = type("Chat", (), {"completions": Completions()})()

            def with_options(self, **_options):
                return self

        with patch.object(provider, "_client", return_value=Client()), \
                patch("src.plugins.ai_chat.llm_gateway._assert_model_job_owned"):
            await provider.create_completion(agy_profile(), model="gemini-4-argon-high", messages=[
                {"role": "system", "content": "TEST_PERSONA"},
                {"role": "user", "content": "TEST_QUESTION"},
                {"role": "system", "content": "TEST_STOP"},
            ])
        self.assertEqual([m["role"] for m in seen["messages"]], ["system", "user", "user"])


class AgySignatureReplayTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_message_keeps_message_and_call_metadata(self):
        response = ChatCompletion.model_validate({
            "id": "TEST", "object": "chat.completion", "created": 0, "model": "gemini-4-argon-high",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "extra_content": AGY_PARTS,
                "tool_calls": [{"id": "TEST_CALL", "type": "function", "extra_content": CALL_SIGNATURE,
                                "function": {"name": "TEST_TOOL", "arguments": "{\"n\": 1}"}}],
            }}],
        })
        payload = _assistant_tool_message(response.choices[0].message)
        self.assertEqual(payload["extra_content"], AGY_PARTS)
        self.assertEqual(payload["tool_calls"][0]["extra_content"], CALL_SIGNATURE)
        self.assertEqual(json.loads(json.dumps(payload))["tool_calls"][0]["function"]["name"], "TEST_TOOL")

    def test_final_message_keeps_metadata(self):
        message = ChatCompletion.model_validate({
            "id": "TEST", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "TEST_ANSWER", "extra_content": AGY_PARTS}}],
        }).choices[0].message
        self.assertEqual(_assistant_final_message(message)["extra_content"], AGY_PARTS)

    async def test_stream_collects_terminal_metadata_without_doubling_arguments(self):
        def chunk(delta, finish=None):
            return ChatCompletionChunk.model_validate({
                "id": "TEST", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            })

        chunks = [
            chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "TEST_CALL", "type": "function",
                                                         "function": {"name": "TEST_TOOL", "arguments": "{\"n\""}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]}),
            # The terminal chunk repeats only index and metadata.
            chunk({"extra_content": AGY_PARTS,
                   "tool_calls": [{"index": 0, "extra_content": CALL_SIGNATURE}]}, "tool_calls"),
        ]

        class Stream:
            def __init__(self):
                self.items = iter(chunks)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration:
                    raise StopAsyncIteration from None

            async def close(self):
                pass

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock(return_value=Stream())):
            response, _ = await _create_streaming_completion(AsyncMock(), agy_profile(), messages=[], model="m")
        message = response.choices[0].message
        payload = _assistant_tool_message(message)
        self.assertEqual(payload["extra_content"], AGY_PARTS)
        call = payload["tool_calls"][0]
        self.assertEqual((call["id"], call["function"]), ("TEST_CALL", {"name": "TEST_TOOL", "arguments": "{\"n\": 1}"}))
        self.assertEqual(call["extra_content"], CALL_SIGNATURE)

    async def test_stream_without_metadata_adds_nothing(self):
        stream_chunk = ChatCompletionChunk.model_validate({
            "id": "TEST", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {"content": "TEST_OK"}, "finish_reason": "stop"}],
        })

        class Stream:
            done = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.done:
                    raise StopAsyncIteration
                self.done = True
                return stream_chunk

            async def close(self):
                pass

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=AsyncMock(return_value=Stream())):
            response, _ = await _create_streaming_completion(AsyncMock(), profile("plain"), messages=[], model="m")
        self.assertNotIn("extra_content", _assistant_final_message(response.choices[0].message))


class BackgroundRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []

        def upstream(request):
            self.seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={"content-type": "text/event-stream"})

        routes = {"gemini-4-argon-high": agy_profile()}
        self.gateway = JobGateway(
            port=free_port(),
            upstream=lambda: profile("relay", provider="cliproxy", base_url="http://cliproxy.test/v1",
                                     api_key="TEST_CLIPROXY_KEY"),
            model=lambda: (["gemini-4-argon-high", "TEST_GPT"], "high"),
            route=routes.get, logger=logging.getLogger(__name__),
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        )
        await self.gateway.start()
        self.addAsyncCleanup(self.gateway.close)
        self.base = f"http://127.0.0.1:{self.gateway.port}/v1"

    async def test_gemini_entry_goes_to_agy_with_its_own_key_and_preset(self):
        token, _ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            reply = await client.post(self.base + "/responses", headers={"Authorization": "Bearer " + token}, json={
                "model": "gaoji-background", "input": [], "include": ["reasoning.encrypted_content"],
                "reasoning": {"effort": "xhigh", "summary": "auto"}})
        self.assertEqual(reply.status_code, 200)
        url, auth, body = self.seen[0]
        self.assertEqual((url, auth), ("https://agy.test/agy/v1/responses", "Bearer TEST_AGY_KEY"))
        self.assertEqual(body["model"], "model-gemini-4-argon-high")
        self.assertEqual(body["reasoning"], {"summary": "auto"})
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])

    async def test_compaction_skips_agy_and_uses_the_next_entry(self):
        token, _ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            reply = await client.post(self.base + "/responses/compact", json={"input": []},
                                      headers={"Authorization": "Bearer " + token})
        self.assertEqual(reply.status_code, 200)
        url, auth, body = self.seen[0]
        self.assertEqual((url, auth, body["model"]),
                         ("http://cliproxy.test/v1/responses/compact", "Bearer TEST_CLIPROXY_KEY", "TEST_GPT"))
        self.assertEqual(body["reasoning"], {"effort": "high"})

    def test_route_only_names_configured_responses_profiles(self):
        catalog = ModelCatalog.from_json(json.dumps({"default": "gemini-4-argon-high", "profiles": {
            "gemini-4-argon-high": {"provider": "agy", "model": "gemini-4-argon-high",
                                    "base_url": "https://agy.test/agy/v1", "api_key_env": "TEST_AGY_ENV"},
            "gemini-4-neon-low": {"provider": "agy", "model": "gemini-4-neon-low",
                                  "base_url": "https://agy.test/agy/v1", "api_key_env": "TEST_MISSING_ENV"},
            "ds": {"provider": "deepseek", "model": "deepseek-v4-flash",
                   "base_url": "https://deepseek.test", "api_key_env": "TEST_AGY_ENV"},
        }}), default_profile="", environ={"TEST_AGY_ENV": "TEST_AGY_KEY"})
        self.assertEqual(background_route(catalog, "gemini-4-argon-high").api_key, "TEST_AGY_KEY")
        self.assertIsNone(background_route(catalog, "gemini-4-neon-low"))
        self.assertIsNone(background_route(catalog, "ds"))
        self.assertIsNone(background_route(catalog, "rewind/gpt-6-astra"))


if __name__ == "__main__":
    unittest.main()
