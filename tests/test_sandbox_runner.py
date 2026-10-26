"""Real local Unix transport with an in-process driver fixture. No Docker/QQ."""
from __future__ import annotations

import asyncio
import json
import importlib.util
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock

from src.sandbox_runtime.client import SandboxClient
from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError, SandboxResult, SandboxObservedManifest
from src.sandbox_runtime.protocol import ProtocolError, fingerprint, read_header, validate_request, write_frame
from src.sandbox_runtime.receipts import Receipts
from src.sandbox_runtime.server import SandboxRunner
from src.sandbox_runtime.factory import manager_from_settings


TOKEN = "test-" + "a" * 40
INSTANCE = "1" * 32
SANDBOX = "s123abc"


def request(method="create", args=None, payload=b"", identifier=None):
    return {"schema": 1, "id": identifier or uuid.uuid4().hex, "token": TOKEN,
            "method": method, "args": {"owner": "group:1:user:2"} if args is None else args, "payload_size": len(payload)}


class FixtureDriver(DockerSandboxManager):
    def __init__(self):
        super().__init__(namespace=INSTANCE, image="gaoji-app-sandbox:" + "b" * 64, max_file_bytes=1024 * 1024)
        self.created = self.deleted = self.calls = 0
        self.files = {}
        self.gate = None
        self.started = asyncio.Event()
        self._run = AsyncMock(side_effect=AssertionError("Tests must not contact Docker"))

    async def create(self, owner, runtime="python", *, purpose="task"):
        self.created += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return {"sandbox_id": SANDBOX, "runtime": runtime, "purpose": purpose}

    async def exec(self, **kwargs):
        self.calls += 1
        return SandboxResult("done", "", 0, SandboxObservedManifest(kwargs["command"], 5,
            "a" * 64, 4, "b" * 64, 0, ("main.py",), (), "none"))

    async def write_file(self, owner, sandbox_id, path, content, *, allow_large=False):
        self.files[(owner, sandbox_id, path)] = content
        return len(content)

    async def install_readonly_file(self, owner, sandbox_id, path, content):
        self.files[(owner, sandbox_id, path)] = content

    async def read_file(self, owner, sandbox_id, path, max_bytes=None):
        return self.files[(owner, sandbox_id, path)]

    async def export_artifact(self, owner, sandbox_id, path):
        return self.files[(owner, sandbox_id, path)], False

    async def reclaim_stopped(self, owner, sandbox_id, *, eligible, not_used_since=None):
        for _ in range(2):
            if not await self._eligible(eligible):
                raise SandboxError("Task changed; refusing cleanup")
        self.deleted += 1


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gj-rpc-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.driver = FixtureDriver()
        await self.start_runner()
        self.addAsyncCleanup(self.close_runner)
        self.client = SandboxClient(str(self.root / "socket/run.sock"), TOKEN, max_file_bytes=1024 * 1024)

    async def start_runner(self):
        self.runner = SandboxRunner(self.driver, socket_path=self.root / "socket/run.sock",
            state_directory=self.root / "state", token=TOKEN)
        await self.runner.start()

    async def close_runner(self):
        await asyncio.wait_for(self.runner.close(), timeout=5)

    async def raw(self, header, payload=b""):
        reader, writer = await asyncio.open_unix_connection(self.client.socket_path)
        try:
            await write_frame(writer, header, payload)
            result = await read_header(reader)
            data = await reader.readexactly(result.get("payload_size", 0))
            return result, data
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_health_and_binary_file_round_trip(self):
        health = await self.client.health()
        self.assertTrue(health["channel_ready"])
        self.assertNotIn("token", health)
        self.assertEqual(health["instance_id"], INSTANCE)
        content = bytes(range(256)) * 1024
        size = await self.client.write_file("owner", SANDBOX, "data.bin", content, allow_large=True)
        self.assertEqual(size, len(content))
        self.assertEqual(await self.client.read_file("owner", SANDBOX, "data.bin", max_bytes=None), content)
        self.assertEqual(await self.client.export_artifact("owner", SANDBOX, "data.bin"), (content, False))

    async def test_exec_restores_typed_manifest(self):
        outcome = await self.client.exec("owner", SANDBOX, "python main.py", 30, ["python3Packages.requests"])
        self.assertEqual(outcome.returncode, 0)
        self.assertEqual(outcome.manifest.changed_workspace_paths, ("main.py",))
        self.assertIsInstance(outcome.manifest, SandboxObservedManifest)

    async def test_capacity_refuses_new_writes_but_exports_existing_artifacts(self):
        from src.storage_admission import CapacityError
        await self.client.write_file("owner", SANDBOX, "report.pdf", b"already-generated")
        def blocked(_size):
            raise CapacityError("实例额度不足")
        self.runner.capacity_check = blocked
        with self.assertRaisesRegex(SandboxError, "额度"):
            await self.client.create("owner")
        with self.assertRaisesRegex(SandboxError, "额度"):
            await self.client.exec("owner", SANDBOX, "echo changed", 30)
        with self.assertRaisesRegex(SandboxError, "额度"):
            await self.client.write_file("owner", SANDBOX, "new.pdf", b"not-written")
        self.assertEqual(await self.client.export_artifact("owner", SANDBOX, "report.pdf"), (b"already-generated", False))
        self.assertEqual(self.driver.created, 0)
        self.assertEqual(self.driver.calls, 0)

    async def test_repeated_mutation_replays_saved_result_after_runner_restart(self):
        identifier = uuid.uuid4().hex
        first = await self.client.request("create", {"owner": "owner"}, request_id=identifier)
        await self.runner.close()
        await self.start_runner()
        second = await self.client.request("create", {"owner": "owner"}, request_id=identifier)
        self.assertEqual(first, second)
        self.assertEqual(self.driver.created, 1)

    async def test_conflicting_request_id_is_rejected(self):
        identifier = uuid.uuid4().hex
        await self.client.request("create", {"owner": "one"}, request_id=identifier)
        with self.assertRaisesRegex(SandboxError, "不同"):
            await self.client.request("create", {"owner": "two"}, request_id=identifier)
        self.assertEqual(self.driver.created, 1)

    async def test_concurrent_duplicate_awaits_single_execution(self):
        self.driver.gate = asyncio.Event()
        identifier = uuid.uuid4().hex
        first = asyncio.create_task(self.client.request("create", {"owner": "owner"}, request_id=identifier))
        await self.driver.started.wait()
        second = asyncio.create_task(self.client.request("create", {"owner": "owner"}, request_id=identifier))
        await asyncio.sleep(0.02)
        self.driver.gate.set()
        self.assertEqual(await first, await second)
        self.assertEqual(self.driver.created, 1)

    async def test_lost_connection_does_not_repeat_or_cancel_started_mutation(self):
        self.driver.gate = asyncio.Event()
        header = request()
        reader, writer = await asyncio.open_unix_connection(self.client.socket_path)
        await write_frame(writer, header)
        await self.driver.started.wait()
        writer.close()
        await writer.wait_closed()
        self.driver.gate.set()
        result, _ = await self.client.request(header["method"], header["args"], request_id=header["id"])
        self.assertEqual(result["sandbox_id"], SANDBOX)
        self.assertEqual(self.driver.created, 1)

    async def test_interrupted_operation_is_not_blindly_reexecuted_on_restart(self):
        self.driver.gate = asyncio.Event()
        header = request()
        reader, writer = await asyncio.open_unix_connection(self.client.socket_path)
        await write_frame(writer, header)
        await self.driver.started.wait()
        await asyncio.wait_for(self.runner.close(), timeout=5)
        writer.close()
        await writer.wait_closed()
        self.driver.gate = None
        await self.start_runner()
        with self.assertRaisesRegex(SandboxError, "尚未确认"):
            await self.client.request(header["method"], header["args"], request_id=header["id"])
        self.assertEqual(self.driver.created, 1)

    async def test_explicit_client_cancellation_cancels_work_not_just_socket(self):
        self.driver.gate = asyncio.Event()
        identifier = uuid.uuid4().hex
        operation = asyncio.create_task(self.client.request("create", {"owner": "owner"}, request_id=identifier))
        await self.driver.started.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(operation, timeout=5)
        self.assertNotIn(identifier, self.runner.inflight)
        with self.assertRaisesRegex(SandboxError, "尚未确认"):
            await self.client.request("create", {"owner": "owner"}, request_id=identifier)
        self.assertEqual(self.driver.created, 1)

    async def test_cancel_still_works_when_operation_capacity_is_full(self):
        self.runner.concurrency = 1
        self.driver.gate = asyncio.Event()
        header = request()
        reader, writer = await asyncio.open_unix_connection(self.client.socket_path)
        await write_frame(writer, header)
        await self.driver.started.wait()
        data, _ = await self.client.request("cancel", {"request_id": header["id"]})
        self.assertTrue(data["cancel_requested"])
        writer.close()
        await writer.wait_closed()

    async def test_wrong_token_never_reaches_driver(self):
        invalid = SandboxClient(self.client.socket_path, "wrong-" + "b" * 40)
        with self.assertRaisesRegex(SandboxError, "身份验证"):
            await invalid.create("owner")
        self.assertEqual(self.driver.created, 0)

    async def test_wire_rejects_arbitrary_methods_scope_images_and_mounts(self):
        for method, args in (("_run", {"command": ["docker", "run", "--privileged"]}),
                             ("create", {"owner": "owner", "image": "other"}),
                             ("create", {"owner": "owner", "namespace": "2" * 32}),
                             ("create", {"owner": "owner", "mounts": ["/:/host"]})):
            answer, _ = await self.raw(request(method, args))
            self.assertFalse(answer["ok"])
        self.assertEqual(self.driver.created, 0)
        self.driver._run.assert_not_awaited()

    async def test_oversized_incomplete_and_invalid_headers_never_execute(self):
        for data in (struct.pack("!I", 65537), struct.pack("!I", 2) + b"[]",
                     struct.pack("!I", 15) + b'{"id":1,"id":2}'):
            reader, writer = await asyncio.open_unix_connection(self.client.socket_path)
            writer.write(data)
            await writer.drain()
            answer = await read_header(reader)
            self.assertFalse(answer["ok"])
            writer.close()
            await writer.wait_closed()
        self.assertEqual(self.driver.created, 0)

    async def test_failed_eligibility_callback_keeps_workspace(self):
        replies = iter([True, True, False])
        with self.assertRaisesRegex(SandboxError, "refusing cleanup"):
            await self.client.reclaim_stopped("owner", SANDBOX, eligible=lambda: next(replies), not_used_since=10)
        self.assertEqual(self.driver.deleted, 0)

    async def test_valid_eligibility_callback_rechecks_before_cleanup(self):
        calls = 0

        def check():
            nonlocal calls
            calls += 1
            return True

        await self.client.reclaim_stopped("owner", SANDBOX, eligible=check, not_used_since=10)
        self.assertEqual(calls, 3)
        self.assertEqual(self.driver.deleted, 1)

    async def test_single_runner_lock_preserves_existing_listener(self):
        duplicate = SandboxRunner(self.driver, socket_path=self.root / "socket/run.sock",
            state_directory=self.root / "state", token=TOKEN)
        with self.assertRaises(TimeoutError):
            await duplicate.start()
        await duplicate.close()
        self.assertTrue((await self.client.health())["channel_ready"])

    async def test_private_socket_and_receipt_permissions(self):
        self.assertEqual((self.root / "socket/run.sock").stat().st_mode & 0o777, 0o660)
        self.assertEqual((self.root / "state/receipts.sqlite3").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "state").stat().st_mode & 0o777, 0o700)

    async def test_upload_contents_do_not_enter_receipt_database(self):
        secret = b"upload-private-binary-" + bytes(range(256))
        await self.client.write_file("owner", SANDBOX, "data.bin", secret)
        self.assertNotIn(secret, (self.root / "state/receipts.sqlite3").read_bytes())


