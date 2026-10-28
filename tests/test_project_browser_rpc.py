"""Real Unix RPC and durable journals; Docker/kernel effects are explicit fixtures."""

import asyncio
import hashlib
import struct
import sys
import uuid
import unittest
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime.client import SandboxClient
from src.sandbox_runtime.docker import SandboxError
from src.sandbox_runtime.project_browser_host import BrowserPipe
from src.sandbox_runtime.project_browser_oci import OciProjectBrowser
from src.sandbox_runtime.protocol import MUTATIONS, validate_request
from src.sandbox_runtime.server import SandboxRunner
from src.sandbox_runtime.wire import encoded, write_frame
from src.storage_admission import CapacityError
from tests import test_project_browser_oci as fixtures
from tests.test_sandbox_runner import TOKEN, request
from tests.test_project_browser_worker import MemoryWriter


class BrowserRpcTests(unittest.IsolatedAsyncioTestCase):
    desktop = False
    volume_directory = "state/project-volumes"
    spawn = fixtures.BrowserOciTests.spawn

    async def asyncSetUp(self):
        await fixtures.BrowserOciTests.asyncSetUp(self)
        (self.root / "state").chmod(0o700)
        self.client = SandboxClient(str(self.root / "socket/run.sock"), TOKEN)
        await self.start_runner()
        self.addAsyncCleanup(self.close_runner)

    async def start_runner(self):
        self.runner = SandboxRunner(self.manager, socket_path=self.root / "socket/run.sock",
            state_directory=self.root / "state", token=TOKEN, project_volumes=self.volumes)
        self.workspaces = self.manager.project_workspaces
        self.browser = self.workspaces.browser
        self.browser._spawn = AsyncMock(side_effect=self.spawn)
        await self.runner.start()

    async def close_runner(self):
        await asyncio.wait_for(self.runner.close(), 5)

    async def rpc(self, operation, **params):
        return await self.client.project_browser(operation, "scope", 1, generation=1, fence=3, **params)

    async def open(self):
        self.launch_id = uuid.uuid4().hex
        self.session = await self.rpc("open", launch_id=self.launch_id)
        self.sid = self.session["session_id"]
        return self.session

    async def call(self, sequence=1, **kwargs):
        return await self.rpc("execute", session_id=self.sid, sequence=sequence,
                              action="browser_click", arguments=kwargs or {"ref": "b1"})

    async def ack(self, reply):
        return await self.rpc("ack", session_id=self.sid, sequence=reply["sequence"],
                              response_sha256=hashlib.sha256(encoded(reply)).hexdigest())

    async def status(self):
        return await self.rpc("status", session_id=self.sid)

    async def test_capability_and_two_phase_ack_block_new_effect_until_commit(self):
        self.assertEqual((await self.client.health())["project_browser_protocol"], 1)
        session = await self.open()
        self.assertEqual(session["launch_id"], self.launch_id)
        self.assertNotIn("executable_path", encoded(session).decode())
        reply = await self.call()
        self.assertFalse((await self.status())["last"]["acknowledged"])
        with self.assertRaises(SandboxError):
            await self.call(2)
        with self.assertRaises(SandboxError):
            await self.rpc("ack", session_id=self.sid, sequence=1, response_sha256="0" * 64)
        self.assertEqual(await self.call(), reply)
        self.engine.click.assert_awaited_once()
        self.assertTrue((await self.ack(reply))["last"]["acknowledged"])
        await self.ack(reply)
        second = await self.call(2)
        self.assertEqual(self.engine.click.await_count, 2)
        with self.assertRaises(SandboxError):
            await self.rpc("receive", session_id=self.sid, sequence=1)
        await self.ack(second)
        await self.rpc("drain", session_id=self.sid)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_transport_loss_replays_observation_without_ack_or_second_click(self):
        await self.open()
        dropped = False

        async def lose_once(writer, header, payload=b"", **kwargs):
            nonlocal dropped
            if header.get("data", {}).get("arguments_sha256") and not dropped:
                dropped = True
                raise OSError("RPC response was lost")
            await write_frame(writer, header, payload, **kwargs)

        with patch("src.sandbox_runtime.server.write_frame", side_effect=lose_once):
            reply = await self.call()
        self.assertTrue(dropped)
        self.engine.click.assert_awaited_once()
        self.assertFalse((await self.status())["last"]["acknowledged"])
        await self.ack(reply)

    async def test_restart_drains_sidecar_but_retains_unpublished_receipt(self):
        await self.open()
        original = await self.call()
        await self.close_runner()
        self.assertEqual(len(self.docker.containers), 1)
        self.assertTrue(self.parent["State"]["Running"])
        await self.start_runner()
        self.assertEqual((await self.status())["state"], "closed")
        self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))
        self.assertEqual(await self.call(), original)
        self.engine.click.assert_awaited_once()
        self.browser._spawn.assert_not_awaited()
        await self.ack(original)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_lost_launch_connection_recovers_same_identity(self):
        entered, gate = asyncio.Event(), asyncio.Event()

        async def gated(record, spec):
            entered.set()
            await gate.wait()
            return await self.spawn(record, spec)

        self.browser._spawn.side_effect = gated
        launch = uuid.uuid4().hex
        header = request("project_browser_open", {"owner": "scope", "project_id": 1,
            "generation": 1, "fence": 3, "launch_id": launch}, identifier=launch)
        _, writer = await asyncio.open_unix_connection(self.client.socket_path)
        try:
            await write_frame(writer, header)
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            writer.close()
            await writer.wait_closed()
        status = await self.rpc("status", launch_id=launch)
        self.assertEqual(status["state"], "starting")
        gate.set()
        result = await self.rpc("open", launch_id=launch)
        self.assertEqual(result["session_id"], status["session_id"])
        self.browser._spawn.assert_awaited_once()

    async def test_cancelled_launch_remains_correlated_after_restart(self):
        entered = asyncio.Event()

        async def blocked(record, spec):
            entered.set()
            await asyncio.Event().wait()

        self.browser._spawn.side_effect = blocked
        launch = uuid.uuid4().hex
        operation = asyncio.create_task(self.rpc("open", launch_id=launch))
        await asyncio.wait_for(entered.wait(), 5)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        await self.close_runner()
        await self.start_runner()
        with self.assertRaises(SandboxError):
            await self.rpc("open", launch_id=launch)
        result = await self.rpc("status", launch_id=launch)
        self.assertEqual(result["state"], "closed")
        self.browser._spawn.assert_not_awaited()

    async def test_old_launch_tombstone_prevents_reuse_after_new_session(self):
        original = await self.open()
        old_launch = self.launch_id
        await self.rpc("drain", session_id=self.sid)
        await self.open()
        self.assertEqual(await self.rpc("open", launch_id=old_launch), original)
        self.assertNotEqual(original["session_id"], self.sid)
        with self.assertRaises(SandboxError):
            await self.rpc("status", launch_id=old_launch)
        self.runner.receipts.connection.execute("UPDATE requests SET updated_at=0 WHERE id=?", (old_launch,))
        with self.assertRaisesRegex(SandboxError, "过期"):
            await self.rpc("open", launch_id=old_launch)
        self.assertEqual(self.browser._spawn.await_count, 2)

    async def test_identity_and_argument_reuse_are_rejected(self):
        await self.open()
        await self.call()
        for changes in ({"owner": "other"}, {"project_id": 2}, {"generation": 2}, {"fence": 4}):
            target = {"owner": "scope", "project_id": 1, "generation": 1, "fence": 3, **changes}
            with self.subTest(changes=changes), self.assertRaises(SandboxError):
                await self.client.project_browser("status", **target, session_id=self.sid)
        with self.assertRaises(SandboxError):
            await self.call(ref="b2")
        with self.assertRaises(SandboxError):
            await self.rpc("status", launch_id="f" * 32)
        self.engine.click.assert_awaited_once()

    async def test_expected_runner_is_checked_before_launch_or_action(self):
        wrong = "f" * 32 if self.manager.namespace != "f" * 32 else "e" * 32
        with self.assertRaisesRegex(SandboxError, "instance changed"):
            await self.rpc("open", launch_id=uuid.uuid4().hex, expected_instance_id=wrong)
        self.browser._spawn.assert_not_awaited()
        await self.open()
        with self.assertRaisesRegex(SandboxError, "instance changed"):
            await self.rpc("execute", session_id=self.sid, sequence=1,
                           action="browser_click", arguments={"ref": "b1"}, expected_instance_id=wrong)
        self.engine.click.assert_not_awaited()
        reply = await self.rpc("execute", session_id=self.sid, sequence=1,
                              action="browser_click", arguments={"ref": "b1"},
                              expected_instance_id=self.manager.namespace)
        self.assertEqual(reply["state"], "completed")

    async def test_capacity_rejects_only_new_effect_not_receipt_recovery(self):
        await self.open()
        reply = await self.call()
        def full(size):
            raise CapacityError("quota full")
        self.runner.capacity_check = full
        self.assertEqual(await self.call(), reply)
        self.assertEqual(await self.rpc("receive", session_id=self.sid, sequence=1), reply)
        await self.ack(reply)
        with self.assertRaisesRegex(SandboxError, "quota full"):
            await self.call(2)
        self.engine.click.assert_awaited_once()
        await self.rpc("drain", session_id=self.sid, cancel_pending=True)
        self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_late_pipe_receipt_recovery_is_not_hidden_by_generic_rpc_cache(self):
        await self.open()
        self.channel.lose_reply = True
        with self.assertRaises(SandboxError):
            await self.call()
        with self.assertRaises(SandboxError):
            await self.call()
        reply = await self.rpc("receive", session_id=self.sid, sequence=1)
        await self.ack(reply)
        self.assertEqual(await self.call(), reply)
        self.engine.click.assert_awaited_once()
        self.assertEqual((await self.status())["state"], "needs_attention")
        await self.rpc("drain", session_id=self.sid)

    async def test_unknown_creation_can_be_located_by_launch_id_without_relaunch(self):
        self.docker.after["create"] = TimeoutError("create ACK lost")
        launch = uuid.uuid4().hex
        with self.assertRaises(SandboxError):
            await self.rpc("open", launch_id=launch)
        record = await self.rpc("status", launch_id=launch)
        self.assertEqual(record["state"], "needs_attention")
        with self.assertRaises(SandboxError):
            await self.rpc("open", launch_id=launch)
        self.browser._spawn.assert_not_awaited()
        await self.rpc("drain", session_id=record["session_id"], cancel_pending=True)
        self.assertEqual(len(self.docker.containers), 1)

    async def test_disabled_project_runtime_never_falls_back_to_host_browser(self):
        self.manager.project_workspaces = None
        try:
            self.assertIsNone((await self.client.health())["project_browser_protocol"])
            with self.assertRaisesRegex(SandboxError, "unavailable"):
                await self.rpc("open", launch_id=uuid.uuid4().hex)
        finally:
            self.manager.project_workspaces = self.workspaces
        self.browser._spawn.assert_not_awaited()

    async def test_shutdown_does_not_remove_foreign_container_or_erase_uncertainty(self):
        await self.open()
        spec = self.browser._load(self.session)
        self.docker.containers[spec["name"]]["Config"]["Labels"]["gaoji.app.browser-session"] = "foreign"
        with self.assertLogs("src.sandbox_runtime.project_browser_oci", level="WARNING"):
            await self.close_runner()
        self.assertEqual(self.volumes.browsers.current(self.identity)["state"], "needs_attention")
        self.assertTrue(self.docker.containers[spec["name"]]["State"]["Running"])
        self.assertTrue(self.parent["State"]["Running"])
        self.assertFalse((self.root / "socket/run.sock").exists())

    async def test_wrong_token_cannot_read_a_browser_receipt(self):
        await self.open()
        await self.call()
        invalid = SandboxClient(self.client.socket_path, "other-" + "b" * 40)
        with self.assertRaisesRegex(SandboxError, "身份验证"):
            await invalid.project_browser("status", "scope", 1, generation=1, fence=3, session_id=self.sid)
        self.assertFalse((await self.status())["last"]["acknowledged"])


