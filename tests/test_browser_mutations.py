"""Real route fences and guest journals, with no real browser or VM operations."""
import ast
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from tests import test_browser_runtime as base
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.browser_mutations import mutate,BrowserActionUnknown
from src.plugins.ai_chat import sandbox_browser_actions as actions
from src.plugins.ai_chat.application.commands import CommandServices,dispatch_command,parse_command

BOOT = '11111111-1111-4111-8111-111111111111'


def decode(script):
    tree = ast.parse(script)
    node = next(node for node in tree.body if isinstance(node,ast.Assign) and node.targets[0].id=='request')
    return json.loads(base64.b64decode(node.value.args[0].args[0].value))


class BrowserMutationTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    authorize = base.BrowserRuntimeTests.authorize

    async def asyncSetUp(self):
        await base.BrowserRuntimeTests.asyncSetUp(self)
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.journal = Path(directory.name)
        self.perform = AsyncMock(side_effect=lambda value:{'cleared':True,**({'domain':value['domain']} if value['action']=='logout' else {})})
        self.requests = []
        self.manager._guest_exec.side_effect = self.guest

    async def guest(self,sid,path,args,**kwargs):
        if '-S' in args:
            return (0,BOOT.encode()+b'\n',b'')
        self.assertEqual(args[:2],['-I','-c'])
        request = decode(args[2]); self.requests.append(request)
        result = await actions.execute(request,self.perform,journal=self.journal,boot=BOOT,clock=lambda:self.now)
        return (0,json.dumps(dict(id=request['id'],boot=BOOT,result=result)).encode(),b'')

    async def mutate(self,**kwargs):
        return await mutate(self.browser,self.endpoint,authorize=self.authorize,
            **{**dict(key='TEST_NATIVE_EXECUTION',action='logout',domain='test.example'),**kwargs})

    async def command(self,text,*,key='TEST_UI_COMMAND'):
        async def callback(inbound,request):
            return await self.browser.command(inbound,request,authorize=self.authorize,key=key)
        return await dispatch_command(parse_command(text),SimpleNamespace(endpoint=self.endpoint),services=CommandServices(browser=callback))

    async def test_chat_logout_dispatches_only_explicit_domains_and_reports_actual_result(self):
        for text in ('/logout','/logout https://test.example','/logout test.example TEST_OTHER'):
            self.assertEqual((await self.command(text)).status,'syntax')
        self.assertEqual((await self.command('/logout test.example',key=None)).status,'denied')
        self.perform.assert_not_awaited()
        result = await self.command('/logout Test.Example')
        self.assertEqual(result.status,'ok'); self.assertIn('test.example',result.text)
        self.assertIn('其他网站、设备和服务器上的登录不会因此撤销',result.text)
        self.assertEqual(self.perform.await_args.args[0]['domain'],'test.example')

    async def test_chat_unknown_is_not_misreported_as_unsupported_or_success(self):
        self.perform.side_effect = RuntimeError('TEST_SECRET_COOKIE')
        result = await self.command('/logout test.example')
        self.assertEqual(result.status,'unavailable')
        self.assertIn('清理结果不明',result.text); self.assertNotIn('TEST_SECRET',result.text)
        self.perform.assert_awaited_once()

    async def test_chat_offline_and_pre_dispatch_denial_are_explicit(self):
        self.running = False
        result = await self.command('/logout test.example')
        self.assertIn('已离线',result.text)
        self.running,self.allowed = True,False
        self.assertEqual((await self.command('/logout test.example')).status,'denied')
        self.perform.assert_not_awaited(); self.manager._guest_exec.assert_not_awaited()

    async def test_fixed_payload_and_journal_do_not_repeat_after_a_new_login(self):
        self.assertEqual(await self.mutate(),{'cleared':True,'domain':'test.example'})
        self.assertEqual(self.requests[0]['expires'],self.now+60)
        self.assertEqual(self.requests[0]['boot'],BOOT)
        self.assertEqual(await self.mutate(),{'cleared':True,'domain':'test.example'})
        self.perform.assert_awaited_once()
        self.now += 1
        self.assertEqual(await self.mutate(),{'error':'browser_request_conflict'})
        self.perform.assert_awaited_once()
        self.manager.start_owned.assert_not_awaited()
        self.assertFalse(self.browser.active)

    async def test_clear_has_a_distinct_intent_and_no_guest_supplied_script(self):
        self.assertEqual(await mutate(self.browser,self.endpoint,authorize=self.authorize,key='TEST_ADMIN_CLEAR',action='clear'),{'cleared':True})
        self.assertEqual(self.requests[0]['domain'],'')
        self.assertEqual(self.manager._guest_exec.await_args.args[1],'/usr/bin/python3')
        self.assertNotIn('TEST_ADMIN_CLEAR',self.manager._guest_exec.await_args.args[2][2])

    async def test_offline_missing_or_revoked_before_dispatch_never_clears(self):
        self.present = False
        self.assertEqual(await self.mutate(),{'error':'browser_vm_missing'})
        self.present,self.running = True,False
        self.assertEqual(await self.mutate(),{'error':'browser_vm_offline'})
        self.manager._guest_exec.assert_not_awaited()
        self.running = True
        async def revoked(*args,**kwargs):
            self.allowed = False
            return (0,BOOT.encode(),b'')
        self.manager._guest_exec.side_effect = revoked
        with self.assertRaises(PermissionError):
            await self.mutate()
        self.manager._guest_exec.assert_awaited_once()
        self.perform.assert_not_awaited()

    async def test_route_change_cancels_waiting_not_claiming_guest_did_not_run(self):
        started,ended = asyncio.Event(),asyncio.Event()
        async def pending(value):
            started.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()
        self.perform.side_effect = pending
        task = asyncio.create_task(self.mutate())
        await asyncio.wait_for(started.wait(),3)
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
        with self.assertRaises(BrowserActionUnknown):
            await asyncio.wait_for(task,3)
        self.assertTrue(ended.is_set()); self.assertFalse(self.browser.active)
        self.assertEqual(self.perform.await_count,1)
        self.assertEqual(json.loads(next(self.journal.glob('*.json')).read_text())['state'],'started')

    async def test_revocation_after_success_remains_unknown_not_false_success(self):
        async def revoke(value):
            self.allowed = False
            return {'cleared':True,'domain':value['domain']}
        self.perform.side_effect = revoke
        with self.assertRaises(BrowserActionUnknown):
            await self.mutate()
        self.perform.assert_awaited_once()
        self.assertFalse(self.browser.active)

    async def test_protocol_never_returns_cookie_values_or_accepts_another_boot_receipt(self):
        for alteration in ('secret','boot','id','cleared'):
            async def wrong(*args,**kwargs):
                code,content,error = await self.guest(*args,**kwargs)
                if '-S' in args[2]:
                    return code,content,error
                result = json.loads(content)
                if alteration=='secret':
                    result['result']['cookie'] = 'TEST_SECRET'
                elif alteration=='cleared':
                    result['result']['cleared'] = 1
                else:
                    result[alteration] = 'TEST_SECRET'
                return (code,json.dumps(result).encode(),b'TEST_SECRET')
            self.manager._guest_exec.side_effect = wrong
            with self.assertRaises(BrowserActionUnknown) as raised:
                await self.mutate()
            self.assertNotIn('TEST_SECRET',str(raised.exception))
        self.perform.assert_awaited_once()

    async def test_parent_domain_result_is_validated_without_broadening_the_request(self):
        self.perform.side_effect = lambda value:{'error':'browser_logout_parent_domain_required','domains':['test.example']}
        result = await mutate(self.browser,self.endpoint,authorize=self.authorize,key='TEST_SUBDOMAIN',action='logout',domain='sub.test.example')
        self.assertEqual(result,{'error':'browser_logout_parent_domain_required','domains':['test.example']})
        self.perform.assert_awaited_once()
        self.perform.side_effect = lambda value:{'error':'browser_logout_parent_domain_required','domains':['TEST_SECRET']}
        with self.assertRaises(BrowserActionUnknown):
            await mutate(self.browser,self.endpoint,authorize=self.authorize,key='TEST_BAD_DOMAIN',action='logout',domain='sub.test.example')
