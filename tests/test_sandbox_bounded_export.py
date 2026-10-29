from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime import artifact_export as export, source_fence as fence
from src.sandbox_runtime import client as ClientModule, protocol as Protocol
from src.sandbox_runtime.docker import DockerSandboxManager as Docker
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager as VM
from src.plugins.ai_chat.agent.control import LeaseLost


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'report.pdf').write_bytes(b'%PDF-TEST')
    def test_exact_regular_file_bytes_and_limit(self):
        self.assertEqual(export.bounded_regular_file(self.root, 'report.pdf', 9), b'%PDF-TEST')
        with self.assertRaises(ValueError):
            export.bounded_regular_file(self.root, 'report.pdf', 8)
    def test_zero_bool_and_unbounded_limits_rejected(self):
        for limit in (0, -1, True, None, 256 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError):
                export.bounded_regular_file(self.root, 'report.pdf', limit)
    def test_file_and_parent_symlinks_and_directory_rejected(self):
        (self.root / 'link').symlink_to('report.pdf')
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'file').write_bytes(b'content')
        (self.root / 'parent-link').symlink_to('sub', target_is_directory=True)
        for path in ('link', 'parent-link/file', 'sub'):
            with self.assertRaises((ValueError, OSError)):
                export.bounded_regular_file(self.root, path, 100)
    def test_no_blocking_read_of_fifo(self):
        os.mkfifo(self.root / 'pipe')
        with self.assertRaises(ValueError):
            export.bounded_regular_file(self.root, 'pipe', 100)
    def test_absolute_and_traversal_paths_rejected(self):
        for path in ('/etc/passwd', '../report.pdf', './report.pdf', 'sub/../report.pdf', 'sub//x', 'a\\b', ''):
            with self.assertRaises(ValueError):
                export.bounded_regular_file(self.root, path, 100)
    def test_file_growth_during_read_is_rejected(self):
        real_read = os.read
        once = False
        def growing(fd, count):
            nonlocal once
            result = real_read(fd, count)
            if not once:
                once = True
                with (self.root / 'report.pdf').open('ab') as stream:
                    stream.write(b'MORE')
            return result
        with patch.object(os, 'read', growing), self.assertRaises(ValueError):
            export.bounded_regular_file(self.root, 'report.pdf', 100)
    def test_embedded_guest_script_uses_same_safe_reader(self):
        # Execute the actual guest source with a test filesystem root. No guest,
        # host shell or subprocess is started; only the fixed root is substituted.
        source = export.EXPORT_FILE_SCRIPT.replace("bounded_regular_file('/workspace',", "bounded_regular_file(TEST_ROOT,")
        output = io.BytesIO()
        with patch('sys.argv', ['script', 'report.pdf', '100']), patch('sys.stdout', SimpleNamespace(buffer=output)):
            exec(source, {'TEST_ROOT': self.root})
        self.assertEqual(output.getvalue(), b'%PDF-TEST')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_bounded_branch_passes_only_owned_path_and_cap(self):
        driver = object.__new__(Docker)
        driver.max_file_bytes, driver._exec_locks = 50, {}
        driver._owned_container = AsyncMock(return_value='TEST_CONTAINER')
        driver._run_bytes = AsyncMock(return_value=(b'%PDF-TEST', b'', 0))
        content, directory = await driver.export_artifact('TEST_OWNER', 's000001', 'result.pdf', file_only=True, max_bytes=100)
        self.assertEqual(content, b'%PDF-TEST')
        self.assertFalse(directory)
        driver._owned_container.assert_awaited_once_with('TEST_OWNER', 's000001')
        args = driver._run_bytes.await_args.args
        self.assertEqual(args[-2:], ('result.pdf', '50'))
        self.assertIs(args[-3], export.EXPORT_FILE_SCRIPT)
        self.assertEqual(args[:3], ('docker', 'exec', 'TEST_CONTAINER'))

    async def test_docker_legacy_branch_unchanged(self):
        driver = object.__new__(Docker)
        driver._exec_locks = {}
        driver._export_artifact = AsyncMock(return_value=(b'old zip bytes', True))
        self.assertEqual(await driver.export_artifact('OWNER', 's000001', 'folder'), (b'old zip bytes', True))
        driver._export_artifact.assert_awaited_once()

    async def test_vm_bounded_branch_uses_same_script_and_owned_guard(self):
        driver = object.__new__(VM)
        driver.max_file_bytes = 40
        @asynccontextmanager
        async def locked(sid):
            yield
        driver.io_lock = locked
        driver._owned_running = AsyncMock(return_value={})
        driver._guest_exec = AsyncMock(return_value=(0, b'PK_TEST', b''))
        result = await driver.export_artifact('OWNER', 's000001', 'result.zip', file_only=True, max_bytes=100)
        self.assertEqual(result, (b'PK_TEST', False))
        driver._owned_running.assert_awaited_once_with('OWNER', 's000001')
        args = driver._guest_exec.await_args.args
        self.assertEqual(args[:2], ('s000001', '/usr/bin/python3'))
        self.assertEqual(args[2][-2:], ['result.zip', '40'])
        self.assertIs(args[2][2], export.EXPORT_FILE_SCRIPT)

    async def test_vm_legacy_branch_unchanged(self):
        driver = object.__new__(VM)
        @asynccontextmanager
        async def locked(sid):
            yield
        driver.io_lock = locked
        driver._export_artifact_locked = AsyncMock(return_value=(b'old', True))
        self.assertEqual(await driver.export_artifact('OWNER', 's000001', 'folder'), (b'old', True))

    async def test_rpc_passes_whitelisted_bounded_fields_and_checks_result(self):
        client = ClientModule.SandboxClient('/tmp/TEST_SOCK', 'T' * 32, max_file_bytes=1024)
        client.request = AsyncMock(return_value=({'directory': False}, b'PDF_TEST'))
        self.assertEqual(await client.export_artifact('OWNER', 's000001', 'a.pdf', file_only=True, max_bytes=5000), (b'PDF_TEST', False))
        client.request.assert_awaited_once_with('export_artifact', {
            'owner': 'OWNER', 'sandbox_id': 's000001', 'path': 'a.pdf', 'file_only': True, 'max_bytes': 1024})
        args = client.request.await_args.args[1]
        header = {'schema': Protocol.VERSION, 'id': '1' * 32, 'token': 'T' * 32,
                  'method': 'export_artifact', 'args': args, 'payload_size': 0}
        self.assertEqual(Protocol.validate_request(header, file_limit=1024)[1], args)
        for invalid in ({'file_only': True, 'max_bytes': 0}, {'file_only': False, 'max_bytes': 1},
                        {'file_only': True, 'max_bytes': True}, {'file_only': 'yes', 'max_bytes': 1}):
            with self.assertRaises(Protocol.ProtocolError):
                Protocol.validate_request({**header, 'args': {**args, **invalid}}, file_limit=1024)
        client.request.return_value = ({'directory': True}, b'ZIP')
        with self.assertRaises(ClientModule.SandboxError):
            await client.export_artifact('OWNER', 's000001', 'a.zip', file_only=True, max_bytes=100)

    async def test_rpc_old_wire_has_no_new_fields(self):
        client = ClientModule.SandboxClient('/tmp/TEST_SOCK', 'T' * 32)
        client.request = AsyncMock(return_value=({'directory': True}, b'zip'))
        await client.export_artifact('OWNER', 's000001', 'folder')
        self.assertEqual(set(client.request.await_args.args[1]), {'owner', 'sandbox_id', 'path'})

    async def test_lowest_docker_and_vm_process_boundaries_check_source(self):
        async def lost():
            raise LeaseLost('TEST_source_lost')
        token = fence.active_sandbox_source.set(lost)
        try:
            docker = object.__new__(Docker)
            docker.namespace, docker.docker_host = None, None
            with patch.object(asyncio, 'create_subprocess_exec', new_callable=AsyncMock) as create:
                with self.assertRaises(LeaseLost):
                    await docker._run_process(('docker', 'TEST'), input_bytes=None, timeout=1)
                with self.assertRaises(LeaseLost):
                    await object.__new__(VM)._run('TEST')
                create.assert_not_awaited()
        finally:
            fence.active_sandbox_source.reset(token)

    async def test_rpc_retry_cannot_dispatch_after_source_loss(self):
        valid = True
        async def check():
            if not valid:
                raise LeaseLost('TEST_lost_before_retry')
        async def disconnect(*args, **kwargs):
            nonlocal valid
            valid = False
            raise OSError('TEST_disconnect')
        token = fence.active_sandbox_source.set(check)
        client = ClientModule.SandboxClient('/tmp/TEST_SOCK', 'T' * 32)
        try:
            with patch.object(asyncio, 'open_unix_connection', side_effect=disconnect) as connect:
                with self.assertRaises(LeaseLost):
                    await client.request('health', {})
                self.assertEqual(connect.call_count, 1)
        finally:
            fence.active_sandbox_source.reset(token)

    async def test_rpc_connection_wait_rechecks_before_frame(self):
        valid = True
        async def check():
            if not valid:
                raise LeaseLost('TEST_lost_while_connecting')
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        async def connected(*args, **kwargs):
            nonlocal valid
            valid = False
            return object(), writer
        token = fence.active_sandbox_source.set(check)
        try:
            with patch.object(asyncio, 'open_unix_connection', side_effect=connected), patch.object(ClientModule, 'write_frame', new_callable=AsyncMock) as write:
                with self.assertRaises(LeaseLost):
                    await ClientModule.SandboxClient('/tmp/TEST_SOCK', 'T' * 32).request('health', {})
                write.assert_not_awaited()
                writer.wait_closed.assert_awaited_once()
        finally:
            fence.active_sandbox_source.reset(token)


if __name__ == '__main__':
    unittest.main()
