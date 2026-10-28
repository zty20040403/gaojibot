"""Durable browser handoff and restart recovery; no production driver or account."""

import asyncio
from copy import deepcopy
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.local_runtime.files import write_json
from src.sandbox_runtime.project_browser_bundle import build_bundle
from src.sandbox_runtime.project_browser_host import BrowserPipe, ProjectBrowserHost
from src.sandbox_runtime.project_browser_journal import ProjectBrowserJournal
from src.sandbox_runtime.project_browser_worker import ProjectBrowserWorker
from src.sandbox_runtime.wire import ProtocolError, encoded, write_frame
from tests.test_browser_runtime import LocalPage
from tests.test_project_browser_worker import MemoryWriter


IDENTITY = {"instance_id": "1" * 32, "owner": "group:one", "project_id": 1,
            "generation": 1, "fence": 7}
RUNTIME = {"backend": "oci", "sandbox_id": "s123abc", "runtime_id": "a" * 64}


class FixturePipe:
    def __init__(self, worker):
        self.worker, self.reader = worker, asyncio.StreamReader()
        self.started = asyncio.Event()
        self.gate = None
        self.lose_reply = False

    async def exchange(self, frame, **kwargs):
        if "workspace" in frame:
            return self.worker.ready()
        self.started.set()
        if self.gate:
            await self.gate.wait()
        reply = await self.worker.dispatch(frame)
        if self.lose_reply:
            writer = MemoryWriter()
            await write_frame(writer, reply)
            self.reader.feed_data(writer.data)
            raise TimeoutError("observation timed out")
        return reply


class BrowserHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.journal = ProjectBrowserJournal(self.root / "host", IDENTITY["instance_id"])
        self.host = ProjectBrowserHost(self.journal)
        self.manager = SimpleNamespace(bound_owner=IDENTITY["owner"], close=AsyncMock(),
            click=AsyncMock(return_value={"text": "clicked"}),
            type_text=AsyncMock(return_value={"text": "redacted"}))
        self.worker = ProjectBrowserWorker(IDENTITY, self.manager)
        self.addAsyncCleanup(self.worker.close)
        self.channel = FixturePipe(self.worker)
        self.config = {"schema": 1, "binding": dict(IDENTITY), "workspace": str(self.root / "workspace"),
            "executable_path": "", "allow_private_network": False,
            "max_profile_bytes": 256 * 1024**2, "cache_bytes": 4 * 1024**2}
        self.published = {}

        async def launch(record):
            current = self.journal.current(IDENTITY, record["session_id"])
            self.assertEqual(current["state"], "starting")
            return self.channel

        self.launch = AsyncMock(side_effect=launch)
        self.session = await self.host.open(self.config, RUNTIME, self.launch)
        self.sid = self.session["session_id"]

    async def persist(self, sid, sequence, reply):
        old = self.published.setdefault((sid, sequence), deepcopy(reply))
        self.assertEqual(old, reply)

    async def call(self, seq=1, *, host=None, persist=None, **args):
        return await (host or self.host).execute(IDENTITY, self.sid, seq,
            "browser_click", args or {"ref": "b1"}, persist=persist or self.persist)

    async def stop(self, record, channel):
        if channel:
            await channel.worker.close()
        return {"binding": record["binding"], "runtime": record["runtime"],
                "session_id": record["session_id"], "processes_stopped": True}

    async def test_result_survives_host_restart_and_lost_application_commit_reply(self):
        async def save_then_lose(*args):
            await self.persist(*args)
            raise OSError("database commit reply lost")

        with self.assertRaises(OSError):
            await self.call(persist=save_then_lose)
        before = self.journal.current(IDENTITY)
        self.assertEqual(before["last"]["state"], "completed")
        self.assertFalse(before["last"]["acknowledged"])
        restarted = ProjectBrowserHost(ProjectBrowserJournal(self.root / "host", IDENTITY["instance_id"]))
        reply = await self.call(host=restarted)
        self.assertEqual(reply, self.published[(self.sid, 1)])
        self.assertTrue(self.journal.current(IDENTITY)["last"]["acknowledged"])
        self.manager.click.assert_awaited_once()
        self.launch.assert_awaited_once()

    async def test_unacknowledged_result_blocks_next_call_and_new_session_even_after_close(self):
        with self.assertRaises(OSError):
            await self.call(persist=AsyncMock(side_effect=OSError("database unavailable")))
        with self.assertRaises(ProtocolError):
            await self.call(2)
        await self.host.drain(IDENTITY, self.sid, self.stop)
        self.assertFalse(self.journal.maintenance_ready(IDENTITY))
        with self.assertRaises(ProtocolError):
            await self.host.open(self.config, RUNTIME, self.launch)
        await self.call()
        self.assertTrue(self.journal.maintenance_ready(IDENTITY))
        newer = self.journal.begin(self.config, RUNTIME)
        self.assertNotEqual(newer["session_id"], self.sid)
        with self.assertRaises(ProtocolError):
            self.journal.current(IDENTITY, self.sid)

    async def test_late_response_reconciles_without_resending_the_effect(self):
        self.channel.lose_reply = True
        with self.assertRaises(TimeoutError):
            await self.call()
        with self.assertRaises(ProtocolError):
            await self.call()
        reply = await self.host.receive_pending(IDENTITY, self.sid, persist=self.persist)
        self.assertEqual(reply["state"], "completed")
        self.manager.click.assert_awaited_once()
        self.assertEqual(self.journal.current(IDENTITY)["state"], "needs_attention")
        with self.assertRaises(ProtocolError):
            await self.call(2)
        await self.host.drain(IDENTITY, self.sid, self.stop)
        self.assertTrue(self.journal.maintenance_ready(IDENTITY))

    async def test_lost_pipe_never_relaunches_or_reexecutes_uncertain_action(self):
        self.channel.lose_reply = True
        with self.assertRaises(TimeoutError):
            await self.call()
        restarted = ProjectBrowserHost(self.journal)
        with self.assertRaises(ProtocolError):
            await self.call(host=restarted)
        with self.assertRaises(ProtocolError):
            await restarted.receive_pending(IDENTITY, self.sid, persist=self.persist)
        stop = AsyncMock(side_effect=self.stop)
        with self.assertRaises(ProtocolError):
            await restarted.drain(IDENTITY, self.sid, stop)
        stop.assert_not_awaited()
        await restarted.drain(IDENTITY, self.sid, stop, cancel_pending=True)
        self.assertFalse(self.journal.maintenance_ready(IDENTITY))
        with self.assertRaises(ProtocolError):
            self.journal.acknowledge(IDENTITY, self.sid, 1, "0" * 64)
        self.launch.assert_awaited_once()
        self.manager.click.assert_awaited_once()

    async def test_concurrent_duplicate_hosts_cannot_both_dispatch(self):
        self.channel.gate = asyncio.Event()
        first = asyncio.create_task(self.call())
        await self.channel.started.wait()
        try:
            with self.assertRaises((ProtocolError, TimeoutError)):
                await self.call(host=ProjectBrowserHost(self.journal))
        finally:
            self.channel.gate.set()
        original = await first
        self.assertEqual(await self.call(), original)
        self.manager.click.assert_awaited_once()
        with self.assertRaises(ProtocolError):
            await self.call(ref="b2")

    async def test_second_controller_cannot_close_an_active_operation_even_with_cancel(self):
        self.channel.gate = asyncio.Event()
        pending = asyncio.create_task(self.call())
        await self.channel.started.wait()
        stop = AsyncMock(side_effect=self.stop)
        try:
            with self.assertRaises(TimeoutError):
                await ProjectBrowserHost(self.journal).drain(
                    IDENTITY, self.sid, stop, cancel_pending=True)
            stop.assert_not_awaited()
            self.assertEqual(self.journal.current(IDENTITY)["state"], "ready")
        finally:
            self.channel.gate.set()
            await pending

    async def test_inflight_launch_cannot_be_confirmed_absent_by_another_controller(self):
        await self.host.drain(IDENTITY, self.sid, self.stop)
        entered, release = asyncio.Event(), asyncio.Event()

        async def launch(record):
            entered.set()
            await release.wait()
            return self.channel

        opening = asyncio.create_task(self.host.open(self.config, RUNTIME, launch))
        await entered.wait()
        new = self.journal.current(IDENTITY)
        stop = AsyncMock(side_effect=self.stop)
        try:
            with self.assertRaises(TimeoutError):
                await ProjectBrowserHost(self.journal).drain(
                    IDENTITY, new["session_id"], stop, cancel_pending=True)
            stop.assert_not_awaited()
            self.assertEqual(self.journal.current(IDENTITY)["state"], "starting")
        finally:
            release.set()
            await opening

    async def test_cancel_before_launch_lock_never_creates_a_process(self):
        await self.host.drain(IDENTITY, self.sid, self.stop)
        begin = self.journal.begin

        def begin_then_cancel(config, runtime):
            record = begin(config, runtime)
            sid = record["session_id"]
            self.journal.closing(IDENTITY, sid, cancel_pending=True)
            self.journal.closed(IDENTITY, sid, {"binding": IDENTITY, "runtime": RUNTIME,
                "session_id": sid, "processes_stopped": True})
            return record

        launch = AsyncMock(side_effect=AssertionError("cancelled launch must not run"))
        with patch.object(self.journal, "begin", side_effect=begin_then_cancel), self.assertRaises(ProtocolError):
            await self.host.open(self.config, RUNTIME, launch)
        launch.assert_not_awaited()
        self.assertTrue(self.journal.maintenance_ready(IDENTITY))

    async def test_worker_close_reply_is_not_host_shutdown_evidence(self):
        await self.call()
        for wrong in ({"closed": True}, {"binding": IDENTITY, "runtime": RUNTIME,
                "session_id": self.sid, "processes_stopped": 1},
                {"binding": IDENTITY, "runtime": {**RUNTIME, "runtime_id": "b" * 64},
                "session_id": self.sid, "processes_stopped": True}):
            with self.assertRaises(ProtocolError):
                await self.host.drain(IDENTITY, self.sid, AsyncMock(return_value=wrong))
            self.assertFalse(self.journal.maintenance_ready(IDENTITY))
        await self.host.drain(IDENTITY, self.sid, self.stop)
        self.assertTrue(self.journal.maintenance_ready(IDENTITY))

    async def test_cancelled_exchange_retains_intent_and_blocks_maintenance(self):
        self.channel.gate = asyncio.Event()
        pending = asyncio.create_task(self.call())
        await self.channel.started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        state = self.journal.current(IDENTITY)
        self.assertEqual(state["state"], "needs_attention")
        self.assertEqual(state["last"]["state"], "needs_attention")
        self.assertFalse(self.journal.maintenance_ready(IDENTITY))
        with self.assertRaises(ProtocolError):
            await self.call()

    async def test_call_arguments_are_not_written_to_host_journal(self):
        secret = "test-password-never-persist-arguments"
        await self.host.execute(IDENTITY, self.sid, 1, "browser_type",
            {"ref": "b1", "text": secret}, persist=self.persist)
        path = self.root / "host/p1.json"
        self.assertNotIn(secret, path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.journal.current(IDENTITY)["last"]["method"], "browser_type")

    async def test_wrong_scope_fence_runtime_or_unsafe_journal_is_rejected(self):
        for changed in ({"owner": "other"}, {"generation": 2}, {"fence": 8},
                        {"instance_id": "2" * 32}):
            with self.assertRaises(ProtocolError):
                self.journal.current({**IDENTITY, **changed}, self.sid)
        with self.assertRaises(ProtocolError):
            self.journal.begin(self.config, {**RUNTIME, "runtime_id": "container-name"})
        path = self.root / "host/p1.json"
        path.chmod(0o644)
        try:
            with self.assertRaises(ProtocolError):
                self.journal.current(IDENTITY)
        finally:
            path.chmod(0o600)
        data = path.read_bytes()
        path.unlink()
        (self.root / "foreign.json").write_bytes(data)
        path.symlink_to(self.root / "foreign.json")
        with self.assertRaises(ProtocolError):
            self.journal.current(IDENTITY)

    async def test_uncertain_launch_retains_identity_and_prevents_second_launch(self):
        await self.host.drain(IDENTITY, self.sid, self.stop)
        failed = AsyncMock(side_effect=OSError("launch acknowledgement lost"))
        with self.assertRaises(OSError):
            await self.host.open(self.config, RUNTIME, failed)
        record = self.journal.current(IDENTITY)
        self.assertEqual(record["state"], "needs_attention")
        self.assertIsNone(record["worker_id"])
        with self.assertRaises(ProtocolError):
            await self.host.open(self.config, RUNTIME, failed)
        failed.assert_awaited_once()


