"""Acceptance tests for §8E generate_image tool (gemini-3.1-flash-image primary, gpt-image-2.5 fallback)."""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.plugins.ai_chat.adapters.qq_official.progress import ProgressEmitter
from src.plugins.ai_chat.application.owned_sandbox import OwnedSandboxExecutor
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import InboundMessage
from src.plugins.ai_chat.tool_images import ToolImage
from src.plugins.ai_chat.tools import image_gen
from src.plugins.ai_chat.tools.image_gen import HANDLERS as IMAGE_GEN_HANDLERS
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.test_frontend_direct_exec import DirectDeliveryStub, DirectSandboxManagerStub, _tiny_png
from tests.test_owned_sandbox import Binding


class GenerateImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_chat_image_and_fallback_images_api_write_to_workspace(self):
        png_b64 = base64.b64encode(_tiny_png()).decode("ascii")
        fail_chat = False
        seen_paths: list[str] = []

        class FakeCliproxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                seen_paths.append(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                if self.path.endswith("/chat/completions"):
                    if fail_chat:
                        self.send_response(503)
                        self.end_headers()
                        self.wfile.write(b'{"error":{"message":"high demand"}}')
                        return
                    body = json.dumps({
                        "choices": [{"message": {"images": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}}]}}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 800},
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.endswith("/images/generations"):
                    body = json.dumps({
                        "data": [{"b64_json": png_b64}],
                        "usage": {"input_tokens": 20, "output_tokens": 1000},
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCliproxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base_url = f"http://127.0.0.1:{server.server_port}/v1"

        binding = Binding()
        manager = DirectSandboxManagerStub()
        delivery = DirectDeliveryStub()
        executor = OwnedSandboxExecutor(manager=manager, binding=binding, delivery=delivery, clock=lambda: 100)
        await executor.readiness()

        inbound = InboundMessage(
            endpoint=binding.endpoint,
            actor=binding.actor,
            event_id="TEST_EVT",
            native_message_id=binding.source_native_message_id,
            sender_display="User",
            platform_role="member",
            body=MessageBody((TextNode(0, "画一只橘猫"),)),
            mentions_bot=True,
            reply_to_native_message_id=None,
            occurred_at=1000,
            reply_credential=None,
        )
        recorded_usage: list[dict] = []

        async def db_call(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        catalog = ModelCatalog.from_json(
            json.dumps({
                "default": "gemini-3.8-flash-high",
                "profiles": {
                    "gemini-3.8-flash-high": {
                        "provider": "cliproxy",
                        "protocol": "openai-chat",
                        "base_url": base_url,
                        "api_key_env": "TEST_CLIPROXY_KEY",
                        "model": "gemini-3.8-flash-high",
                    }
                },
            }),
            default_profile="gemini-3.8-flash-high",
            environ={"TEST_CLIPROXY_KEY": "TEST_KEY"},
        )
        app = SimpleNamespace(
            model_catalog=catalog,
            usage_store=SimpleNamespace(record=lambda **kw: recorded_usage.append(kw)),
            db_call=db_call,
        )
        progress = ProgressEmitter(token=None, outbox=None, current=None, database_calls=None)
        progress.say = AsyncMock(return_value={"ok": False})
        platform = SimpleNamespace(account=binding.endpoint.account, features=SimpleNamespace(),
                                   progress=progress, progress_enabled=True)
        ctx = ToolContext(
            SimpleNamespace(context=app),
            capabilities=frozenset({"sandbox"}),
            inbound=inbound,
            platform=platform,
            agent_executor=executor,
        )
        registry = ToolRegistry(IMAGE_GEN_HANDLERS)
        self.assertEqual([d["function"]["name"] for d in registry.definitions(ctx)], ["generate_image"])
        turn = TurnState(ctx, model_profile=catalog.default)

        # 1. Primary gemini-3.1-flash-image via /chat/completions
        res1 = await registry.execute(turn, "generate_image", {"prompt": "一只戴墨镜的橘猫", "path": "/work/cat.png"})
        self.assertIsInstance(res1, ToolImage)
        meta1 = json.loads(res1.text)
        self.assertTrue(meta1["ok"])
        self.assertEqual(meta1["model"], "gemini-3.1-flash-image")
        self.assertEqual(meta1["path"], "/work/cat.png")
        sid = (await manager.list(executor.owner))[0]["sandbox_id"]
        self.assertEqual(manager.own(executor.owner, sid)["files"]["cat.png"], _tiny_png())
        self.assertEqual(recorded_usage[-1]["model"], "gemini-3.1-flash-image")

        # 2. Primary 503 -> automatic fallback to gpt-image-2.5 via /images/generations
        fail_chat = True
        res2 = await registry.execute(turn, "generate_image", {"prompt": "赛博朋克企鹅", "path": "memes/penguin"})
        self.assertIsInstance(res2, ToolImage)
        meta2 = json.loads(res2.text)
        self.assertTrue(meta2["ok"])
        self.assertEqual(meta2["model"], "gpt-image-2.5")
        self.assertEqual(meta2["path"], "/work/memes/penguin.png")
        self.assertEqual(manager.own(executor.owner, sid)["files"]["memes/penguin.png"], _tiny_png())
        self.assertEqual(recorded_usage[-1]["model"], "gpt-image-2.5")
        progress.say.assert_awaited_once_with("出图服务失败，我再尝试备用服务。")
        self.assertFalse(turn.delivered)  # enqueue is not a delivery receipt

    async def test_safe_errors_for_both_routes(self):
        secret = "TEST_KEY Authorization Bearer TEST_TOKEN https://TEST_USER:TEST_PASS@TEST_HOST/?sig=TEST_SIG"
        prompt = "TEST_PRIVATE_PROMPT"
        payloads = [
            ({"error": {"code": "content_policy_violation", "message": f"Content policy {secret} {prompt}"}},
             "code=content_policy_violation", "message=content policy", True),
            ({"error": {"code": "insufficient_quota", "message": f"Quota exceeded {secret}"}},
             "code=insufficient_quota", "message=quota exceeded", False),
            ({"error": {"status": "IMAGE_RECITATION", "message": "Copyright restriction"}},
             "code=image_recitation", "message=copyright", True),
            ({"error": {"code": secret, "message": "TEST_BASE64_AAAAAAA"}}, None, None, False),
            (f"<html>{secret} {prompt}</html>", None, None, False),
            ([secret], None, None, False),
        ]
        for route in (image_gen._generate_via_chat, image_gen._generate_via_images_api):
            for body, code, excerpt, restricted in payloads:
                with self.subTest(route=route.__name__, body=body):
                    response = httpx.Response(400, text=body) if isinstance(body, str) else httpx.Response(400, json=body)
                    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
                        with self.assertRaises(image_gen.ImageProviderError) as caught:
                            await route(client, "https://TEST_HOST", "TEST_KEY", "TEST_MODEL", prompt)
                    result = str(caught.exception)
                    self.assertEqual(caught.exception.restricted, restricted)
                    if code:
                        self.assertIn(code, result)
                    if excerpt:
                        self.assertIn(excerpt, result)
                    for forbidden in ("TEST_KEY", "Authorization", "TEST_TOKEN", "TEST_USER", "TEST_PASS",
                                      "TEST_HOST", "TEST_SIG", prompt, "TEST_BASE64", "<html>"):
                        self.assertNotIn(forbidden, result)
                    self.assertLess(len(result), 150)

    async def test_http_success_with_filtered_finish_reason_is_not_empty_or_retryable(self):
        body = {'choices': [{'finish_reason': 'content_filter', 'message': {}}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            with self.assertRaisesRegex(image_gen.ImageProviderError, 'code=content_filter') as caught:
                await image_gen._generate_via_chat(client, 'https://example.test', 'TEST_KEY', 'TEST', 'TEST')
        self.assertTrue(caught.exception.restricted)

    async def test_restrictions_stop_and_transient_errors_fallback(self):
        cases = [
            ([{"code": "content_policy_violation", "message": "Content policy"}], 1, "content_policy_violation"),
            ([{"status": "SAFETY", "message": "blocked"}], 1, "code=safety"),
            ([{"message": "Copyright restriction"}], 1, "message=copyright"),
            ([{"message": "high demand"}, {"code": "insufficient_quota", "message": "quota exceeded"}],
             2, "code=insufficient_quota"),
            ([{"message": "high demand"}, {"code": "copyright_violation", "message": "copyright"}],
             2, "code=copyright_violation"),
            ([None, None], 2, "images_api_error"),
        ]
        for errors, count, expected in cases:
            paths = []
            def respond(request):
                paths.append(request.url.path)
                error = errors[len(paths) - 1]
                if error is None:
                    raise httpx.ConnectError("TEST_KEY TEST_PRIVATE_PROMPT https://TEST_HOST/?sig=TEST_SIG")
                return httpx.Response(503 if len(paths) == 1 else 429, json={"error": error})
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            turn = SimpleNamespace(context=SimpleNamespace(platform=None))
            profile = SimpleNamespace(base_url="https://TEST_HOST", api_key="TEST_KEY")
            with self.subTest(errors=errors), patch.object(image_gen, "enabled", return_value=True), \
                    patch.object(image_gen, "_cliproxy_profile", return_value=profile), \
                    patch.object(image_gen.httpx, "AsyncClient", return_value=client):
                result = json.loads(await image_gen.execute(turn, {"prompt": "TEST_PRIVATE_PROMPT"}))
            self.assertEqual(len(paths), count)
            self.assertIn(expected, result["error"])
            self.assertNotIn("TEST_KEY", result["error"])
            self.assertNotIn("TEST_PRIVATE_PROMPT", result["error"])
            self.assertNotIn("TEST_HOST", result["error"])
            self.assertNotIn("TEST_SIG", result["error"])
