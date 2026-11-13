"""Real lifecycle, ownership metadata and DB; replace only the guest-agent hop."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.adapters.qq_official.command_runner import CommandRunner
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from tests import test_vm_lifecycle as fixture
from tests.test_command_execution import report
from tests.qq_official_outbox_fixture import OutboxFixture


class CommandRunnerTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.VMLifecycleTests.asyncSetUp(self)
        await self.lifecycle.commands.close()
        await self.lifecycle.programs.close()  # Inspect the saved wake, without executing a model.
        self.runner = self.lifecycle.commands = CommandRunner(self.lifecycle)
        self.current = self.lifecycle.store.authenticate(self.token_value)
        self.requests,self.guest_reports = [],{}
        self.hold,self.lose_start = None,False
        self.entered = asyncio.Event()
        original = self.manager._guest_exec
        async def guest(sid,path,args,*,input_bytes=None,timeout=60):
            if args!=['-c',self.runner.script]:
                return await original(sid,path,args,input_bytes=input_bytes,timeout=timeout)
            self.assertEqual((sid,path,timeout),('s123456','/usr/bin/python3',30))
            request = json.loads(input_bytes); self.requests.append(request); self.entered.set()
            if self.hold:
                await self.hold.wait()
            key = request['command_id']
            if request['op']=='start':
                self.guest_reports.setdefault(key,report())
                if self.lose_start:
                    raise OSError('TEST_GUEST_REPLY_LOST')
            if request['op']=='stop':
                self.guest_reports[key] = report(state='cancelled',stopped=True,error='command_cancelled')
            return 0,json.dumps(self.guest_reports.get(key,report(state='ambiguous',error='command_start_unconfirmed'))).encode(),b''
        self.manager._guest_exec = AsyncMock(side_effect=guest)

    def reserve(self, request='TEST_START'):
        row,_ = self.runner.commands.reserve(self.current,generation=(0,0),program='TEST_PROGRAM',request_id=request,argv=['printf','TEST_RESULT'])
        return row['command_id']

    def stored(self, key):
        return self.query('SELECT * FROM qq_official_background_commands WHERE command_id=?',(key,))[0]

    async def test_composed_start_output_completion_and_once_only_notice_without_model_wait(self):
        key = self.reserve()
        # run-bg can be requested by an existing shell that holds the VM lock.
        async with self.manager.io_lock('s123456'):
            await asyncio.wait_for(self.runner.visit(key),3)
        self.assertEqual(self.stored(key)['state'],'running')
        self.assertEqual(self.requests[0]['argv'],['printf','TEST_RESULT'])
        self.assertFalse(self.query('SELECT * FROM usage_events'))
        self.guest_reports[key] = report(state='completed',stopped=True,exit_code=0,elapsed_seconds=11)
        await self.runner.visit(key); await self.runner.visit(key)
        self.assertEqual(self.stored(key)['elapsed_seconds'],11)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_program_actions')),1)
        self.assertEqual([row['op'] for row in self.requests],['start','status'])
        self.assertNotIn(self.token_value,json.dumps(self.requests))

    async def test_lost_start_response_recovers_by_status_never_by_reexecution(self):
        key = self.reserve(); self.lose_start = True
        await self.runner.visit(key)
        self.assertEqual(self.stored(key)['state'],'ambiguous')
        self.assertIsNone(self.stored(key)['finished_at'])
        self.runner = self.lifecycle.commands = CommandRunner(self.lifecycle)
        await self.runner.visit(key)
        self.assertEqual(self.stored(key)['state'],'running')
        self.assertEqual([row['op'] for row in self.requests],['start','status'])

    async def test_revoke_during_hypervisor_wait_prevents_the_start_rpc(self):
        key = self.reserve()
        entered,release = asyncio.Event(),asyncio.Event()
        async def running(_):
            entered.set(); await release.wait(); return True
        self.manager._running.side_effect = running
        task = asyncio.create_task(self.runner.visit(key))
        try:
            await asyncio.wait_for(entered.wait(),3)
            self.lifecycle.store.revoke(self.endpoint)
            release.set(); await asyncio.wait_for(task,3)
            self.assertEqual(self.requests,[])
            self.assertIsNone(self.stored(key)['finished_at'])
            await self.runner.visit(key)
            self.assertEqual([row['op'] for row in self.requests],['stop'])
            self.assertEqual(self.stored(key)['state'],'cancelled')
        finally:
            release.set(); task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_kill_stops_in_guest_and_offline_vm_never_boots_for_old_work(self):
        key = self.reserve(); await self.runner.visit(key)
        with self.tx() as cursor:
            change(cursor,self.endpoint,'kill',now=self.now,actor='TEST_ADMIN',authorized=True)
        await self.runner.visit(key)
        self.assertEqual(self.requests[-1]['op'],'stop')
        self.assertIsNotNone(self.stored(key)['finished_at'])
        self.assertFalse(self.query('SELECT * FROM qq_official_program_actions'))
        row,_ = self.runner.commands.reserve(self.current,generation=(0,1),program='TEST_PROGRAM',request_id='TEST_OFFLINE',argv=['true'])
        self.manager._running.return_value = False
        before = len(self.requests)
        await self.runner.visit(row['command_id'])
        self.assertEqual(len(self.requests),before)
        self.assertEqual(self.stored(row['command_id'])['error_code'],'vm_not_running')

    async def test_monitor_capacity_and_shutdown_keep_unconfirmed_slots(self):
        keys = [self.reserve('TEST_'+str(index)) for index in range(8)]
        self.hold = asyncio.Event()
        await self.runner.tick()
        self.assertEqual(len(self.runner.active),4)
        await asyncio.wait_for(self.entered.wait(),3)
        interrupted = next(iter(self.runner.active.values()))
        interrupted.cancel(); await asyncio.gather(interrupted,return_exceptions=True)
        await self.runner.tick()  # One interrupted helper does not kill the monitor.
        self.assertEqual(len(self.runner.active),4)
        await self.runner.close()
        self.assertEqual(self.runner.active,{})
        self.assertTrue(all(self.stored(key)['finished_at'] is None for key in keys))
        self.assertFalse(self.lifecycle.store.revoke_vm('s123456',idle_owner=self.owner))