@unittest.skipUnless(os.getenv("GAOJI_TEST_BROWSER_EXECUTABLE"), "explicit local Chromium required")
class RealBrowserHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_result_is_durable_after_host_restart_without_reclicking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            bundle = root / "worker.pyz"
            bundle.write_bytes(build_bundle())
            journal = ProjectBrowserJournal(root / "private", IDENTITY["instance_id"])
            host = ProjectBrowserHost(journal)
            server = ThreadingHTTPServer(("127.0.0.1", 0), LocalPage)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            process = None

            async def launch(record):
                nonlocal process
                process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(bundle),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=root,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
                         "LANG": "en_US.UTF-8", "TMPDIR": str(root)})
                return BrowserPipe(process.stdout, process.stdin)

            async def stop(record, channel):
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 15)
                self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())
                # The runtime target is a driver fixture here, not native OCI proof.
                return {"binding": record["binding"], "runtime": record["runtime"],
                    "session_id": record["session_id"], "processes_stopped": True}

            config = {"schema": 1, "binding": IDENTITY, "workspace": str(workspace),
                "executable_path": os.environ["GAOJI_TEST_BROWSER_EXECUTABLE"],
                "allow_private_network": True, "max_profile_bytes": 256 * 1024**2, "cache_bytes": 4 * 1024**2}

            async def persist(sid, seq, reply):
                path = root / f"result-{seq}.json"
                if path.exists():
                    self.assertEqual(json.loads(path.read_text()), reply)
                else:
                    write_json(path, reply)

            async def lose_after_commit(*args):
                await persist(*args)
                raise OSError("receipt acknowledgement lost")

            try:
                record = await host.open(config, RUNTIME, launch)
                sid = record["session_id"]
                url = f"http://127.0.0.1:{server.server_port}/"
                await host.execute(IDENTITY, sid, 1, "browser_navigate", {"url": url}, persist=persist)
                with self.assertRaises(OSError):
                    await host.execute(IDENTITY, sid, 2, "browser_click", {"ref": "b2"}, persist=lose_after_commit)
                restarted = ProjectBrowserHost(ProjectBrowserJournal(root / "private", IDENTITY["instance_id"]))
                reply = await restarted.execute(IDENTITY, sid, 2, "browser_click", {"ref": "b2"}, persist=persist)
                self.assertIn("clicks=1", reply["result"]["text"])
                self.assertEqual(reply, json.loads((root / "result-2.json").read_text()))
                self.assertTrue(journal.current(IDENTITY)["last"]["acknowledged"])
                self.assertEqual(hashlib.sha256(encoded(reply)).hexdigest(),
                    hashlib.sha256(encoded(journal.current(IDENTITY)["last"]["response"])).hexdigest())
                await restarted.drain(IDENTITY, sid, stop)
                self.assertTrue(journal.maintenance_ready(IDENTITY))
            finally:
                if process is not None and process.returncode is None:
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), 15)
                    except TimeoutError:
                        process.kill()
                        await process.wait()


if __name__ == "__main__":
    unittest.main()