class BrowserRpcProtocolTests(unittest.TestCase):
    def test_allowlist_and_strict_operation_identity(self):
        target = {"owner": "scope", "project_id": 1, "generation": 1, "fence": 3}
        action = {**target, "session_id": "b" * 32, "sequence": 1,
                  "action": "browser_click", "arguments": {"ref": "b1"}}
        for method, args in (
            ("project_browser_open", {**target, "launch_id": "a" * 32}),
            ("project_browser_open", {**target, "launch_id": "a" * 32, "workspace": "/etc"}),
            ("project_browser_status", target),
            ("project_browser_status", {**target, "launch_id": "a" * 32, "session_id": "b" * 32}),
            ("project_browser_execute", {**action, "action": "evaluate"}),
            ("project_browser_execute", {**action, "arguments": {"ref": "b1", "script": "evil"}}),
            ("project_browser_execute", {**action, "sequence": True}),
            ("project_browser_execute", {**action, "sequence": 0}),
            ("project_browser_drain", {**target, "session_id": "../../tmp"}),
            ("project_browser_drain", {**target, "session_id": "b" * 32, "cancel_pending": 1}),
            ("project_browser_ack", {**target, "session_id": "b" * 32, "sequence": 1,
                                     "response_sha256": "not-a-digest"}),
        ):
            with self.subTest(method=method, args=args), self.assertRaises(SandboxError):
                validate_request(request(method, args), file_limit=1024)
        good = request("project_browser_open", {**target, "launch_id": "a" * 32}, identifier="a" * 32)
        validate_request(good, file_limit=1024)
        with self.assertRaises(SandboxError):
            validate_request({**good, "payload_size": 1}, file_limit=1024)
        self.assertEqual({key for key in MUTATIONS if key.startswith("project_browser_")}, {"project_browser_open"})


class BrowserPipeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_frame_timeout_then_receive_keeps_original_boundary(self):
        reader, writer = asyncio.StreamReader(), MemoryWriter()
        writer.close, writer.wait_closed = lambda: None, AsyncMock()
        pipe = BrowserPipe(reader, writer)
        reply = {"result": "received only once"}
        body = encoded(reply)
        reader.feed_data(struct.pack("!I", len(body)) + body[:4])
        with self.assertRaises(TimeoutError):
            await pipe.exchange({"action": "one"}, timeout=0.02)
        self.assertFalse(pipe.pending.done())
        sent = bytes(writer.data)
        reader.feed_data(body[4:])
        self.assertEqual(await pipe.receive(), reply)
        self.assertEqual(bytes(writer.data), sent)
        await pipe.eof()

    async def test_cancelled_caller_does_not_cancel_half_read_frame(self):
        reader, writer = asyncio.StreamReader(), MemoryWriter()
        writer.close, writer.wait_closed = lambda: None, AsyncMock()
        pipe = BrowserPipe(reader, writer)
        body = encoded({"result": "late"})
        reader.feed_data(struct.pack("!I", len(body)) + body[:2])
        operation = asyncio.create_task(pipe.exchange({"action": "one"}))
        await asyncio.sleep(0.02)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertFalse(pipe.pending.done())
        reader.feed_data(body[2:])
        self.assertEqual(await pipe.receive(), {"result": "late"})
        await pipe.eof()

    async def test_close_cancels_reader_without_claiming_effect_success(self):
        reader, writer = asyncio.StreamReader(), MemoryWriter()
        writer.close, writer.wait_closed = lambda: None, AsyncMock()
        pipe = BrowserPipe(reader, writer)
        with self.assertRaises(TimeoutError):
            await pipe.exchange({"action": "one"}, timeout=0.01)
        pending = pipe.pending
        await pipe.eof()
        self.assertTrue(pending.cancelled())
        self.assertIsNone(pipe.pending)

    async def test_attach_client_is_reaped_with_full_stdout_and_no_stderr_pipe(self):
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c",
            "import sys; sys.stdout.buffer.write(b'x'*262144); sys.stdout.flush(); sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            await OciProjectBrowser._reap(process)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
