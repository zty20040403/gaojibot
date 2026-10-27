from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.mcp_bridge.client import McpWireClient
from src.plugins.ai_chat.mcp_bridge.http_transport import McpTransportDenied
from src.plugins.ai_chat.mcp_bridge.stdio_transport import (
    CONTAINER_PREFIX, EXPIRY_LABEL, OWNER_LABEL, DockerLauncher, McpSessions,
    StdioProfile, StdioSessions, bounded_stdio,
)

IMAGE = "sha256:" + "a" * 64
OWNER = "b" * 32
NAME = CONTAINER_PREFIX + "c" * 32
PROFILE = StdioProfile(IMAGE, ("/usr/bin/example-mcp", "--stdio"))
CONFIG = {"transport": "stdio", "profile": "example"}


class IsolatedProfileTests(unittest.IsolatedAsyncioTestCase):
    def launcher(self):
        return DockerLauncher("/usr/bin/docker", "/var/run/docker.sock", OWNER)

    def test_only_pinned_images_and_predefined_profiles_can_start(self):
        for image in ("latest", "example:latest", "sha256:../other", "repo@sha256:aaa"):
            with self.assertRaises(ValueError):
                StdioProfile(image, ("tool",))
        for args in ((), ("tool\x00",), ["arbitrary-command"]):
            with self.assertRaises(ValueError):
                StdioProfile(IMAGE, args)
        with self.assertRaises(ValueError):
            DockerLauncher("docker", "tcp://host:2375", OWNER)
        with self.assertRaises(ValueError):
            StdioSessions({"../escape": PROFILE}, self.launcher())

    def test_launcher_has_no_host_mount_network_or_secret_inheritance(self):
        launcher = self.launcher()
        args = launcher.arguments(PROFILE, NAME, 1234)
        for flag in ("--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--cpus=1", "--memory=512m",
                     "--pids-limit=64", "--pull=never", "--log-driver=none"):
            self.assertIn(flag, args)
        self.assertNotIn("--privileged", args)
        self.assertFalse(any(arg.startswith(("--mount", "--volume", "--env-file")) for arg in args))
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": "secret", "HTTP_PROXY": "http://proxy", "DOCKER_HOST": "tcp://remote"}):
            env = launcher.environment()
            self.assertNotIn("AI_POSTGRES_DSN", env)
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("DOCKER_HOST", env)
            self.assertEqual(env["HOME"], "/nonexistent")

    async def test_anonymous_image_volumes_are_rejected(self):
        launcher = self.launcher()
        launcher.command = AsyncMock(return_value=b'{"/data":{}}')
        with self.assertRaises(McpTransportDenied):
            await launcher.verify(PROFILE)
        launcher.command = AsyncMock(return_value=b"null")
        await launcher.verify(PROFILE)

    async def test_cleanup_is_installation_and_name_bound(self):
        launcher = self.launcher()
        launcher.command = AsyncMock(return_value=json.dumps({OWNER_LABEL: "d" * 32}).encode())
        with self.assertRaises(McpTransportDenied):
            await launcher.remove(NAME)
        self.assertEqual(launcher.command.await_count, 1)
        with self.assertRaises(ValueError):
            await launcher.remove("someone-elses-database")
        launcher.command = AsyncMock(side_effect=[json.dumps({OWNER_LABEL: OWNER}).encode(), b""])
        await launcher.remove(NAME)
        self.assertEqual(launcher.command.call_args.args[0], ["rm", "--force", NAME])

    async def test_reaper_only_removes_expired_owned_mcp_containers(self):
        launcher = self.launcher()
        other = CONTAINER_PREFIX + "d" * 32
        active = CONTAINER_PREFIX + "e" * 32
        launcher.command = AsyncMock(side_effect=[f"{NAME}\n{other}\n{active}\nuser-container\n".encode(),
            json.dumps({OWNER_LABEL: OWNER, EXPIRY_LABEL: "99"}).encode(),
            json.dumps({OWNER_LABEL: "foreign", EXPIRY_LABEL: "99"}).encode(),
            json.dumps({OWNER_LABEL: OWNER, EXPIRY_LABEL: "101"}).encode()])
        launcher.remove = AsyncMock()
        await launcher.reap(now=100)
        launcher.remove.assert_awaited_once_with(NAME)

    async def test_unknown_profile_is_not_a_host_command(self):
        sessions = StdioSessions({"example": PROFILE}, self.launcher())
        with self.assertRaises(McpTransportDenied):
            async with sessions.connect({"transport": "stdio", "profile": "shell"}):
                self.fail("Unknown profile was started")
        with self.assertRaises(McpTransportDenied):
            McpSessions(None).connect(CONFIG)


