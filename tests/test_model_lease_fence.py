"""Every physical model attempt respects the current durable execution owner."""
from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import AsyncOpenAI

from src.plugins.ai_chat import agent_loop
from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence
from src.plugins.ai_chat.llm_gateway import (
    AnthropicMessagesProvider,
    LLMConnectionError,
    LLMGateway,
    LLMProviderError,
    ModelCircuit,
    OpenAIChatProvider,
)
from src.plugins.ai_chat.model_budget import ModelCallBudget, model_budget_scope
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelCapabilities, ModelProfile


def profile(name="primary", *, protocol="openai-chat", provider="test"):
    return ModelProfile(
        name=name, provider=provider, protocol=protocol, model=f"model-{name}",
        base_url="http://127.0.0.1:1/v1", api_key="TEST_MODEL_KEY",
        max_output_tokens=1024,
        capabilities=ModelCapabilities.defaults_for(protocol),
    )


class ModelLeaseFenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owned = True
        self.fence = JobFence(71, "TEST_WORKER", 1, lambda: self.owned)
        token = active_job_fence.set(self.fence)
        self.addCleanup(active_job_fence.reset, token)
        self.primary, self.backup = profile(), profile("backup")
        self.provider = SimpleNamespace(create_completion=AsyncMock(return_value="ok"))
        self.gateway = LLMGateway(
            {"openai-chat": self.provider},
            catalog=ModelCatalog(
                {p.name: p for p in (self.primary, self.backup)},
                default_profile=self.primary.name,
            ),
        )

    async def test_lost_owner_never_enters_provider_and_releases_half_open_probe(self):
        self.owned = False
        self.gateway.health._circuits[self.primary.name] = ModelCircuit(
            opened_until=time.monotonic() - 1,
        )
        with self.assertRaises(LeaseLost):
            await self.gateway.create_completion(self.primary, messages=[])
        self.provider.create_completion.assert_not_awaited()
        circuit = self.gateway.health._circuits[self.primary.name]
        self.assertFalse(circuit.probe_in_flight)
        self.assertEqual(circuit.total_failures, 0)

    async def test_lease_lost_by_provider_is_not_converted_or_retried(self):
        lost = LeaseLost("fixed test lease loss")
        self.provider.create_completion.side_effect = lost
        with self.assertRaises(LeaseLost) as caught:
            await self.gateway.create_completion(self.primary, messages=[])
        self.assertIs(caught.exception, lost)
        self.provider.create_completion.assert_awaited_once()

    async def test_fallback_rechecks_owner_after_primary_failure(self):
        async def fail(*args, **kwargs):
            self.owned = False
            raise LLMConnectionError("fixed test connection failure")
        self.provider.create_completion.side_effect = fail
        with self.assertRaises(LeaseLost):
            await self.gateway.create_completion(self.primary, messages=[])
        self.provider.create_completion.assert_awaited_once()
        self.assertEqual(self.provider.create_completion.await_args.args[0], self.primary)

    async def test_tool_choice_compatibility_retry_rechecks_owner(self):
        async def fail(*args, **kwargs):
            self.owned = False
            raise LLMProviderError("test", 400, "tool_choice_not_supported")
        self.provider.create_completion.side_effect = fail
        with self.assertRaises(LeaseLost):
            await self.gateway.create_completion(
                self.primary, messages=[], tools=[{"type": "function"}],
                tool_choice={"type": "function", "function": {"name": "reply"}},
            )
        self.provider.create_completion.assert_awaited_once()

    async def test_stream_start_failure_cannot_start_backup_after_loss(self):
        owner = self

        class BrokenStream:
            close = AsyncMock()

            def __aiter__(self):
                return self

            async def __anext__(self):
                owner.owned = False
                raise httpx.ReadError("fixed test stream failure")

        stream = BrokenStream()
        self.provider.create_completion.return_value = stream
        with self.assertRaises(LeaseLost):
            await self.gateway.create_completion(self.primary, messages=[], stream=True)
        self.provider.create_completion.assert_awaited_once()
        stream.close.assert_awaited_once()

    async def test_budget_preflight_loss_prevents_reservation_and_network(self):
        async def before():
            await asyncio.sleep(0)
            self.owned = False
        budget = ModelCallBudget(20000, before_request=before, reserve_request=AsyncMock())
        with model_budget_scope(budget), self.assertRaises(LeaseLost):
            await self.gateway.create_completion(self.primary, messages=[])
        self.assertEqual(budget.calls, [])
        budget.reserve_request.assert_not_awaited()
        self.provider.create_completion.assert_not_awaited()

    async def test_budget_reservation_await_loss_prevents_network_and_finishes_reservation(self):
        async def reserve(reservation):
            await asyncio.sleep(0)
            self.owned = False
        budget = ModelCallBudget(20000, reserve_request=reserve)
        with model_budget_scope(budget), self.assertRaises(LeaseLost):
            await self.gateway.create_completion(self.primary, messages=[])
        self.assertEqual(len(budget.calls), 1)
        self.assertTrue(budget.calls[0].finished)
        self.provider.create_completion.assert_not_awaited()

    async def test_agent_plain_and_stream_requests_guard_custom_completion_port(self):
        self.owned = False
        for stream in (False, True):
            with self.subTest(stream=stream), patch.object(
                agent_loop, "_create_completion", new_callable=AsyncMock,
            ) as create, self.assertRaises(LeaseLost):
                if stream:
                    await agent_loop._create_streaming_completion(
                        AsyncMock(), self.primary, messages=[],
                    )
                else:
                    await agent_loop._invoke_completion(self.primary, messages=[])
            create.assert_not_awaited()

    async def test_empty_answer_repair_does_not_start_after_owner_loss(self):
        selected = profile(provider="qwen")

        async def empty(**kwargs):
            self.owned = False
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[], reasoning_content="test"),
                finish_reason="length",
            )])

        with patch.object(agent_loop, "_create_completion", side_effect=empty) as create:
            with patch.object(agent_loop, "_actual_profile", return_value=selected):
                with self.assertRaises(LeaseLost):
                    await agent_loop._completion_with_optional_stream(
                        None, selected, messages=[],
                    )
        create.assert_awaited_once()

    def openai_client(self):
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value="ok"))),
            models=SimpleNamespace(list=AsyncMock(return_value=SimpleNamespace(data=[]))),
        )
        client.with_options = Mock(return_value=client)
        return client

    async def test_openai_adapter_guards_direct_plain_and_stream_calls(self):
        self.owned = False
        client, provider = self.openai_client(), OpenAIChatProvider()
        with patch.object(provider, "_client", return_value=client):
            for stream in (False, True):
                with self.subTest(stream=stream), self.assertRaises(LeaseLost):
                    await provider.create_completion(self.primary, messages=[], stream=stream)
        client.chat.completions.create.assert_not_awaited()

    async def test_openai_sdk_hidden_retries_disabled_only_for_owned_execution(self):
        client, provider = self.openai_client(), OpenAIChatProvider()
        with patch.object(provider, "_client", return_value=client):
            self.assertEqual(await provider.create_completion(self.primary, messages=[]), "ok")
            client.with_options.assert_called_once_with(max_retries=0)
            client.with_options.reset_mock()
            token = active_job_fence.set(None)
            try:
                self.assertEqual(await provider.create_completion(self.primary, messages=[]), "ok")
            finally:
                active_job_fence.reset(token)
            client.with_options.assert_not_called()
        self.assertEqual(client.chat.completions.create.await_count, 2)

    async def test_route_only_fence_also_disables_sdk_hidden_retries(self):
        from src.plugins.ai_chat.agent.control import RouteFence, active_route_fence, RouteAuthorityLost
        client, provider = self.openai_client(), OpenAIChatProvider()
        job_token = active_job_fence.set(None)
        route_live = True
        route_token = active_route_fence.set(RouteFence(lambda: route_live))
        try:
            with patch.object(provider, "_client", return_value=client):
                self.assertEqual(await provider.create_completion(self.primary, messages=[]), "ok")
                client.with_options.assert_called_once_with(max_retries=0)
                route_live = False
                with self.assertRaises(RouteAuthorityLost):
                    await provider.create_completion(self.primary, messages=[])
                self.assertEqual(client.chat.completions.create.await_count, 1)
        finally:
            active_route_fence.reset(route_token)
            active_job_fence.reset(job_token)

    async def test_real_openai_sdk_does_not_hide_retry_after_server_failure(self):
        requests = []

        def respond(request):
            requests.append(request)
            self.owned = False
            return httpx.Response(500, json={"error": {"message": "test failure", "type": "server_error"}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        client = AsyncOpenAI(api_key="TEST_KEY", base_url=self.primary.base_url, http_client=http)
        provider = OpenAIChatProvider()
        try:
            with patch.object(provider, "_client", return_value=client), self.assertRaises(LLMProviderError):
                await provider.create_completion(self.primary, model=self.primary.model, messages=[])
        finally:
            await client.close()
        self.assertEqual(len(requests), 1)

    async def test_anthropic_adapter_guards_direct_network_request(self):
        self.owned = False
        selected = profile(protocol="anthropic-messages")
        provider, client = AnthropicMessagesProvider(), SimpleNamespace(post=AsyncMock())
        with patch.object(provider, "_client", return_value=client), self.assertRaises(LeaseLost):
            await provider.create_completion(selected, messages=[])
        client.post.assert_not_awaited()

    async def test_model_listing_cannot_bypass_owner_guard(self):
        self.owned = False
        client, provider = self.openai_client(), OpenAIChatProvider()
        with patch.object(provider, "_client", return_value=client), self.assertRaises(LeaseLost):
            await provider.list_models(self.primary)
        client.models.list.assert_not_awaited()
        self.provider.list_models = AsyncMock(return_value=[])
        with self.assertRaises(LeaseLost):
            await self.gateway.list_models(self.primary)
        self.provider.list_models.assert_not_awaited()

    async def test_without_fence_legacy_fallback_still_succeeds(self):
        self.provider.create_completion.side_effect = [LLMConnectionError("test failure"), "ok"]
        token = active_job_fence.set(None)
        try:
            result = await self.gateway.create_completion_with_profile(self.primary, messages=[])
        finally:
            active_job_fence.reset(token)
        self.assertEqual(result.profile, self.backup)
        self.assertEqual(result.response, "ok")
        self.assertEqual(self.provider.create_completion.await_count, 2)
