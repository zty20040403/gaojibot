"""Portable browser ownership, shutdown and optional real local-page checks."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.browser_runtime import (
    BrowserManager,
    BrowserPolicyError,
    BrowserUnavailable,
    _owner_key,
)


class BrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = BrowserManager(self.root / "profiles", max_sessions=1)
        self.contexts = []

        async def launch(**kwargs):
            page = SimpleNamespace(goto=AsyncMock(return_value=None))
            context = SimpleNamespace(
                pages=[page],
                set_default_timeout=Mock(),
                route=AsyncMock(),
                close=AsyncMock(),
            )
            self.contexts.append(context)
            return context

        self.launch = AsyncMock(side_effect=launch)
        self.manager._playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=self.launch),
            stop=AsyncMock(),
        )
        self.manager._snapshot_locked = AsyncMock(return_value={"text": "page"})
        self.addAsyncCleanup(self.manager.close)

    async def test_busy_session_cannot_be_evicted_or_closed_as_idle(self):
        await self.manager.navigate("one", "https://1.1.1.1/")
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(session, **kwargs):
            entered.set()
            await release.wait()
            return {"text": "finished"}

        self.manager._snapshot_locked.side_effect = slow
        pending = asyncio.create_task(self.manager.snapshot("one"))
        try:
            await entered.wait()
            self.manager._sessions[_owner_key("one")].touched_at = (
                time.monotonic() - 4000
            )
            self.assertEqual(await self.manager.close_idle(), 0)
            with self.assertRaisesRegex(BrowserUnavailable, "busy"):
                await self.manager.navigate("two", "https://1.1.1.1/")
            self.contexts[0].close.assert_not_awaited()
        finally:
            release.set()
            self.assertEqual((await pending)["text"], "finished")
        self.manager._snapshot_locked.side_effect = None
        await self.manager.navigate("two", "https://1.1.1.1/")
        self.contexts[0].close.assert_awaited_once()

    async def test_clear_waits_for_operation_then_removes_only_its_profile(self):
        await self.manager.navigate("one", "https://1.1.1.1/")
        profile = self.manager.profile_root / _owner_key("one")
        (profile / "cookie").write_bytes(b"login")
        other = self.manager.profile_root / _owner_key("two")
        other.mkdir()
        (other / "cookie").write_bytes(b"other")
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(session, **kwargs):
            entered.set()
            await release.wait()
            return {}

        self.manager._snapshot_locked.side_effect = slow
        operation = asyncio.create_task(self.manager.snapshot("one"))
        await entered.wait()
        cleanup = asyncio.create_task(self.manager.clear_profile("one"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(cleanup.done())
            self.assertTrue((profile / "cookie").exists())
            self.contexts[0].close.assert_not_awaited()
        finally:
            release.set()
            await operation
            self.assertTrue(await cleanup)
        self.assertFalse(profile.exists())
        self.assertEqual((other / "cookie").read_bytes(), b"other")

    async def test_lost_close_reply_keeps_profile_and_blocks_reuse_until_retry(self):
        await self.manager.navigate("one", "https://1.1.1.1/")
        profile = self.manager.profile_root / _owner_key("one")
        (profile / "cookie").write_bytes(b"keep")
        self.contexts[0].close.side_effect = OSError("lost close reply")
        with self.assertRaises(OSError):
            await self.manager.clear_profile("one")
        self.assertEqual((profile / "cookie").read_bytes(), b"keep")
        self.assertEqual(self.manager.stats()["closing_sessions"], 1)
        with self.assertRaisesRegex(BrowserUnavailable, "unconfirmed"):
            await self.manager.navigate("one", "https://1.1.1.1/")
        self.assertEqual(self.launch.await_count, 1)
        self.contexts[0].close.side_effect = None
        self.assertTrue(await self.manager.clear_profile("one"))
        self.assertFalse(profile.exists())

    async def test_cancelled_wait_releases_busy_count_but_shutdown_rejects_new_work(
        self,
    ):
        await self.manager.navigate("one", "https://1.1.1.1/")
        entered = asyncio.Event()

        async def wait(session, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.manager._snapshot_locked.side_effect = wait
        pending = asyncio.create_task(self.manager.snapshot("one"))
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.manager._sessions[_owner_key("one")].users, 0)
        await self.manager.close()
        with self.assertRaisesRegex(BrowserUnavailable, "shutting down"):
            await self.manager.navigate("one", "https://1.1.1.1/")

    async def test_single_profile_worker_rejects_another_owner_and_bounds_cache(self):
        self.manager.bound_owner = "project:1:g2"
        await self.manager.navigate("project:1:g2", "https://1.1.1.1/")
        args = self.launch.await_args.kwargs
        self.assertEqual(args["user_data_dir"], str(self.manager.profile_root))
        self.assertEqual(args["service_workers"], "block")
        self.assertFalse(args["accept_downloads"])
        self.assertIn("--disk-cache-size=33554432", args["args"])
        for method, args in (
            (self.manager.snapshot, ("other",)),
            (self.manager.close_session, ("other",)),
            (self.manager.clear_profile, ("other",)),
        ):
            with self.assertRaises(BrowserPolicyError):
                await method(*args)

    async def test_oversized_or_linked_profile_never_launches_or_deletes_foreign_data(
        self,
    ):
        profile = self.manager.profile_root / _owner_key("one")
        profile.mkdir()
        self.manager.max_profile_bytes = 1024
        (profile / "large").write_bytes(b"x" * 2048)
        with self.assertRaisesRegex(BrowserUnavailable, "size limit"):
            await self.manager.navigate("one", "https://1.1.1.1/")
        (profile / "large").unlink()
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "keep").write_bytes(b"private")
        (profile / "Default").symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(BrowserPolicyError):
            await self.manager.navigate("one", "https://1.1.1.1/")
        self.launch.assert_not_awaited()
        await self.manager.clear_profile("one")
        self.assertEqual((foreign / "keep").read_bytes(), b"private")
        profile.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(BrowserPolicyError):
            await self.manager.clear_profile("one")
        self.assertEqual((foreign / "keep").read_bytes(), b"private")

    async def test_failed_context_setup_closes_before_releasing_profile(self):
        context = SimpleNamespace(
            pages=[],
            set_default_timeout=Mock(),
            route=AsyncMock(side_effect=RuntimeError("setup failed")),
            close=AsyncMock(),
        )
        self.launch.side_effect = None
        self.launch.return_value = context
        with self.assertRaisesRegex(RuntimeError, "setup failed"):
            await self.manager.navigate("one", "https://1.1.1.1/")
        context.close.assert_awaited_once()
        self.assertEqual(self.manager.stats()["active_sessions"], 0)

    def test_module_does_not_initialize_bot_or_playwright(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import src.browser_runtime; "
                "assert 'nonebot' not in sys.modules; assert 'playwright' not in sys.modules",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class LocalPage(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"""<!doctype html><title>Project browser</title>
        <input placeholder="Display name"><button onclick="localStorage.setItem('name',
        document.querySelector('input').value);
        localStorage.setItem('clicks', (Number(localStorage.getItem('clicks')) || 0) + 1);
        document.cookie='session=local-only; Max-Age=3600; SameSite=Lax';
        location.reload()">Save</button>
        <p id="value"></p><script>document.querySelector('#value').textContent =
        (localStorage.getItem('name') || 'No saved name') + ';' + document.cookie +
        ';clicks=' + (localStorage.getItem('clicks') || '0');</script>
        <input type="password" aria-label="Password">"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(
    os.getenv("GAOJI_TEST_BROWSER_EXECUTABLE"),
    "explicit local browser executable required",
)
class RealBrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_profile_survives_restart_without_cross_project_login(self):
        with tempfile.TemporaryDirectory() as temp:
            server = ThreadingHTTPServer(("127.0.0.1", 0), LocalPage)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}/"

            def manager(name):
                return BrowserManager(
                    Path(temp) / name,
                    bound_owner=name,
                    allow_private_network=True,
                    executable_path=os.environ["GAOJI_TEST_BROWSER_EXECUTABLE"],
                    cache_bytes=4 * 1024**2,
                )

            first = manager("project-1")
            try:
                result = await first.navigate("project-1", url)
                self.assertEqual(result["title"], "Project browser")
                field = next(
                    item["ref"]
                    for item in result["elements"]
                    if item["text"] == "Display name"
                )
                result = await first.type_text("project-1", field, "Alice")
                button = next(
                    item["ref"] for item in result["elements"] if item["text"] == "Save"
                )
                saved = (await first.click("project-1", button))["text"]
                self.assertIn("Alice", saved)
                self.assertIn("session=local-only", saved)
                snapshot = await first.snapshot("project-1")
                password = next(item["ref"] for item in snapshot["elements"] if item["text"] == "Password")
                secret = "fixture-secret-must-not-enter-receipts"
                entered = await first.type_text("project-1", password, secret)
                self.assertNotIn(secret, str(entered))
                self.assertNotIn(secret, str(await first.snapshot("project-1")))
            finally:
                await first.close()
            same, other = manager("project-1"), manager("project-2")
            try:
                restored = (await same.navigate("project-1", url))["text"]
                self.assertIn("Alice", restored)
                self.assertIn("session=local-only", restored)
                separate = (await other.navigate("project-2", url))["text"]
                self.assertIn("No saved name", separate)
                self.assertNotIn("session=local-only", separate)
                await same.clear_profile("project-1")
                cleared = (await same.navigate("project-1", url))["text"]
                self.assertIn("No saved name", cleared)
                self.assertNotIn("session=local-only", cleared)
            finally:
                await same.close()
                await other.close()


if __name__ == "__main__":
    unittest.main()
