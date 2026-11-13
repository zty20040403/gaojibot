"""Real lease/database and guest-like files; only the QGA hop is a double."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.adapters.qq_official.vm_credentials import VMCredentials
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from src.plugins.ai_chat.background_tools import workspace_owner
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.sandbox_identity import run
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
from tests.qq_official_outbox_fixture import OutboxFixture

BOOT = '00000000-0000-4000-8000-000000000001'


class VMCredentialTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guest,self.boot = self.root/'run'/'gaoji',self.root/'TEST_BOOT_ID'
        self.boot.write_text(BOOT)
        self.fail_install = False
        self.cancel_install = False
        lock = asyncio.Lock()
        @asynccontextmanager
        async def io_lock(sandbox_id):
            async with lock:
                yield
        host = VmSandboxManager(image='/TEST_IMAGE',root=str(self.root/'host'))
        host._directory('s123456').mkdir(parents=True)
        host._save_metadata('s123456',dict(id='s123456',owner_ref=workspace_owner(self.endpoint),
            owner_hash=host._owner_hash(workspace_owner(self.endpoint)),created_at=self.now,last_used=self.now))
        host.program_state.clock = lambda:self.now
        async def metadata(owner,sandbox_id):
            if owner!=workspace_owner(self.endpoint) or sandbox_id!='s123456':
                raise PermissionError('vm_owner_mismatch')
            return await host._metadata(owner,sandbox_id)
        async def guest(sandbox_id,path,args,*,input_bytes,timeout):
            if path=='/usr/bin/timeout':
                return 0,b'{"ok":true,"units":[],"errors":{}}',b''
            self.assertEqual(path,'/usr/bin/python3')
            self.assertIn("if __name__=='__main__'",args[1])
            payload = json.loads(input_bytes)
            if payload['op']=='install' and self.cancel_install:
                raise asyncio.CancelledError
            result = run(payload,directory=self.guest,boot_file=self.boot)
            if payload['op']=='install' and self.fail_install:
                return 1,payload['token'].encode(),b'TEST_GUEST_ERROR'
            return 0,json.dumps(result).encode(),b''
        self.manager = SimpleNamespace(_metadata=AsyncMock(side_effect=metadata),_running=AsyncMock(return_value=True),
            _guest_exec=AsyncMock(side_effect=guest),io_lock=io_lock,
            _all_metadata=host._all_metadata,_directory=host._directory,program_state=host.program_state)
        async def calls(function,*args,**kwargs):
            return await asyncio.to_thread(function,*args,**kwargs)
        self.calls = calls
        self.store = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.broker = VMCredentials(self.store,self.manager,calls,port=18640)

    async def prepare(self):
        return await self.broker.prepare(self.endpoint,'s123456',expected_route=(1,1))

    def token_file(self):
        return (self.guest/'token').read_text()

    async def test_first_boot_and_repeated_prepare_keep_one_token(self):
        first = await self.prepare()
        token = self.token_file()
        self.assertEqual(await self.prepare(),first)
        self.assertEqual(self.token_file(),token)
        self.assertEqual(await self.broker.authenticate(token),first)
        self.assertNotIn(token,repr(first))
        self.assertNotIn(first.token_hash,repr(first))
        self.assertEqual(sum(json.loads(call.kwargs['input_bytes'])['op']=='install'
            for call in self.manager._guest_exec.await_args_list),1)

    async def test_boot_changed_before_sweep_rejects_old_token_then_rotates(self):
        first = await self.prepare()
        old = self.token_file()
        self.boot.write_text('00000000-0000-4000-8000-000000000002')
        shutil.rmtree(self.guest)
        with self.assertRaisesRegex(PermissionError,'vm_boot_replaced'):
            await self.broker.authenticate(old)
        new = await self.prepare()
        self.assertEqual(new.token_generation,first.token_generation+1)
        with self.assertRaises(PermissionError):
            await self.broker.authenticate(old)
        self.assertEqual(await self.broker.authenticate(self.token_file()),new)

    async def test_host_restart_revalidates_same_boot_without_reading_plaintext_token(self):
        first = await self.prepare()
        old = self.token_file()
        self.account_lease.close()
        self.store = VMSessions(self.tx,self.lease().acquire(),private_admission=lambda *_:True,clock=lambda:self.now)
        self.broker = VMCredentials(self.store,self.manager,self.calls,port=18640)
        current = await self.prepare()
        self.assertEqual(current.token_hash,first.token_hash)
        self.assertGreater(current.account_epoch,first.account_epoch)
        self.assertEqual(await self.broker.authenticate(old),current)
        with self.assertRaises(PermissionError):
            await self.broker.check(first)

    async def test_wrong_vm_owner_and_stopped_vm_fail_closed(self):
        current = await self.prepare()
        other = EndpointKey(self.account,'group','TEST_OTHER')
        self.provision(other)
        with self.assertRaisesRegex(PermissionError,'vm_owner_mismatch'):
            await self.broker.prepare(other,'s123456',expected_route=(1,1))
        self.manager._running.return_value = False
        with self.assertRaisesRegex(PermissionError,'vm_not_running'):
            await self.broker.authenticate(self.token_file())
        self.manager._running.return_value = True
        self.assertEqual(await self.broker.authenticate(self.token_file()),current)

    async def test_uncertain_install_is_revoked_without_echoing_guest_output(self):
        self.fail_install = True
        with self.assertRaisesRegex(PermissionError,'^vm_identity_unavailable$') as error:
            await self.prepare()
        self.assertNotIn(self.token_file(),str(error.exception))
        with self.assertRaises(PermissionError):
            self.store.authenticate(self.token_file())
        self.assertIsNotNone(self.query('SELECT revoked_at FROM qq_official_vm_sessions')[0]['revoked_at'])

    async def test_cancelled_install_is_revoked(self):
        self.cancel_install = True
        with self.assertRaises(asyncio.CancelledError):
            await self.prepare()
        self.assertIsNotNone(self.query('SELECT revoked_at FROM qq_official_vm_sessions')[0]['revoked_at'])

    async def test_stale_cleanup_and_deleted_row_never_revalidate_an_old_source(self):
        old = await self.prepare()
        self.query('DELETE FROM qq_official_vm_sessions')
        new = await self.prepare()
        self.assertEqual(old.token_generation,new.token_generation)
        self.assertNotEqual(old.token_hash,new.token_hash)
        with self.assertRaises(PermissionError):
            await self.broker.check(old)
        await self.calls(self.store.discard,old)
        self.assertEqual(await self.broker.authenticate(self.token_file()),new)
