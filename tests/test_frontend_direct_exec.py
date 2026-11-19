from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
import zlib
import struct
import binascii

from src.plugins.ai_chat.application.owned_sandbox import (
    ArtifactAvailability,
    ArtifactRegistration,
    GenerationLimits,
    OwnedSandboxExecutor,
)
from src.plugins.ai_chat.background_tools import workspace_owner
from src.plugins.ai_chat.conversation_window import ConversationStore
from src.plugins.ai_chat.model_catalog import ModelCapabilities, ModelCatalog, ModelProfile
from src.plugins.ai_chat.platform_types import InboundMessage, PlatformFeatures
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.sandbox import SandboxResult
from src.plugins.ai_chat.tool_images import ToolImage
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools.owned_sandbox import HANDLERS
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.test_owned_sandbox import Binding, FakeManager


def _tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = binascii.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    raw_scanlines = b"\x00\xff\x00\x00\xff\x00\x00\x00\x00\xff\x00\x00\x00\xff"
    idat = zlib.compress(raw_scanlines)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class DirectDeliveryStub:
    def __init__(self):
        self.registered = []

    async def readiness(self, binding):
        return ArtifactAvailability(True, 32 * 1024 * 1024, "native")

    async def register(self, binding, exported, *, logical_request_key, draft, draft_reason):
        self.registered.append((exported.filename, exported.mime, len(exported.content), logical_request_key))
        return ArtifactRegistration(artifact_ref="qqfile:test1234", state="committed", message_accepted=True)


class DirectSandboxManagerStub(FakeManager):
    max_file_bytes = 32 * 1024 * 1024

    def __init__(self):
        super().__init__()
        self.container_commands = []

    async def run_container_command(self, owner, sandbox_id, command, *, workdir="/work", timeout_seconds=120, input_bytes=None):
        self.container_commands.append((command, workdir, timeout_seconds, input_bytes))
        files = self.own(owner, sandbox_id)["files"]
        if command.startswith("gaoji units start "):
            out = json.dumps({
                "ok": True,
                "unit": "gaoji-cmd-test01",
                "output_file": "/work/.gaoji/out/gaoji-cmd-test01.txt",
                "said": "后台命令已启动。",
            }, ensure_ascii=False)
            return SandboxResult(returncode=0, stdout=out, stderr="")
        if command.startswith("node /etc/gaoji/runner/runner.js --video "):
            files[".gaoji/shown/clip.mp4"] = b"\x00\x00\x00\x18ftypmp42TESTVIDEO"
            out = json.dumps({"ok": True, "value": {"path": "/work/.gaoji/shown/clip.mp4", "seconds": 5, "bytes": 25}})
            return SandboxResult(returncode=0, stdout=out, stderr="")
        if command == "node /etc/gaoji/runner/runner.js":
            req = json.loads((input_bytes or b"{}").decode("utf-8"))
            if "SHOW_IMAGE" in req.get("code", ""):
                files[".gaoji/shown/pic.png"] = _tiny_png()
                out = json.dumps({
                    "ok": True,
                    "result": "shown-ok",
                    "shown": [{"kind": "image", "path": "/work/.gaoji/shown/pic.png"}],
                    "delivered": True,
                })
                return SandboxResult(returncode=0, stdout=out, stderr="console-line\n")
            out = json.dumps({"ok": True, "result": "plain-ok", "shown": [], "delivered": False})
            return SandboxResult(returncode=0, stdout=out, stderr="")
        return SandboxResult(returncode=0, stdout="SHELL_OK:" + command, stderr="")

    async def export_temp_bytes(self, owner, sandbox_id, path, *, max_bytes, delete_after=False):
        files = self.own(owner, sandbox_id)["files"]
        data = files[path]
        if delete_after:
            files.pop(path, None)
        return data