class BoundedStdioTests(unittest.IsolatedAsyncioTestCase):
    async def process(self, source):
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", source,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=16384)
        async def cleanup():
            if process.returncode is None:
                process.kill()
            await process.wait()
        self.addAsyncCleanup(cleanup)
        return process

    async def test_unterminated_or_oversized_line_fails_without_unbounded_buffer(self):
        for source in ('print("x" * 200)', 'import sys; sys.stdout.write("x" * 200); sys.stdout.flush()'):
            process = await self.process(source)
            with patch("src.plugins.ai_chat.mcp_bridge.stdio_transport.MAX_RESPONSE_BYTES", 100):
                async with bounded_stdio(process) as (reader, _):
                    error = await asyncio.wait_for(reader.receive(), 2)
                    self.assertIsInstance(error, McpTransportDenied)

    async def test_invalid_json_is_redacted_and_session_bytes_are_bounded(self):
        process = await self.process('print("private-secret-invalid-json")')
        async with bounded_stdio(process) as (reader, _):
            error = await asyncio.wait_for(reader.receive(), 2)
            self.assertIsInstance(error, McpTransportDenied)
            self.assertNotIn("private-secret", str(error))
        process = await self.process('import sys; sys.stdout.write(" " * 101); sys.stdout.flush()')
        with patch("src.plugins.ai_chat.mcp_bridge.stdio_transport.MAX_SESSION_BYTES", 100):
            async with bounded_stdio(process) as (reader, _):
                error = await asyncio.wait_for(reader.receive(), 2)
                self.assertIsInstance(error, McpTransportDenied)

    async def test_real_sdk_stdio_round_trip_and_cleanup_on_success(self):
        source = '''from mcp.server import MCPServer
server = MCPServer("stdio-fixture")
@server.tool(structured_output=True)
def add(left: int, right: int) -> dict[str, object]:
    return {"sum": left + right}
server.run(transport="stdio")
'''
        launcher = DockerLauncher("/usr/bin/docker", "/var/run/docker.sock", OWNER)
        launcher.verify = AsyncMock()
        launcher.remove = AsyncMock()
        # Replace only the process launcher; protocol framing and both SDKs are real.
        launcher.arguments = lambda *_: [sys.executable, "-u", "-c", source]
        sessions = StdioSessions({"example": PROFILE}, launcher)
        wire = McpWireClient(sessions, timeout_seconds=10)
        catalog = await wire.discover(CONFIG)
        self.assertEqual(catalog[0]["name"], "add")
        guard = AsyncMock()
        result = await wire.call(CONFIG, catalog[0], {"left": 4, "right": 5}, before_call=guard)
        self.assertTrue(result["ok"])
        self.assertIn('"sum":9', result["content"])
        guard.assert_awaited_once()
        self.assertEqual(launcher.verify.await_count, 2)
        self.assertEqual(launcher.remove.await_count, 2)

    async def test_session_cancellation_removes_container_and_reaps_process(self):
        launcher = DockerLauncher("/usr/bin/docker", "/var/run/docker.sock", OWNER)
        launcher.verify = AsyncMock()
        launcher.remove = AsyncMock()
        launcher.arguments = lambda *_: [sys.executable, "-u", "-c", "import time; time.sleep(60)"]
        sessions = StdioSessions({"example": PROFILE}, launcher)
        started = asyncio.Event()
        async def run():
            async with sessions.connect(CONFIG):
                started.set()
                await asyncio.sleep(60)
        task = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        launcher.remove.assert_awaited_once()
