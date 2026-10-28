"""Private browser transport without server access or personal login profiles."""

import asyncio
from copy import deepcopy
from http.server import ThreadingHTTPServer
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import zipfile

from src.sandbox_runtime.project_browser_bundle import MODULES, build_bundle
from src.sandbox_runtime.project_browser_worker import (
    FRAME_LIMIT, ProjectBrowserWorker, configured_worker, serve,
)
from src.sandbox_runtime.wire import ProtocolError, encoded, read_header, write_frame
from tests.test_browser_runtime import LocalPage


IDENTITY = {"instance_id": "fixture", "owner": "project:one", "project_id": 1,
            "generation": 1, "fence": 2}


def request(worker, seq, method="browser_snapshot", **args):
    return {"schema": 1, "worker_id": worker.worker_id, "sequence": seq,
            "method": method, "args": args}


class MemoryWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass


class BrowserWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = SimpleNamespace(
            bound_owner=IDENTITY["owner"], snapshot=AsyncMock(return_value={"text": "page"}),
            click=AsyncMock(return_value={"text": "clicked"}), close=AsyncMock(),
        )
        self.worker = ProjectBrowserWorker(IDENTITY, self.manager)
        self.addAsyncCleanup(self.worker.close)

    async def test_same_sequence_replays_once_and_cannot_be_rebound(self):
        action = request(self.worker, 1, "browser_click", ref="b1")
        first, replay = await asyncio.gather(self.worker.dispatch(action), self.worker.dispatch(action))
        self.assertEqual(first, replay)
        self.assertEqual(first["state"], "completed")
        self.manager.click.assert_awaited_once_with(IDENTITY["owner"], ref="b1")
        first["result"]["text"] = "tamper"
        self.assertEqual((await self.worker.dispatch(action))["result"]["text"], "clicked")
        with self.assertRaises(ProtocolError):
            await self.worker.dispatch(request(self.worker, 1, "browser_click", ref="b2"))
        await self.worker.dispatch(request(self.worker, 2))
        with self.assertRaises(ProtocolError):
            await self.worker.dispatch(action)

    async def test_unknown_effect_is_not_repeated_or_followed_by_new_operations(self):
        self.manager.click.side_effect = RuntimeError("typed-secret-and-private-page")
        action = request(self.worker, 1, "browser_click", ref="b1")
        reply = await self.worker.dispatch(action)
        self.assertEqual(reply["state"], "needs_attention")
        self.assertNotIn("typed-secret", str(reply))
        self.assertEqual(await self.worker.dispatch(action), reply)
        with self.assertRaises(ProtocolError):
            await self.worker.dispatch(request(self.worker, 2))
        self.manager.click.assert_awaited_once()
        self.manager.snapshot.assert_not_awaited()

    async def test_cancelled_effect_stays_uncertain_and_close_waits_for_inflight(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()

        self.manager.click.side_effect = slow
        action = request(self.worker, 1, "browser_click", ref="b1")
        pending = asyncio.create_task(self.worker.dispatch(action))
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual((await self.worker.dispatch(action))["state"], "needs_attention")
        await self.worker.close()
        with self.assertRaises(ProtocolError):
            await self.worker.dispatch(action)

        worker = ProjectBrowserWorker(IDENTITY, self.manager)
        self.addAsyncCleanup(worker.close)
        entered.clear()
        pending = asyncio.create_task(worker.dispatch(request(worker, 1, "browser_click", ref="b1")))
        await entered.wait()
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await pending
        await closing

    async def test_previous_incarnation_and_invalid_requests_never_execute(self):
        old = ProjectBrowserWorker(IDENTITY, self.manager)
        cases = [request(old, 1), request(self.worker, 2), request(self.worker, True),
                 request(self.worker, 1, "evaluate", code="steal()"),
                 request(self.worker, 1, "browser_clear", owner="foreign"),
                 request(self.worker, 1, "browser_type", ref="b1", text="hello", submit=1),
                 request(self.worker, 1, "browser_scroll", amount=True),
                 request(self.worker, 1, "browser_click", ref="body"),
                 request(self.worker, 1, "browser_snapshot", profile_root="/foreign")]
        for action in cases:
            with self.subTest(action=action), self.assertRaises(ProtocolError):
                await self.worker.dispatch(action)
        self.assertEqual(self.worker.sequence, 0)
        self.manager.snapshot.assert_not_awaited()

    async def test_oversized_reply_is_uncertain_not_a_partial_success(self):
        self.manager.snapshot.return_value = {"text": "x" * FRAME_LIMIT}
        reply = await self.worker.dispatch(request(self.worker, 1))
        self.assertEqual(reply["state"], "needs_attention")
        self.assertLess(len(encoded(reply)), 1000)

    async def test_eof_malformed_frame_and_idle_close_the_worker(self):
        for tail in (b"", struct.pack("!I", FRAME_LIMIT + 1),
                     struct.pack("!I", 13) + b'{"a":1,"a":2}'):
            worker = ProjectBrowserWorker(IDENTITY, self.manager)
            reader, writer = asyncio.StreamReader(), MemoryWriter()
            reader.feed_data(struct.pack("!I", 2) + b"{}" + tail)
            reader.feed_eof()
            if tail:
                with self.assertRaises(ProtocolError):
                    await serve(reader, writer, factory=lambda _: worker)
            else:
                await serve(reader, writer, factory=lambda _: worker)
            self.assertTrue(worker.closed)
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", 2) + b"{}")
        worker = ProjectBrowserWorker(IDENTITY, self.manager)
        await serve(reader, MemoryWriter(), idle_seconds=0.01, factory=lambda _: worker)
        self.assertTrue(worker.closed)

    async def test_failed_close_is_not_reported_as_normal_exit(self):
        self.manager.close.side_effect = RuntimeError("close failed")
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", 2) + b"{}")
        reader.feed_eof()
        try:
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                await serve(reader, MemoryWriter(), factory=lambda _: self.worker)
        finally:
            self.manager.close.side_effect = None

    def test_bundle_is_deterministic_and_contains_no_bot_or_config(self):
        data = build_bundle()
        self.assertEqual(data, build_bundle())
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(set(archive.namelist()), {*MODULES, "src/__init__.py",
                "src/sandbox_runtime/__init__.py", "__main__.py"})
        self.assertLess(len(data), 32 * 1024)

    async def test_profile_is_inside_workspace_and_rejects_linked_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            config = {"schema": 1, "binding": deepcopy(IDENTITY), "workspace": str(root),
                      "executable_path": "", "allow_private_network": False,
                      "max_profile_bytes": 256 * 1024**2, "cache_bytes": 4 * 1024**2}
            worker = configured_worker(config)
            self.assertEqual(worker.manager.profile_root, root / ".gaoji/browser")
            await worker.close()
            (root / ".gaoji/browser").rmdir()
            (root / ".gaoji").rmdir()
            (root / "outside").mkdir()
            (root / ".gaoji").symlink_to(root / "outside", target_is_directory=True)
            with self.assertRaises(ProtocolError):
                configured_worker(config)
            self.assertEqual(list((root / "outside").iterdir()), [])


@unittest.skipUnless(os.getenv("GAOJI_TEST_BROWSER_EXECUTABLE"), "explicit local Chromium required")
class RealBrowserWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_bundle_click_replay_restart_and_project_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            bundle = root / "worker.pyz"
            bundle.write_bytes(build_bundle())
            server = ThreadingHTTPServer(("127.0.0.1", 0), LocalPage)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}/"

            async def start(name):
                workspace = root / name
                workspace.mkdir(exist_ok=True)
                process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(bundle),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=root,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
                         "LANG": "en_US.UTF-8", "TMPDIR": str(root)})

                async def finish():
                    if process.returncode is None:
                        process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), 15)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                        self.fail("Browser worker did not close after pipe EOF")
                    self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())

                self.addAsyncCleanup(finish)
                await write_frame(process.stdin, {"schema": 1, "binding": {**IDENTITY, "owner": name},
                    "workspace": str(workspace), "executable_path": os.environ["GAOJI_TEST_BROWSER_EXECUTABLE"],
                    "allow_private_network": True, "max_profile_bytes": 256 * 1024**2,
                    "cache_bytes": 4 * 1024**2})
                ready = await read_header(process.stdout)
                self.assertEqual(ready["binding"]["owner"], name)
                return process, SimpleNamespace(worker_id=ready["worker_id"]), finish

            async def call(process, message):
                await write_frame(process.stdin, message)
                reply = await read_header(process.stdout, timeout=40)
                self.assertEqual(reply["state"], "completed", reply)
                self.assertEqual(reply["sequence"], message["sequence"])
                self.assertEqual(reply["worker_id"], message["worker_id"])
                return reply

            first, worker, finish = await start("one")
            page = (await call(first, request(worker, 1, "browser_navigate", url=url)))["result"]
            field = next(item["ref"] for item in page["elements"] if item["text"] == "Display name")
            page = (await call(first, request(worker, 2, "browser_type", ref=field, text="Alice")))["result"]
            button = next(item["ref"] for item in page["elements"] if item["text"] == "Save")
            action = request(worker, 3, "browser_click", ref=button)
            result = await call(first, action)
            self.assertIn("Alice", result["result"]["text"])
            self.assertIn("session=local-only", result["result"]["text"])
            self.assertEqual(await call(first, action), result)
            page = (await call(first, request(worker, 4)))["result"]
            self.assertIn("clicks=1", page["text"])
            await finish()

            for name in ("one", "two"):
                process, new, close = await start(name)
                self.assertNotEqual(new.worker_id, worker.worker_id)
                page = (await call(process, request(new, 1, "browser_navigate", url=url)))["result"]
                if name == "one":
                    self.assertIn("Alice", page["text"])
                    self.assertIn("session=local-only", page["text"])
                    self.assertIn("clicks=1", page["text"])
                else:
                    self.assertNotIn("Alice", page["text"])
                    self.assertNotIn("session=local-only", page["text"])
                    self.assertIn("clicks=0", page["text"])
                await close()


if __name__ == "__main__":
    unittest.main()