class MultimodalAndCatalogTests(unittest.TestCase):
    def test_tool_image_supports_video_and_multiple_media_parts(self):
        single_vid = ToolImage('{"ok":true}', "data:video/mp4;base64,AAAA")
        parts = single_vid.parts()
        self.assertEqual(parts[1], {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}})
        self.assertIn("video", parts[0]["text"])

        combo = ToolImage(
            '{"ok":true}',
            media_urls=("data:image/png;base64,BBBB", "data:video/mp4;base64,CCCC"),
        )
        cparts = combo.parts()
        self.assertEqual(len(cparts), 4)
        self.assertEqual(cparts[1]["type"], "image_url")
        self.assertEqual(cparts[2]["type"], "video_url")

    def test_cliproxy_gemini_enables_video_capability_by_default(self):
        catalog = ModelCatalog.from_json(
            json.dumps({
                "default": "gemini-3.8-flash-high",
                "profiles": {
                    "gemini-3.8-flash-high": {
                        "provider": "cliproxy",
                        "protocol": "openai-chat",
                        "base_url": "http://127.0.0.1:8317/v1",
                        "api_key": "TEST_KEY",
                        "model": "gemini-3.8-flash-high",
                        "capabilities": {"vision": True},
                    },
                    "gpt-6.1-sol": {
                        "provider": "cliproxy",
                        "protocol": "openai-chat",
                        "base_url": "http://127.0.0.1:8317/v1",
                        "api_key": "TEST_KEY",
                        "model": "gpt-6.1-sol",
                        "capabilities": {"vision": True},
                    },
                },
            }),
            default_profile="gemini-3.8-flash-high",
        )
        self.assertTrue(catalog.resolve("gemini-3.8-flash-high").capabilities.video)
        self.assertFalse(catalog.resolve("gpt-6.1-sol").capabilities.video)

    def test_scrub_media_urls_strips_video_blobs(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + ("A" * 1024)}},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,SHORT"}},
                ],
            }
        ]
        scrubbed = ConversationStore._scrub_media_urls(messages)
        self.assertIn("<omitted", scrubbed[0]["content"][1]["video_url"]["url"])
        self.assertEqual(scrubbed[0]["content"][2]["image_url"]["url"], "data:image/png;base64,SHORT")