class ReceiptTests(unittest.TestCase):
    def test_expired_results_leave_tombstones(self):
        with tempfile.TemporaryDirectory() as root:
            store = Receipts(Path(root))
            self.addCleanup(store.close)
            identifier, digest = "1" * 32, "a" * 64
            store.begin(identifier, digest)
            store.finish(identifier, {"ok": True, "data": {"sandbox_id": SANDBOX}})
            store.connection.execute("UPDATE requests SET updated_at=?", (int(time.time()) - 3601,))
            with self.assertRaisesRegex(ProtocolError, "过期"):
                store.lookup(identifier, digest)
            self.assertEqual(store.connection.execute("SELECT status,response FROM requests").fetchone(), ("expired", None))

    def test_receipt_file_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            target = directory / "other"
            target.write_bytes(b"keep")
            (directory / "receipts.sqlite3").symlink_to(target)
            with self.assertRaises(OSError):
                Receipts(directory)
            self.assertEqual(target.read_bytes(), b"keep")

    def test_validation_is_strict_before_execution(self):
        invalid = [request("exec", {"owner": "o", "sandbox_id": SANDBOX, "command": "id", "packages": ["a;id"]}),
                   request("read_file", {"owner": "o", "sandbox_id": SANDBOX, "path": "../etc/shadow"}),
                   {**request(), "payload_size": True}, {**request(), "schema": True}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                validate_request(value, file_limit=1024)

    def test_fingerprint_covers_command_and_binary_not_auth_token(self):
        first = request("write_file", {"owner": "o", "sandbox_id": SANDBOX, "path": "a.txt"}, b"one")
        self.assertNotEqual(fingerprint(first, b"one"), fingerprint(first, b"two"))
        self.assertEqual(fingerprint(first, b"one"), fingerprint({**first, "token": "rotated"}, b"one"))


class RuntimeTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "src/plugins/ai_chat/config.py"
        spec = importlib.util.spec_from_file_location("sandbox_config_fixture", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.settings_type = module.Settings

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("sandbox_config_fixture", None)

    def test_explicit_runner_configuration_selects_socket_client(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"AI_SANDBOX_RUNNER_SOCKET": "/run/gaoji-sandbox/runner.sock", "AI_SANDBOX_RUNNER_TOKEN": TOKEN}, clear=True):
            settings = self.settings_type.from_env()
        client = manager_from_settings(settings)
        self.assertIsInstance(client, SandboxClient)
        self.assertNotIn(TOKEN, repr(settings))

    def test_existing_configuration_keeps_original_driver(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            settings = self.settings_type.from_env()
        self.assertIsInstance(manager_from_settings(settings), DockerSandboxManager)

    def test_partial_runner_configuration_fails_without_docker_fallback(self):
        from unittest.mock import patch
        for environment in ({"AI_SANDBOX_RUNNER_SOCKET": "/run/gaoji-sandbox/runner.sock"},
                            {"AI_SANDBOX_RUNNER_TOKEN": TOKEN}):
            with patch.dict(os.environ, environment, clear=True):
                settings = self.settings_type.from_env()
            with self.assertRaises(ValueError):
                manager_from_settings(settings)


if __name__ == "__main__":
    unittest.main()
