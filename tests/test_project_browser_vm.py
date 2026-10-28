"""Real host journals and Unix frames; QGA/libvirt are explicit fixtures."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_browser_vm import VmProjectBrowser
from src.sandbox_runtime.project_browser_worker import ProjectBrowserWorker
from src.sandbox_runtime.project_vm_browser_guest import CHANNEL_NAME, HELPER_PATH, SOCKET_NAME, _unit
from src.sandbox_runtime.project_vm_runtime import VmProjectRuntime
from src.sandbox_runtime.wire import ProtocolError, encoded, read_header, write_frame
from tests.test_project_vm_provision import INSTANCE, RESERVATION, VmProvisionFixture


class BrowserVmWireFixture:
    async def start_browser_fixture(self, directory):
        self.guest_sessions = {}
        self.controls = []
        self.wire_tasks = set()
        self.engine = SimpleNamespace(bound_owner="scope", click=AsyncMock(return_value={"text": "clicked"}),
                                      snapshot=AsyncMock(return_value={"text": "page"}), close=AsyncMock())
        self.server = await asyncio.start_unix_server(self.wire, path=directory / SOCKET_NAME)
        self.addAsyncCleanup(self.stop_browser_fixture)
        self.original_guest = self.manager._guest_exec_raw
        self.manager._guest_exec_raw = AsyncMock(side_effect=self.guest_browser)

    async def stop_browser_fixture(self):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.wire_tasks):
            task.cancel()
        await asyncio.gather(*self.wire_tasks, return_exceptions=True)

    async def wire(self, reader, writer):
        task = asyncio.current_task()
        self.wire_tasks.add(task)
        try:
            config = await read_header(reader)
            self.engine.bound_owner = config["binding"]["owner"]
            worker = ProjectBrowserWorker(config["binding"], self.engine)
            current = next(reversed(self.guest_sessions.values()))
            current["state"] = "running"
            await write_frame(writer, worker.ready())
            while True:
                frame = await read_header(reader)
                await write_frame(writer, await worker.dispatch(frame))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.wire_tasks.discard(task)

    async def guest_browser(self, sid, path, args, **kwargs):
        if args != ["-I", HELPER_PATH]:
            return await self.original_guest(sid, path, args, **kwargs)
        request = json.loads(kwargs["input_bytes"])
        self.controls.append(request)
        metadata = json.loads((self.manager._directory(sid) / "metadata.json").read_text())
        self.assertIn(kwargs["domain_uuid"], self.domains)
        identity = {k: metadata["project"][k] for k in ("project_id", "generation", "filesystem_uuid", "serial")}
        identity.update(epoch=request["epoch"], verified=True)
        action = request["action"]
        if action == "prepare":
            result = {**identity, "toolchain_ready": True}
        else:
            session = request["session_id"]
            if action in {"start", "stop"}:
                result = {**identity, "session_id": session, "tasks": request["tasks"],
                          "state": "starting" if action == "start" else "closed", "unit": _unit(session),
                          "unit_present": action == "start", "unit_active": action == "start",
                          "cgroup_empty": action == "stop", "admission_active": True}
                self.guest_sessions[session] = result
            result = self.guest_sessions[session]
        return 0, json.dumps(result).encode(), b""


class VmBrowserTests(BrowserVmWireFixture, VmProvisionFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager._guest_exec_raw = AsyncMock(side_effect=self.guest)
        await self.boot()
        self.store.reserve("scope", 1, generation=1, fence=4, action="release", reservation=RESERVATION)
        self.runtime = VmProjectRuntime(self.manager, self.store, host=self.host)
        self.browser = self.runtime.browser
        await self.start_browser_fixture(self.directory())
        self.addAsyncCleanup(self.browser.close)
        self.target = dict(owner="scope", project_id=1, generation=1, fence=4, expected_instance_id=INSTANCE)
        self.identity = {"instance_id": INSTANCE, **{k: v for k, v in self.target.items() if k != "expected_instance_id"}}

    async def opened(self, launch="a" * 32):
        return await self.browser.project_browser("open", **self.target, launch_id=launch)

    async def call(self, record, *, browser=None):
        return await (browser or self.browser).project_browser("execute", **self.target,
            session_id=record["session_id"], sequence=1, action="browser_click", arguments={"ref": "b1"})

    async def test_wire_lifecycle_persisted_result_recovery_and_only_browser_is_stopped(self):
        record = await self.opened()
        self.assertEqual(record["runtime"]["backend"], "vm")
        reply = await self.call(record)
        await self.browser.host.channels.pop(record["session_id"]).eof()
        restarted = VmProjectBrowser(self.runtime)
        self.assertEqual(await self.call(record, browser=restarted), reply)
        self.engine.click.assert_awaited_once()
        await restarted.project_browser("ack", **self.target, session_id=record["session_id"], sequence=1,
                                        response_sha256=hashlib.sha256(encoded(reply)).hexdigest())
        closed = await restarted.project_browser("drain", **self.target, session_id=record["session_id"])
        self.assertEqual(closed["state"], "closed")
        self.assertTrue(all(d["state"] == "running" for d in self.domains.values()))
        self.assertTrue(self.browser.journal.maintenance_ready(self.identity))
        self.assertEqual(sum(c["action"] == "start" for c in self.controls), 1)

    async def test_launch_replay_never_starts_again_even_after_later_session(self):
        first = await self.opened()
        self.assertEqual(await self.opened(), first)
        await self.browser.drain(self.identity, first["session_id"])
        second = await self.opened("b" * 32)
        await self.browser.drain(self.identity, second["session_id"])
        with self.assertRaisesRegex(ProtocolError, "consumed"):
            await self.opened()
        self.assertEqual(sum(c["action"] == "start" for c in self.controls), 2)

    async def test_lost_start_reply_is_observed_and_stopped_without_relaunch(self):
        async def lost(*args, **kwargs):
            result = await self.guest_browser(*args, **kwargs)
            if json.loads(kwargs["input_bytes"])["action"] == "start":
                raise OSError("lost QGA reply")
            return result
        self.manager._guest_exec_raw.side_effect = lost
        with self.assertRaises(OSError):
            await self.opened()
        record = await self.opened()
        self.assertEqual(record["state"], "needs_attention")
        self.manager._guest_exec_raw.side_effect = self.guest_browser
        await VmProjectBrowser(self.runtime).drain(self.identity, record["session_id"], cancel_pending=True)
        self.assertEqual(sum(c["action"] == "start" for c in self.controls), 1)
        self.engine.click.assert_not_awaited()

    async def test_crash_before_start_is_cancelled_without_creating_a_browser(self):
        with patch.object(self.browser, "_connect", side_effect=AssertionError("must not connect")):
            original = self.browser._control
            async def fail(manager, boot, epoch, action, session=None):
                if action == "start":
                    raise OSError("before QGA send")
                return await original(manager, boot, epoch, action, session)
            with patch.object(self.browser, "_control", side_effect=fail), self.assertRaises(OSError):
                await self.opened()
        record = await self.opened()
        await self.browser.drain(self.identity, record["session_id"], cancel_pending=True)
        self.assertFalse(any(c["action"] == "start" for c in self.controls))

    async def test_revoked_guest_admission_blocks_effects(self):
        record = await self.opened()
        self.guest_sessions[record["session_id"]]["admission_active"] = False
        with self.assertRaisesRegex(SandboxError, "admitted"):
            await self.call(record)
        self.engine.click.assert_not_awaited()
        self.assertEqual(self.browser.journal.current(self.identity)["state"], "needs_attention")

    async def test_changed_domain_or_missing_channel_rejected_before_preparation(self):
        identifier, domain = next(iter(self.domains.items()))
        root = ElementTree.fromstring(domain["xml"])
        devices = root.find("devices")
        for channel in devices.findall("channel"):
            if channel.find("target").get("name") == CHANNEL_NAME:
                devices.remove(channel)
        self.domains[identifier]["xml"] = ElementTree.tostring(root, encoding="unicode")
        with self.assertRaises(SandboxError):
            await self.opened()
        self.assertEqual(self.controls, [])

    async def test_wrong_identity_unknown_arguments_and_capacity_fail_before_guest(self):
        for changes in ({"expected_instance_id": "f" * 32}, {"generation": True}, {"extra": 1},
                        {"fence": 3}, {"owner": "another"}, {"request_id": "b" * 32}):
            with self.subTest(changes=changes), self.assertRaises((SandboxError, ValueError)):
                await self.browser.project_browser("open", **{**self.target, **changes}, launch_id="a" * 32)
        from src.storage_admission import CapacityError
        self.store.capacity_check = lambda _: (_ for _ in ()).throw(CapacityError("full"))
        with self.assertRaises(SandboxError):
            await self.opened()
        self.assertEqual(self.controls, [])

    async def test_stop_reply_requires_independent_empty_cgroup(self):
        record = await self.opened()
        original = self.guest_browser
        async def occupied(*args, **kwargs):
            code, raw, err = await original(*args, **kwargs)
            value = json.loads(raw)
            if json.loads(kwargs["input_bytes"])["action"] == "stop":
                value["cgroup_empty"] = False
            return code, json.dumps(value).encode(), err
        self.manager._guest_exec_raw.side_effect = occupied
        with self.assertRaisesRegex(SandboxError, "termination"):
            await self.browser.drain(self.identity, record["session_id"])
        self.assertFalse(self.browser.journal.maintenance_ready(self.identity))
        self.manager._guest_exec_raw.side_effect = original
        self.assertEqual((await self.browser.drain(self.identity, record["session_id"]))["state"], "closed")

    async def test_actual_qga_dispatch_and_poll_address_the_same_immutable_uuid(self):
        from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
        manager = VmSandboxManager(image=str(self.base), root=str(self.root / "probe"))
        domain = next(iter(self.domains))
        manager._virsh = AsyncMock(side_effect=[json.dumps({"return": {"pid": 42}}),
                                              json.dumps({"return": {"exited": True, "exitcode": 0}})])
        self.assertEqual(await manager._guest_exec_raw("s123abc", "/usr/bin/true", [], domain_uuid=domain), (0, b"", b""))
        for call in manager._virsh.call_args_list:
            self.assertEqual(call.args[:2], ("qemu-agent-command", domain))
        for invalid in (True, "another-vm", domain.upper()):
            with self.assertRaises(SandboxError):
                await manager._agent("s123abc", {}, domain_uuid=invalid)
