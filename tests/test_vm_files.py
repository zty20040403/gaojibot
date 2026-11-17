"""VM-only file HTTP + bounded local guest files + persistent real file outbox."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_vm_program_http as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.qq_official_file_http_fixture import FileMediaFake
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServices,FileServiceConfig
from src.sandbox_runtime.artifact_export import EXPORT_FILE_SCRIPT, STAGE_FILE_SCRIPT, bounded_regular_file, stage_regular_file
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


class VMFileHTTPTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    ensure_current = fixture.VMProgramHTTPTests.ensure_current
    make_sender = fixture.VMProgramHTTPTests.make_sender
    fast_sleep = fixture.VMProgramHTTPTests.fast_sleep

    async def asyncSetUp(self):
        fake = FileMediaFake(); fake.clock = lambda:self.now
        with patch('tests.test_vm_program_http.FakePlatform',return_value=fake):
            await fixture.VMProgramHTTPTests.asyncSetUp(self)
        self.runtime = self.service.runtime
        self.runtime.deliveries,self.runtime.outbox = self.deliveries,self.outbox
        self.runtime._token,self.runtime._stopping = self.token,False
        self.files = self.runtime.file_service = FileServices(self.runtime,self.token,self.rest,
            FileServiceConfig(self.root/'private-files',self.manager,generation_enabled=True))
        self.addAsyncCleanup(self.files.close)
        self.work = self.root/'work'; self.work.mkdir()
        self.manager.max_file_bytes = 65536
        async def export(owner,sid,path,*,file_only,max_bytes):
            self.assertEqual((owner,sid,file_only),(self.owner,'s123456',True))
            return await asyncio.to_thread(bounded_regular_file,self.work,path,max_bytes),False
        self.manager.export_artifact = AsyncMock(side_effect=export)
        (self.work/'report.txt').write_bytes(b'TEST_ORIGINAL')

    async def post(self, **changes):
        return await self.client.post(self.base+'/vm/send-file',json=dict(path='/work/report.txt',request_id='TEST_FILE')|changes)

    async def test_file_is_sent_once_and_idempotent_across_retries(self):
        response = await self.post()
        self.assertEqual((response.status_code,response.json()['state']),(200,'completed'),response.text)
        action_id = response.json()['action_id']
        self.assertNotIn('token_hash',response.text); self.assertNotIn('payload_json',response.text)
        (self.work/'report.txt').write_bytes(b'TEST_CHANGED_AFTER_CAPTURE')
        self.assertEqual((await self.post()).json(),response.json())
        self.manager.export_artifact.assert_awaited_once()
        state = await self.client.get(self.base+'/vm/actions/'+action_id)
        self.assertEqual(state.json()['state'],'completed')
        self.assertEqual(self.fake.uploaded[('groups',self.endpoint.native_conversation_id)],b'TEST_ORIGINAL')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual((await self.post(path='different.txt')).status_code,409)

    async def test_stdlib_cli_client_uses_real_vm_routes(self):
        from src.plugins.ai_chat import sandbox_gaoji as cli
        client = cli.Client(self.guest)
        original = cli.http.client.HTTPConnection
        def connect(host,port,**kwargs):
            self.assertEqual((host,port),('10.0.2.2',self.service.gateway.port))
            return original('127.0.0.1',port,**kwargs)
        with patch.object(cli.http.client,'HTTPConnection',side_effect=connect):
            status,reply = await asyncio.to_thread(client.request,'POST','/vm/say',
                {'text':'TEST_CLI','request_id':'TEST_CLI_SAY'})
            self.assertEqual((status,reply['state']),(200,'completed'))
            status,reply = await asyncio.to_thread(client.request,'POST','/vm/send-file',
                {'path':'/work/report.txt','request_id':'TEST_CLI_FILE'})
            self.assertEqual((status,reply['state']),(200,'completed'))
            status,stored = await asyncio.to_thread(client.request,'GET','/vm/actions?request_id=TEST_CLI_FILE')
            self.assertEqual((status,stored['action_id']),(200,reply['action_id']))

    async def test_only_bounded_regular_files_from_this_workspace_are_accepted(self):
        outside = self.root/'TEST_PRIVATE'; outside.write_bytes(b'TEST_OUTSIDE')
        (self.work/'link').symlink_to(outside)
        (self.work/'dirlink').symlink_to(self.root,target_is_directory=True)
        os.mkfifo(self.work/'fifo')
        (self.work/'large').write_bytes(b'X'*65537)
        for index,(path,status) in enumerate((('/etc/passwd',400),('../TEST_PRIVATE',400),('/work/../TEST_PRIVATE',400),
                ('link',502),('dirlink/TEST_PRIVATE',502),('fifo',502),('large',502),('missing',502))):
            with self.subTest(path=path):
                response = await self.post(path=path,request_id='TEST_'+str(status)+'_'+str(index))
                self.assertEqual(response.status_code,status,response.text)
                self.assertNotIn('TEST_OUTSIDE',response.text)
        self.assertEqual((await self.post(endpoint_key='TEST_OTHER')).status_code,400)
        self.assertEqual((await self.post(filename='../bad')).status_code,400)
        token,_ = self.service.gateway.open(source={'job_id':1},current=AsyncMock())
        response = await self.client.post(self.base+'/vm/send-file',headers={'Authorization':'Bearer '+token},json={})
        self.assertEqual(response.status_code,401)
        self.assertEqual(self.fake.received,[])

    async def test_stop_cancels_export_and_start_does_not_replay_the_reserved_path(self):
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def export(*args,**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.manager.export_artifact.side_effect = export
        pending = asyncio.create_task(self.post())
        try:
            await asyncio.wait_for(entered.wait(),3)
            self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
            response = await asyncio.wait_for(pending,3)
            self.assertEqual(response.status_code,403,response.text)
            await asyncio.wait_for(cancelled.wait(),3)
            self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])
            self.assertEqual(self.lifecycle.http.active_exports,{})
        finally:
            pending.cancel(); await asyncio.gather(pending,return_exceptions=True)

    async def test_export_limit_and_stop_cancel_real_work_without_more_exports(self):
        entered,cancelled = asyncio.Event(),[]
        calls = 0
        async def export(*args,**kwargs):
            nonlocal calls
            calls += 1
            if calls==2:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)
        self.manager.export_artifact.side_effect = export
        pending = [asyncio.create_task(self.post(request_id='TEST_'+str(i))) for i in range(2)]
        try:
            await asyncio.wait_for(entered.wait(),3)
            excess = await self.post(request_id='TEST_EXCESS')
            self.assertEqual((excess.status_code,excess.json()['error']),(429,'vm_file_exports_busy'))
            self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
            responses = await asyncio.wait_for(asyncio.gather(*pending),3)
            self.assertEqual([item.status_code for item in responses],[403,403])
            self.assertEqual(len(cancelled),2)
            self.assertEqual(self.lifecycle.http.active_exports,{})
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)

    async def test_slow_body_cannot_borrow_the_generation_after_stop_start(self):
        authenticated,release = asyncio.Event(),asyncio.Event()
        original = self.lifecycle.api.authorize
        async def authorize(*args,**kwargs):
            result = await original(*args,**kwargs)
            authenticated.set()
            return result
        async def body():
            yield b'{"request_id":"TEST_OLD",'
            await release.wait()
            yield b'"path":"/work/report.txt"}'
        with patch.object(self.lifecycle.api,'authorize',side_effect=authorize):
            pending = asyncio.create_task(self.client.post(self.base+'/vm/send-file',content=body()))
            try:
                await asyncio.wait_for(authenticated.wait(),3)
                self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,FALSE,1)',(self.endpoint.scope.key,))
                release.set()
                response = await asyncio.wait_for(pending,3)
                self.assertEqual((response.status_code,response.json()['error']),(403,'conversation_stopped_or_replaced'))
            finally:
                release.set(); pending.cancel(); await asyncio.gather(pending,return_exceptions=True)
        self.manager.export_artifact.assert_not_awaited()
        self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])

    async def test_timeout_fails_cleanly_and_clears_export_slot(self):
        async def slow(*args,**kwargs):
            await asyncio.Future()
        self.manager.export_artifact.side_effect = slow
        with patch('src.plugins.ai_chat.adapters.qq_official.vm_http.EXPORT_SECONDS',0.05):
            self.assertEqual((await self.post()).status_code,502)
        self.manager.export_artifact.assert_awaited_once()
        self.assertEqual(self.lifecycle.http.active_exports,{})


class VMLargeExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_30mib_export_uses_1mib_chunks_and_cleans_staged_file(self):
        import base64
        import hashlib
        import json
        import tempfile
        from pathlib import Path
        from src.plugins.ai_chat.sandbox import SANDBOX_ID_PATTERN

        self.assertRegex('s123456', SANDBOX_ID_PATTERN)
        payload = (b'GAOJI30M' * (4 * 1024 * 1024))[:30 * 1024 * 1024]
        expected_sha = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            work = root_path / 'workspace'
            guest_root = root_path / 'guest_root'
            work.mkdir(); guest_root.mkdir()
            (work / 'big.mp4').write_bytes(payload)
            vms_root = root_path / 'vms'
            vms_root.mkdir(mode=0o700)
            manager = VmSandboxManager(image='/TEST_base.qcow2', root=str(vms_root), max_file_bytes=64 * 1024 * 1024)
            vm_dir = manager._directory('s123456')
            vm_dir.mkdir(parents=True)
            (vm_dir / 'metadata.json').write_text(json.dumps({
                'id': 's123456',
                'owner_hash': manager._owner_hash('qqo:conversation:TEST'),
                'owner_ref': 'qqo:conversation:TEST',
                'runtime': 'debian',
                'purpose': 'task',
            }), encoding='utf-8')
            manager._running = AsyncMock(return_value=True)
            manager._wait_guest = AsyncMock()
            handles = {}
            next_handle = 1
            max_response_bytes = 0

            def map_staged(guest_path: str) -> Path:
                if guest_path.startswith('/root/'):
                    return guest_root / guest_path.removeprefix('/root/')
                raise AssertionError(f'unexpected guest path: {guest_path}')

            async def fake_guest_exec_raw(sandbox_id, path, args, *, timeout=60, input_bytes=None):
                if path == '/usr/bin/python3' and args[:2] == ['-I', '-c']:
                    staged = map_staged(args[5])
                    size = stage_regular_file(str(work), args[3], int(args[4]), str(staged))
                    return 0, f'{size}\n'.encode(), b''
                if path == '/usr/bin/stat' and args[:1] == ['-c']:
                    target = map_staged(args[2])
                    return 0, f'regular file:{target.stat().st_size}\n'.encode(), b''
                if path == '/bin/rm' and args[:1] == ['-f']:
                    map_staged(args[1]).unlink(missing_ok=True)
                    return 0, b'', b''
                raise AssertionError(f'unexpected guest exec: {path} {args}')

            async def fake_virsh(*args, timeout=30):
                nonlocal next_handle, max_response_bytes
                assert args[0] == 'qemu-agent-command'
                req = json.loads(args[2])
                op = req['execute']
                arguments = req.get('arguments', {})
                if op == 'guest-file-open':
                    target = map_staged(arguments['path'])
                    f = open(target, 'rb')
                    hid = next_handle; next_handle += 1
                    handles[hid] = f
                    resp = {'return': hid}
                elif op == 'guest-file-read':
                    data = handles[arguments['handle']].read(arguments['count'])
                    resp = {'return': {'count': len(data), 'buf-b64': base64.b64encode(data).decode('ascii'), 'eof': not data}}
                elif op == 'guest-file-close':
                    handles.pop(arguments['handle']).close()
                    resp = {'return': {}}
                else:
                    raise AssertionError(f'unexpected QGA op: {op}')
                encoded = json.dumps(resp)
                max_response_bytes = max(max_response_bytes, len(encoded.encode('utf-8')))
                return encoded

            manager._guest_exec_raw = AsyncMock(side_effect=fake_guest_exec_raw)
            manager._virsh = AsyncMock(side_effect=fake_virsh)

            exported, is_dir = await manager.export_artifact(
                'qqo:conversation:TEST', 's123456', 'big.mp4',
                file_only=True, max_bytes=64 * 1024 * 1024,
            )
            self.assertFalse(is_dir)
            self.assertEqual(hashlib.sha256(exported).hexdigest(), expected_sha)
            self.assertLess(max_response_bytes, 2 * 1024 * 1024)
            self.assertEqual(list(guest_root.iterdir()), [])

    def test_staging_rejects_the_same_unsafe_sources_and_preserves_existing_output(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            work = base / 'work'; work.mkdir()
            outside = base / 'outside'; outside.write_bytes(b'TEST_OUTSIDE')
            (work / 'good').write_bytes(b'TEST_CONTENT')
            (work / 'empty').touch()
            (work / 'link').symlink_to(outside)
            (work / 'dirlink').symlink_to(base, target_is_directory=True)
            staged = base / 'staged'
            for path, limit in (('../outside', 1024), ('/outside', 1024), ('link', 1024),
                                ('dirlink/outside', 1024), ('empty', 1024), ('good', 0), ('good', 3)):
                with self.subTest(path=path, limit=limit):
                    with self.assertRaises((ValueError, OSError)):
                        bounded_regular_file(str(work), path, limit)
                    with self.assertRaises((ValueError, OSError)):
                        stage_regular_file(str(work), path, limit, str(staged))
                    self.assertFalse(staged.exists())
            staged.write_bytes(b'TEST_EXISTING')
            with self.assertRaises(FileExistsError):
                stage_regular_file(str(work), 'good', 1024, str(staged))
            self.assertEqual(staged.read_bytes(), b'TEST_EXISTING')
            self.assertEqual(outside.read_bytes(), b'TEST_OUTSIDE')

    def test_stage_and_export_scripts_standalone_and_clean_on_change(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            work = root_path / 'work'
            work.mkdir()
            (work / 'hello.txt').write_bytes(b'TEST_PAYLOAD')
            # Verify EXPORT_FILE_SCRIPT still compiles and runs standalone (docker.py relies on it).
            proc = subprocess.run(
                [sys.executable, '-I', '-c', EXPORT_FILE_SCRIPT.replace("'/workspace'", repr(str(work))), 'hello.txt', '1024'],
                capture_output=True, check=False,
            )
            self.assertEqual((proc.returncode, proc.stdout), (0, b'TEST_PAYLOAD'))

            staged_ok = root_path / 'staged_ok'
            proc2 = subprocess.run(
                [sys.executable, '-I', '-c', STAGE_FILE_SCRIPT.replace("'/workspace'", repr(str(work))), 'hello.txt', '1024', str(staged_ok)],
                capture_output=True, check=False,
            )
            self.assertEqual((proc2.returncode, proc2.stdout.strip()), (0, b'12'))
            self.assertEqual(staged_ok.read_bytes(), b'TEST_PAYLOAD')

            staged_changed = root_path / 'staged_changed'
            real_read = os.read
            def mutating_read(fd, n):
                chunk = real_read(fd, n)
                if chunk:
                    (work / 'hello.txt').write_bytes(b'TEST_MUTATED_BYTES!')
                return chunk
            with patch('os.read', side_effect=mutating_read):
                with self.assertRaisesRegex(ValueError, 'export_file_changed'):
                    stage_regular_file(str(work), 'hello.txt', 1024, str(staged_changed))
            self.assertFalse(staged_changed.exists())