class RunnerNodeIntegrationTests(unittest.TestCase):
    def test_runner_js_concurrent_promise_all_and_await_guard(self):
        if shutil.which("node") is None:
            self.skipTest("node not in PATH")
        runner_path = Path(__file__).resolve().parents[1] / "src" / "plugins" / "ai_chat" / "sandbox_runner.js"
        active_lock = threading.Lock()
        in_flight = 0
        max_concurrent = 0
        barrier = threading.Barrier(2, timeout=5)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/job/tools":
                    body = json.dumps({
                        "tools": [
                            {"function": {"name": "web_search"}},
                            {"function": {"name": "say"}},
                        ]
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                nonlocal in_flight, max_concurrent
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode())
                name = payload["name"]
                args = payload["arguments"]
                if name == "web_search":
                    with active_lock:
                        in_flight += 1
                        max_concurrent = max(max_concurrent, in_flight)
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
                    finally:
                        with active_lock:
                            in_flight -= 1
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                self.wfile.write((json.dumps({"type": "heartbeat"}) + "\n").encode())
                res = {"ok": True, "echo": (args.get("queries") or [args.get("text")])[0]}
                line = json.dumps({"type": "result", "ok": True, "content": [{"type": "text", "text": json.dumps(res)}]})
                self.wfile.write((line + "\n").encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_address[1]}"

        with TemporaryDirectory() as tmp:
            # 1) Concurrent Promise.all + delivered flag on say
            proc = subprocess.run(
                ["node", str(runner_path)],
                input=json.dumps({
                    "url": url,
                    "token": "TEST_TOKEN",
                    "work": tmp,
                    "code": """
                      console.log("starting batch");
                      const [a, b] = await Promise.all([
                        tools.web_search({ queries: ["alpha"] }),
                        tools.web_search({ queries: ["beta"] }),
                      ]);
                      await tools.say({ text: "done" });
                      return { a: a.echo, b: b.echo };
                    """,
                }).encode(),
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            env = json.loads(proc.stdout.decode().strip())
            self.assertTrue(env["ok"])
            self.assertTrue(env["delivered"])
            self.assertEqual(json.loads(env["result"]), {"a": "alpha", "b": "beta"})
            self.assertGreaterEqual(max_concurrent, 2)
            self.assertIn("starting batch", proc.stderr.decode())

            # 2) Missing await throws clear TypeError
            proc2 = subprocess.run(
                ["node", str(runner_path)],
                input=json.dumps({
                    "url": url,
                    "token": "TEST_TOKEN",
                    "work": tmp,
                    "tools": ["web_search"],
                    "code": "return tools.web_search({ queries: ['oops'] }).echo;",
                }).encode(),
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(proc2.returncode, 1)
            env2 = json.loads(proc2.stdout.decode().strip())
            self.assertFalse(env2["ok"])
            self.assertIn("await it first", env2["error"])


class OwnedSandboxDirectToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = DirectSandboxManagerStub()
        self.binding = Binding()
        self.delivery = DirectDeliveryStub()
        self.executor = OwnedSandboxExecutor(
            manager=self.manager,
            binding=self.binding,
            delivery=self.delivery,
            limits=GenerationLimits(max_file_bytes=32 * 1024 * 1024),
            clock=lambda: 100,
        )
        await self.executor.readiness()
        self.inbound = InboundMessage(
            endpoint=self.binding.endpoint,
            actor=self.binding.actor,
            event_id="EVT1",
            native_message_id=self.binding.source_native_message_id,
            sender_display="tester",
            platform_role="member",
            body=MessageBody((TextNode(0, "hi"),)),
            mentions_bot=True,
            reply_to_native_message_id=None,
            occurred_at=100,
            reply_credential=None,
        )
        platform = SimpleNamespace(account=self.binding.endpoint.account, features=PlatformFeatures())
        async def db_call(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        self.context = ToolContext(
            SimpleNamespace(context=SimpleNamespace(db_call=db_call)),
            inbound=self.inbound,
            platform=platform,
            capabilities=frozenset({"sandbox"}),
            agent_executor=self.executor,
        )
        self.registry = ToolRegistry(HANDLERS)
        self.gemini_profile = ModelProfile(
            name="gemini-3.8-flash-high",
            provider="cliproxy",
            protocol="openai-chat",
            model="gemini-3.8-flash-high",
            base_url="http://127.0.0.1:8317/v1",
            api_key="TEST_KEY",
            capabilities=ModelCapabilities(vision=True, video=True),
        )
        self.gpt_profile = replace(
            self.gemini_profile,
            name="gpt-6.1-sol",
            model="gpt-6.1-sol",
            capabilities=ModelCapabilities(vision=True, video=False),
        )

    async def test_shell_foreground_and_background(self):
        turn = TurnState(context=self.context, model_profile=self.gemini_profile)
        fg = json.loads(await self.registry.execute(turn, "shell", {"command": "echo hi", "timeout": 30}))
        self.assertTrue(fg["ok"])
        self.assertIn("SHELL_OK:echo hi", fg["stdout"])

        bg = json.loads(await self.registry.execute(
            turn, "shell", {"command": "sleep 300", "background": True, "title": "long build"},
        ))
        self.assertTrue(bg["ok"])
        self.assertEqual(bg["unit"], "gaoji-cmd-test01")

    async def test_send_image_validates_and_normalizes_extension(self):
        turn = TurnState(context=self.context, model_profile=self.gemini_profile)
        sid = (await self.manager.ensure_default(self.executor.owner))["sandbox_id"]
        await self.manager.write_file(self.executor.owner, sid, "downloaded.bin", _tiny_png())
        token = active_tool_call_id.set("CALL_IMG_1")
        try:
            res = json.loads(await self.registry.execute(turn, "send_image", {"path": "/work/downloaded.bin"}))
        finally:
            active_tool_call_id.reset(token)
        self.assertTrue(res["ok"], res)
        self.assertTrue(turn.delivered)
        self.assertEqual(self.delivery.registered[-1][0], "downloaded.png")
        self.assertEqual(self.delivery.registered[-1][1], "image/png")

    async def test_video_tool_returns_multimodal_tool_image_only_for_video_capable_model(self):
        turn_gpt = TurnState(context=self.context, model_profile=self.gpt_profile)
        denied = json.loads(await self.registry.execute(turn_gpt, "video", {"path": "/work/chat/a.mp4"}))
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["code"], "video_not_supported")

        turn_gem = TurnState(context=self.context, model_profile=self.gemini_profile)
        out = await self.registry.execute(turn_gem, "video", {"path": "/work/chat/a.mp4", "start": 0, "end": 5})
        self.assertIsInstance(out, ToolImage)
        parts = out.parts()
        self.assertEqual(parts[1]["type"], "video_url")
        self.assertTrue(parts[1]["video_url"]["url"].startswith("data:video/mp4;base64,"))

    async def test_run_code_attaches_shown_images_and_marks_delivered(self):
        turn = TurnState(context=self.context, model_profile=self.gemini_profile)
        out = await self.registry.execute(turn, "run_code", {"code": "// SHOW_IMAGE"})
        self.assertIsInstance(out, ToolImage)
        self.assertTrue(turn.delivered)
        parts = out.parts()
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_ops_safety_owner_match(self):
        owner = workspace_owner(self.inbound.endpoint)
        self.assertTrue(owner.startswith("qqo:conversation:"))
