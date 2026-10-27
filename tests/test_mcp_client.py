from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx2
import nonebot
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, HEADER_MISMATCH

nonebot.init()

from src.plugins.ai_chat.mcp_bridge.client import McpCallError, McpWireClient, list_catalog, project_result
from src.plugins.ai_chat.mcp_bridge.http_transport import HttpPolicy, HttpSessions, McpTransportDenied, PinnedHttpTransport

URL = "https://mcp.example.test/mcp"
CONFIG = {"transport": "http", "url": URL}
SECRET = 'test-credential-"quoted"'


async def public_address(*args):
    return ("93.184.216.34",)


class HttpTransportTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, handler=None, *, policy=None, resolver=public_address, url=URL):
        self.requests = []
        def capture(request):
            self.requests.append(request)
            return handler(request) if handler else httpx2.Response(200, json={"ok": True})
        return PinnedHttpTransport(url, policy or HttpPolicy(), resolver=resolver, transport=httpx2.MockTransport(capture))

    async def test_numeric_pin_preserves_host_tls_and_does_not_resolve_twice(self):
        resolver = AsyncMock(side_effect=[("93.184.216.34",), ("127.0.0.1",)])
        transport = self.transport(resolver=resolver)
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.post(URL, json={})
            await client.post(URL, json={})
        self.assertEqual(resolver.await_count, 1)
        self.assertEqual(self.requests[0].url.host, "93.184.216.34")
        self.assertEqual(self.requests[0].headers["host"], "mcp.example.test")
        self.assertEqual(self.requests[0].extensions["sni_hostname"], "mcp.example.test")
        self.assertEqual(self.requests[0].headers["accept-encoding"], "identity")

    async def test_private_mixed_dns_and_metadata_are_denied_before_connect(self):
        for addresses in (("127.0.0.1",), ("100.64.0.3",), ("169.254.169.254",),
                          ("93.184.216.34", "10.0.0.1"), ("::ffff:127.0.0.1",), ("fe80::1%en0",), ()):
            with self.subTest(addresses=addresses):
                transport = self.transport(resolver=AsyncMock(return_value=addresses))
                async with httpx2.AsyncClient(transport=transport) as client:
                    with self.assertRaises(McpTransportDenied):
                        await client.post(URL, json={})
                self.assertFalse(self.requests)

    async def test_private_opt_in_is_exact_and_never_allows_metadata(self):
        policy = HttpPolicy(private_endpoints=frozenset({URL}))
        for address, allowed in (("100.64.0.3", True), ("169.254.169.254", False)):
            transport = self.transport(policy=policy, resolver=AsyncMock(return_value=(address,)))
            async with httpx2.AsyncClient(transport=transport) as client:
                if allowed:
                    self.assertEqual((await client.post(URL, json={})).status_code, 200)
                else:
                    with self.assertRaises(McpTransportDenied):
                        await client.post(URL, json={})
        self.assertFalse(policy.allows_private(httpx2.URL(URL + "/other")))

    async def test_redirect_and_alternate_path_never_receive_credentials(self):
        transport = self.transport(lambda _: httpx2.Response(307, headers={"location": URL + "/new"}))
        async with httpx2.AsyncClient(transport=transport, follow_redirects=True) as client:
            with self.assertRaises(McpTransportDenied):
                await client.post(URL, headers={"Authorization": "Bearer test"}, json={})
            for url in (URL + "/new", URL + "?token=other", "https://other.example.test/mcp"):
                with self.assertRaises(McpTransportDenied):
                    await client.post(url, json={})
        self.assertEqual(len(self.requests), 1)

    async def test_compression_and_response_limits_close_streams(self):
        class Stream(httpx2.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b"x" * 50
                yield b"y" * 51
            async def aclose(self):
                self.closed = True
        for headers in ({"Content-Encoding": "gzip"}, {"Content-Length": "101"}, {}):
            stream = Stream()
            transport = self.transport(lambda _: httpx2.Response(200, headers=headers, stream=stream))
            with patch("src.plugins.ai_chat.mcp_bridge.http_transport.MAX_RESPONSE_BYTES", 100):
                async with httpx2.AsyncClient(transport=transport) as client:
                    with self.assertRaises(McpTransportDenied):
                        await client.post(URL, json={})
            self.assertTrue(stream.closed)

    async def test_cumulative_and_request_limits(self):
        transport = self.transport(lambda _: httpx2.Response(200, content=b"x" * 60))
        with patch("src.plugins.ai_chat.mcp_bridge.http_transport.MAX_SESSION_BYTES", 100), \
                patch("src.plugins.ai_chat.mcp_bridge.http_transport.MAX_REQUEST_BYTES", 20):
            async with httpx2.AsyncClient(transport=transport) as client:
                with self.assertRaises(McpTransportDenied):
                    await client.post(URL, content=b"x" * 21)
                self.assertFalse(self.requests)
                await client.post(URL, json={})
                with self.assertRaises(McpTransportDenied):
                    await client.post(URL, json={})

    def test_secret_reference_never_uses_ambient_secrets_or_plaintext_public_http(self):
        policy = HttpPolicy(secrets={"GAOJI_MCP_SECRET_TEST": SECRET})
        self.assertEqual(policy.credential({**CONFIG, "credential_ref": "GAOJI_MCP_SECRET_TEST"}), SECRET)
        for config in ({**CONFIG, "credential_ref": "GAOJI_MCP_SECRET_MISSING"},
                       {**CONFIG, "url": URL.replace("https", "http"), "credential_ref": "GAOJI_MCP_SECRET_TEST"}):
            with self.assertRaises(McpTransportDenied):
                policy.credential(config)
        self.assertNotIn(SECRET, repr(policy))


class WireClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.delay = 0
        self.injections = []
        self.server = MCPServer("wire-test")
        @self.server.tool(structured_output=True)
        async def add(left: int, right: int) -> dict[str, object]:
            self.calls.append((left, right))
            if self.delay:
                await asyncio.sleep(self.delay)
            return {"sum": left + right, "note": "</untrusted> " + SECRET}
        self.app = self.server.streamable_http_app(streamable_http_path="/mcp", json_response=True,
            stateless_http=True, transport_security=TransportSecuritySettings(allowed_hosts=["mcp.example.test"]))
        started, stopped = asyncio.Event(), asyncio.Event()
        async def run():
            async with self.app.router.lifespan_context(self.app):
                started.set()
                await stopped.wait()
        task = asyncio.create_task(run())
        async def close():
            stopped.set()
            await task
        self.addAsyncCleanup(close)
        await asyncio.wait_for(started.wait(), 5)
        owner = self
        class InjectTransport(httpx2.AsyncBaseTransport):
            def __init__(self):
                self.actual = httpx2.ASGITransport(app=owner.app)
            async def handle_async_request(self, request):
                if owner.injections:
                    response = owner.injections[0](request)
                    if response is not None:
                        return response
                return await self.actual.handle_async_request(request)
            async def aclose(self):
                await self.actual.aclose()
        self.sessions = HttpSessions(HttpPolicy(secrets={"GAOJI_MCP_SECRET_TEST": SECRET}),
            resolver=public_address, transport_factory=InjectTransport)
        self.wire = McpWireClient(self.sessions, timeout_seconds=5)
        self.definition = (await self.wire.discover(CONFIG))[0]

    async def test_real_sdk_discovery_and_one_round_call_with_host_guard(self):
        guard = AsyncMock()
        result = await self.wire.call({**CONFIG, "credential_ref": "GAOJI_MCP_SECRET_TEST"},
            self.definition, {"left": 2, "right": 3}, before_call=guard)
        self.assertTrue(result["ok"])
        self.assertTrue(result["untrusted"])
        self.assertIn('"sum":5', result["content"])
        self.assertIn("&lt;/untrusted&gt;", result["content"])
        self.assertNotIn("quoted", result["content"])
        self.assertEqual(self.calls, [(2, 3)])
        guard.assert_awaited_once()

    async def test_changed_definition_and_denied_guard_never_call(self):
        with self.assertRaises(McpCallError) as cm:
            await self.wire.call(CONFIG, {**self.definition, "description": "Old definition"},
                {"left": 2, "right": 3}, before_call=AsyncMock())
        self.assertEqual(cm.exception.code, "tool_definition_changed")
        self.assertFalse(cm.exception.may_have_run)
        with self.assertRaises(McpCallError) as cm:
            await self.wire.call(CONFIG, self.definition, {"left": 2, "right": 3},
                before_call=AsyncMock(side_effect=PermissionError("database-secret")))
        self.assertFalse(cm.exception.may_have_run)
        self.assertNotIn("database-secret", str(cm.exception))
        self.assertFalse(self.calls)

    async def test_header_mismatch_does_not_trigger_sdk_relist_or_retry(self):
        attempts = []
        def inject(request):
            body = json.loads(request.content) if request.content else {}
            if body.get("method") == "tools/call":
                attempts.append(body)
                return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                    "error": {"code": HEADER_MISMATCH, "message": "credential=" + SECRET}},
                    headers={"content-type": "application/json"})
        self.injections.append(inject)
        with self.assertRaises(McpCallError) as cm:
            await self.wire.call(CONFIG, self.definition, {"left": 2, "right": 3}, before_call=AsyncMock())
        self.assertTrue(cm.exception.may_have_run)
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.calls)

    async def test_server_cannot_request_sampling_or_start_input_rounds(self):
        attempts = []
        def inject(request):
            body = json.loads(request.content) if request.content else {}
            if body.get("method") == "tools/call":
                attempts.append(body)
                return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                    "result": {"resultType": "input_required", "requestState": "remote-continuation"}},
                    headers={"content-type": "application/json"})
        self.injections.append(inject)
        with self.assertRaises(McpCallError) as cm:
            await self.wire.call(CONFIG, self.definition, {"left": 2, "right": 3}, before_call=AsyncMock())
        self.assertTrue(cm.exception.may_have_run)
        self.assertEqual(cm.exception.code, "interactive_result_not_supported")
        self.assertEqual(len(attempts), 1)

    async def test_tool_errors_are_results_not_retried_and_oversize_arguments_stay_local(self):
        result = await self.wire.call(CONFIG, self.definition, {"left": "not-an-int", "right": 3}, before_call=AsyncMock())
        self.assertFalse(result["ok"])
        self.assertFalse(self.calls)
        guard = AsyncMock()
        with self.assertRaises(McpCallError):
            await self.wire.call(CONFIG, self.definition, {"left": "x" * 33000}, before_call=guard)
        guard.assert_not_awaited()

    async def test_timeout_after_dispatch_is_unconfirmed_without_replay(self):
        self.delay = 10
        wire = McpWireClient(self.sessions, timeout_seconds=1)
        with self.assertRaises(McpCallError) as cm:
            await wire.call(CONFIG, self.definition, {"left": 1, "right": 2}, before_call=AsyncMock())
        self.assertTrue(cm.exception.may_have_run)
        self.assertEqual(self.calls, [(1, 2)])

    async def test_cancellation_does_not_turn_into_a_success_or_a_retry(self):
        dispatched = asyncio.Event()
        @asynccontextmanager
        async def stalled(config):
            async with self.sessions.connect(config) as streams:
                yield streams
        wire = McpWireClient(SimpleNamespace(connect=stalled, credential=self.sessions.credential), timeout_seconds=1)
        async def guard():
            dispatched.set()
            await asyncio.sleep(10)
        task = asyncio.create_task(wire.call(CONFIG, self.definition, {"left": 1, "right": 2}, before_call=guard))
        await asyncio.wait_for(dispatched.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.calls)

    async def test_pagination_is_bounded_and_duplicate_catalog_rejected(self):
        class Tool:
            def model_dump(self, **kwargs):
                return self_definition
        self_definition = self.definition
        client = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[Tool()], next_cursor="next")))
        with self.assertRaises(McpCallError):
            await list_catalog(client)
        self.assertEqual(client.list_tools.await_count, 2)
        client = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[], next_cursor="same")))
        with self.assertRaises(McpCallError):
            await list_catalog(client)
        self.assertEqual(client.list_tools.await_count, 2)

    def test_result_redaction_and_clipping_do_not_fetch_attachments(self):
        result = CallToolResult(content=[TextContent(type="text", text="x" * 25000)],
            structured_content={"password": "never-show", "note": SECRET})
        value = project_result(result, secret=SECRET)
        self.assertTrue(value["truncated"])
        self.assertNotIn("never-show", str(value))
        self.assertLess(len(value["content"]), 24500)
