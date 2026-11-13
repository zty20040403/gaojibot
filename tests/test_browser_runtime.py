"""Real account/route fences, with an owned VM double and no browser secrets."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.browser_runtime import BrowserRuntime,BrowserUnavailable
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from src.plugins.ai_chat.application.commands import CommandServices,dispatch_command,parse_command
from src.plugins.ai_chat.background_tools import workspace_owner
from src.plugins.ai_chat.platform_types import EndpointKey
from tests.qq_official_outbox_fixture import OutboxFixture


class BrowserRuntimeTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls(); self.addAsyncCleanup(self.calls.close)
        self.owner = workspace_owner(self.endpoint)
        self.present,self.running,self.allowed = True,True,True
        self.lock = asyncio.Lock()
        @asynccontextmanager
        async def io_lock(sid):
            self.assertEqual(sid,'s123456')
            async with self.lock:
                yield
        async def listing(owner):
            return [{'sandbox_id':'s123456'}] if owner==self.owner and self.present else []
        async def metadata(owner,sid):
            self.assertEqual((owner,sid),(self.owner,'s123456'))
            return {'owner_ref':owner,'id':sid}
        self.manager = SimpleNamespace(list=AsyncMock(side_effect=listing),io_lock=io_lock,
            _metadata=AsyncMock(side_effect=metadata),_running=AsyncMock(side_effect=lambda _:self.running),
            _guest_exec=AsyncMock(return_value=(0,b'{"domains":["test.example"],"truncated":false}',b'')),
            start_owned=AsyncMock(),ensure_default=AsyncMock())
        sessions = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.browser = BrowserRuntime(SimpleNamespace(store=sessions,manager=self.manager,calls=self.calls))

    def authorize(self, cursor):
        if not self.allowed:
            raise PermissionError('TEST revoked')

    async def snapshot(self, endpoint=None):
        return await self.browser.snapshot(endpoint or self.endpoint,authorize=self.authorize)

    async def test_read_and_command_are_physical_not_history_alias_or_fake_login_success(self):
        with patch('sqlite3.connect',side_effect=AssertionError('Host must not open the guest Cookie file')) as sqlite:
            result = await self.snapshot()
        sqlite.assert_not_called()
        self.assertEqual(result,{'state':'ready','sandbox_id':'s123456','domains':['test.example'],'truncated':False})
        args = self.manager._guest_exec.await_args.args
        self.assertEqual(args[:2],('s123456','/usr/bin/python3'))
        self.assertEqual(args[2][-1],'domains'); self.assertIn('base64',args[2][1])
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?",(self.endpoint.key,))
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=? WHERE endpoint_key=?',(self.endpoint.scope.key,other.key))
        self.assertEqual((await self.snapshot(other))['state'],'missing')
        self.query("UPDATE qq_official_endpoints SET canonical_scope_key=? WHERE endpoint_key=?",(other.scope.key,other.key))
        self.query("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(self.endpoint.key,))
        async def callback(inbound,request):
            return await self.browser.command(inbound,request,authorize=self.authorize)
        inbound = SimpleNamespace(endpoint=self.endpoint)
        services = CommandServices(browser=callback)
        response = await dispatch_command(parse_command('/logins'),inbound,services=services)
        self.assertEqual(response.status,'ok'); self.assertIn('不保证登录仍有效',response.text)
        self.assertIn('test.example',response.text)
        self.assertEqual((await dispatch_command(parse_command('/logins TEST'),inbound,services=services)).status,'syntax')
        self.assertEqual((await dispatch_command(parse_command('/logins'),inbound)).status,'unavailable')

    async def test_missing_or_offline_never_boots_or_exposes_an_empty_login_claim(self):
        self.present = False
        self.assertEqual((await self.snapshot())['state'],'missing')
        self.present,self.running = True,False
        self.assertEqual((await self.snapshot())['state'],'offline')
        self.manager.start_owned.assert_not_awaited(); self.manager.ensure_default.assert_not_awaited()
        self.manager._guest_exec.assert_not_awaited()

    async def test_permission_and_route_revocation_cancel_inflight_read(self):
        for changed in ('permission','route'):
            self.allowed = True
            started,ended = asyncio.Event(),asyncio.Event()
            async def guest(*args,**kwargs):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    ended.set()
            self.manager._guest_exec.side_effect = guest
            task = asyncio.create_task(self.snapshot())
            await asyncio.wait_for(started.wait(),3)
            with self.assertRaisesRegex(BrowserUnavailable,'busy'):
                await self.snapshot()
            if changed=='permission':
                self.allowed = False
            else:
                self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
            with self.assertRaises(PermissionError):
                await asyncio.wait_for(task,3)
            self.assertTrue(ended.is_set()); self.assertFalse(self.browser.active)

    async def test_recheck_after_guest_result_and_strict_protocol_reject_private_values(self):
        async def revoke(*args,**kwargs):
            self.allowed = False
            return (0,b'{"domains":["test.example"],"truncated":false}',b'')
        self.manager._guest_exec.side_effect = revoke
        with self.assertRaises(PermissionError):
            await self.snapshot()
        self.allowed = True; self.manager._guest_exec.side_effect = None
        for result in ({'domains':['test.example'],'truncated':False,'cookies':'TEST_SECRET'},
                {'domains':['https://test.example/?TEST_SECRET'],'truncated':False},
                {'domains':['test.example']*201,'truncated':False},{'domains':[],'truncated':'TEST_SECRET'}):
            self.manager._guest_exec.return_value = (0,json.dumps(result).encode(),b'TEST_SECRET')
            with self.assertRaises(BrowserUnavailable) as raised:
                await self.snapshot()
            self.assertNotIn('TEST_SECRET',str(raised.exception))
        self.assertFalse(self.browser.active)

    async def test_cancel_while_waiting_for_vm_lock_has_no_guest_effect(self):
        async with self.lock:
            task = asyncio.create_task(self.snapshot())
            await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.manager._guest_exec.assert_not_awaited()
        self.assertFalse(self.browser.active)
