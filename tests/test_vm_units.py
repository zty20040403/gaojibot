"""Real VM-authenticated HTTP and DB policy; fake guest systemd, real files."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from tests import test_vm_lifecycle as lifecycle_fixture, test_sandbox_units as unit_fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class VMUnitHTTPTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await lifecycle_fixture.VMLifecycleTests.asyncSetUp(self)
        self.units = unit_fixture.SandboxUnitsTests()
        self.units.setUp(); self.addCleanup(self.units.doCleanups)
        (self.units.registry/'TEST.service').write_text(unit_fixture.SERVICE)
        original = self.manager._guest_exec.side_effect
        self.release,self.entered = None,asyncio.Event()
        self.policies = []
        async def guest(sid,path,args,*,input_bytes,timeout):
            if path=='/usr/bin/python3':
                return await original(sid,path,args,input_bytes=input_bytes,timeout=timeout)
            self.assertEqual(path,'/usr/bin/timeout')
            self.assertEqual(args[:5],['--signal=TERM','--kill-after=5s','90','/usr/bin/python3','-c'])
            self.assertIn('class Units:',args[5])
            self.assertEqual(args[6: ],['--policy-stdin'])
            self.assertEqual(sid,'s123456')
            policy = json.loads(input_bytes)
            self.policies.append(policy); self.entered.set()
            if self.release is not None:
                await self.release.wait()
            report = self.units.manager.sync(**policy)
            return int(not report['ok']),json.dumps(report).encode(),b''
        self.manager._guest_exec.side_effect = guest
        self.url = f'http://127.0.0.1:{self.service.gateway.port}/vm/units'

    async def post(self, **data):
        return await self.client.post(self.url,json=data)

    async def test_sandbox_user_sync_uses_host_policy_and_does_not_require_sender_or_active_chat(self):
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
        # No nested manager.io_lock: an ordinary sandbox exec may hold it
        # while its unprivileged gaoji-units client waits for this response.
        async with self.manager.io_lock('s123456'):
            response = await asyncio.wait_for(self.post(),3)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.policies,[{'enabled':True,'disabled':[]}])
        self.assertIn(['systemctl','start','TEST.service'],self.units.calls)
        self.assertEqual(self.query('SELECT * FROM qq_official_program_actions'),[])
        self.assertEqual(self.service.gateway.channels,{})

    async def test_disabled_program_and_conversation_switch_stop_units_without_guest_override(self):
        self.assertEqual((await self.post()).status_code,200)
        self.query('INSERT INTO program_controls VALUES (?,?,FALSE,?)',(self.endpoint.scope.key,'TEST',self.now))
        response = await self.post()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['units'][0]['enabled'],False)
        self.assertIn(['systemctl','stop','TEST.service'],self.units.calls)
        self.assertEqual((await self.post(enabled=True)).status_code,400)
        self.query('UPDATE program_controls SET enabled=TRUE')
        self.query('INSERT INTO conversation_settings(scope_key,programs_enabled) VALUES (?,FALSE)',(self.endpoint.scope.key,))
        self.assertFalse((await self.post()).json()['units'][0]['enabled'])

    async def test_task_tokens_and_caller_supplied_paths_or_policies_are_rejected(self):
        token,_ = self.service.gateway.open(source={'job_id':1},current=AsyncMock())
        response = await self.client.post(self.url,json={},headers={'Authorization':'Bearer '+token})
        self.assertEqual(response.status_code,401)
        for payload in ({'scope_key':'TEST_OTHER'},{'path':'/etc/systemd/system'},{'command':'TEST_SHELL'},{'disabled':[]}):
            self.assertEqual((await self.post(**payload)).status_code,400)
        self.assertEqual((await self.client.post(self.url+'?scope=TEST_OTHER',json={})).status_code,400)
        self.assertEqual(self.policies,[])

    async def test_malformed_guest_reply_is_a_gateway_error_not_client_success(self):
        original = self.manager._guest_exec.side_effect
        async def guest(sid,path,args,**kwargs):
            if path=='/usr/bin/timeout':
                return 0,b'TEST_INVALID_JSON',b''
            return await original(sid,path,args,**kwargs)
        self.manager._guest_exec.side_effect = guest
        response = await self.post()
        self.assertEqual((response.status_code,response.json()['error']),(502,'program_unit_result_unavailable'))
        self.assertEqual(self.lifecycle.units.active,set())

    async def test_guest_report_shape_cannot_poison_future_host_status(self):
        original = self.manager._guest_exec.side_effect
        current_report = {}
        async def guest(sid,path,args,**kwargs):
            if path=='/usr/bin/timeout':
                return 0,json.dumps(current_report).encode(),b''
            return await original(sid,path,args,**kwargs)
        self.manager._guest_exec.side_effect = guest
        for units in (['TEST_INVALID_ROW'],[{'name':'TEST.service','enabled':'yes'}],
                [{'name':'TEST.service','enabled':True,'ActiveState':'x'*10000}],
                [{'name':'TEST.service','enabled':True,'command':'TEST_INJECTED'}]):
            current_report = dict(ok=True,units=units,errors={})
            response = await self.post()
            self.assertEqual(response.status_code,502,response.text)
            state = (await self.manager._metadata(self.owner,'s123456'))['programs']
            self.assertEqual(state['snapshot']['errors'],{'_sync':'program_sync_unavailable'})
            self.assertTrue(state['retained'])

    async def test_duplicate_sync_is_bounded_and_policy_change_cannot_report_stale_success(self):
        self.release = asyncio.Event()
        pending = asyncio.create_task(self.post())
        try:
            await asyncio.wait_for(self.entered.wait(),3)
            duplicate = await self.post()
            self.assertEqual((duplicate.status_code,duplicate.json()['error']),(429,'program_units_busy'))
            self.query('INSERT INTO program_controls VALUES (?,?,FALSE,?)',(self.endpoint.scope.key,'TEST',self.now))
            self.release.set()
            result = await asyncio.wait_for(pending,3)
            self.assertEqual((result.status_code,result.json()['error']),(409,'program_unit_policy_changed'))
            self.assertEqual(self.lifecycle.units.active,set())
            self.assertFalse((await self.post()).json()['units'][0]['enabled'])
        finally:
            self.release.set(); pending.cancel(); await asyncio.gather(pending,return_exceptions=True)

    async def test_ready_restores_units_and_two_unplanned_boots_suspend_loading(self):
        self.assertEqual((await self.post()).status_code,200)
        self.units.calls.clear()
        for digit in ('2','3'):
            self.boot.write_text('00000000-0000-4000-8000-00000000000'+digit)
            await self.lifecycle.ready(self.owner,'s123456')
        self.assertEqual(self.policies[-2]['enabled'],True)
        self.assertEqual(self.policies[-1]['enabled'],False)
        state = (await self.manager._metadata(self.owner,'s123456'))['programs']
        self.assertTrue(state['suspended'])
        self.assertTrue(state['alert'])
        self.assertEqual(self.units.states['TEST.service'],'inactive')
        self.client.headers['Authorization'] = 'Bearer '+(self.guest/'token').read_text()
        result = await self.post()
        self.assertEqual(result.status_code,409,result.text)
        self.assertEqual(result.json()['errors']['_host'],'vm_programs_suspended')
        # Guest sync cannot clear the crash circuit.
        self.assertTrue((await self.manager._metadata(self.owner,'s123456'))['programs']['suspended'])

    async def test_stopping_cancels_host_initiated_sync_not_only_http_scopes(self):
        current = await self.lifecycle.credentials.authenticate(self.token_value)
        self.release = asyncio.Event()
        task = asyncio.create_task(self.lifecycle.units.sync(current))
        try:
            await asyncio.wait_for(self.entered.wait(),3)
            await self.lifecycle.stopping(self.owner,'s123456')
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.units.calls,[])
            self.assertEqual(self.lifecycle.units.tasks,{})
            # Until the hypervisor stop succeeds, the unknown slot stays held.
            self.assertTrue((await self.manager._metadata(self.owner,'s123456'))['programs']['retained'])
            await self.lifecycle.stopped(self.owner,'s123456')
            self.assertFalse((await self.manager._metadata(self.owner,'s123456'))['programs']['retained'])
        finally:
            self.release.set(); task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_zero_resident_capacity_never_starts_registered_units(self):
        self.manager.program_state.limit = 0
        self.units.calls.clear()
        result = await self.post()
        self.assertEqual(result.status_code,409,result.text)
        self.assertEqual(result.json()['errors']['_host'],'resident_vm_limit')
        self.assertFalse(any(row[1] in {'start','restart'} for row in self.units.calls))
        self.assertFalse((await self.manager._metadata(self.owner,'s123456'))['programs']['retained'])

    async def test_systemd_failure_is_not_reported_as_success(self):
        self.units.fail.add('TEST.service')
        original = self.units.manager.command
        def logged(args):
            if args[0]=='journalctl':
                return 0,'TEST last failure '+self.token_value
            return original(args)
        self.units.manager.command = logged
        response = await self.post()
        self.assertEqual(response.status_code,409,response.text)
        self.assertFalse(response.json()['ok'])
        self.assertIn('TEST last failure',response.json()['units'][0]['logs'])
        self.assertNotIn(self.token_value,response.text)
