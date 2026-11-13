"""Real DB/metadata/credentials/HTTP; only hypervisor and systemd are doubles."""
import asyncio
import shutil
import unittest
from unittest.mock import AsyncMock,patch

from tests import test_vm_units as unit_fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class ProgramKeeperTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await unit_fixture.VMUnitHTTPTests.asyncSetUp(self)
        self.assertEqual((await self.client.post(self.url,json={})).status_code,200)
        self.running,self.boot_number,self.fail_start = True,1,False
        self.manager._running.side_effect = lambda _:self.running
        async def start(owner,sid,*,automatic=False):
            await self.lifecycle.starting(owner,sid,automatic=automatic)
            if self.fail_start:
                raise RuntimeError('TEST_VM_START_FAILED')
            self.running = True
            self.boot_number += 1
            self.boot.write_text('00000000-0000-4000-8000-'+str(self.boot_number).zfill(12))
            shutil.rmtree(self.units.runtime); self.units.runtime.mkdir()
            self.units.states.clear()
            await self.lifecycle.ready(owner,sid,automatic=automatic)
        self.manager.start_owned = AsyncMock(side_effect=start)

    async def state(self):
        return (await self.manager._metadata(self.owner,'s123456'))['programs']

    async def test_offline_retained_vm_recovers_once_and_rotates_old_credentials(self):
        old = self.token_value
        self.running = False
        await self.lifecycle.keeper.scan()
        self.manager.start_owned.assert_awaited_once_with(self.owner,'s123456',automatic=True)
        self.assertTrue(self.running)
        self.assertEqual(self.units.states['TEST.service'],'active')
        self.assertTrue((await self.state())['retained'])
        with self.assertRaises(PermissionError):
            self.lifecycle.store.authenticate(old)
        await self.lifecycle.keeper.scan()
        self.manager.start_owned.assert_awaited_once()
        self.assertEqual(len((await self.state())['failures']),1)

    async def test_two_start_failures_suspend_future_recovery_without_erasing_registry(self):
        # A retained VM known to be stopped when the host starts: initial
        # inventory is not itself counted as a failed boot attempt.
        state = await self.state()
        self.manager.program_state.manager._save_metadata('s123456',dict(state,running=False),filename='programs.json')
        self.running,self.fail_start = False,True
        for _ in range(3):
            await self.lifecycle.keeper.scan()
        self.assertEqual(self.manager.start_owned.await_count,2)
        state = await self.state()
        self.assertTrue(state['suspended']); self.assertTrue(state['alert'])
        self.assertFalse(state['retained']); self.assertTrue(state['wanted'])
        self.assertTrue((self.units.registry/'TEST.service').is_file())

    async def test_explicit_stop_is_not_undone_and_global_disable_does_not_boot(self):
        await self.lifecycle.stopping(self.owner,'s123456')
        self.running = False
        await self.lifecycle.stopped(self.owner,'s123456')
        await self.lifecycle.keeper.scan()
        self.manager.start_owned.assert_not_awaited()
        self.assertTrue((await self.state())['paused'])
        await self.manager.program_state.reset(self.owner,'s123456',
            expected=self.manager.program_state.recovery_fence(await self.manager._metadata(self.owner,'s123456')),
            request_id='TEST_RESUME',authorize=AsyncMock())
        self.query('INSERT INTO conversation_settings(scope_key,programs_enabled) VALUES (?,FALSE)',(self.endpoint.scope.key,))
        await self.lifecycle.keeper.scan()
        self.manager.start_owned.assert_not_awaited()

    async def test_idle_stop_can_resume_after_reenable_and_reservation_cannot_override_manual_stop(self):
        await self.lifecycle.stopping(self.owner,'s123456',automatic=True)
        self.running = False
        await self.lifecycle.stopped(self.owner,'s123456')
        self.assertFalse((await self.state())['paused'])
        await self.lifecycle.keeper.scan()
        self.assertTrue(self.running)
        self.manager.start_owned.assert_awaited_once()
        self.running = False
        self.assertTrue(await self.manager.program_state.reserve_start(self.owner,'s123456'))
        await self.lifecycle.stopping(self.owner,'s123456')
        await self.lifecycle.stopped(self.owner,'s123456')
        with self.assertRaisesRegex(PermissionError,'vm_autostart_cancelled'):
            await self.lifecycle.starting(self.owner,'s123456',automatic=True)
        self.assertFalse(self.running)

    async def test_close_during_guest_restore_drains_helper_tasks(self):
        await self.lifecycle.keeper.close()
        self.release = asyncio.Event(); self.entered.clear()
        task = self.lifecycle.keeper.task = asyncio.create_task(self.lifecycle.keeper.scan())
        try:
            await asyncio.wait_for(self.entered.wait(),3)
            await asyncio.wait_for(self.lifecycle.close(),3)
            self.assertTrue(task.done())
            self.assertEqual(self.lifecycle.units.tasks,{})
            self.assertTrue((await self.state())['retained'])
        finally:
            self.release.set(); task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_capacity_zero_and_revoked_route_cannot_boot_retained_vm(self):
        self.running = False
        self.manager.program_state.limit = 0
        await self.lifecycle.keeper.scan()
        self.manager.start_owned.assert_not_awaited()
        self.assertEqual((await self.state())['hold_reason'],'resident_vm_limit')
        self.manager.program_state.limit = 4
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?",(self.endpoint.key,))
        previous = await self.state()
        await self.lifecycle.keeper.scan()
        self.manager.start_owned.assert_not_awaited()
        self.assertEqual(await self.state(),previous)

    async def test_cancelling_one_vm_sync_does_not_cancel_scan_or_periodic_keeper(self):
        self.release = asyncio.Event(); self.entered.clear()
        scan = asyncio.create_task(self.lifecycle.keeper.scan())
        try:
            await asyncio.wait_for(self.entered.wait(),3)
            await self.lifecycle.stopping(self.owner,'s123456')
            await asyncio.wait_for(scan,3)
            self.assertFalse(scan.cancelled())
            self.assertFalse(self.lifecycle.keeper.task.done())
            self.assertEqual(self.lifecycle.units.tasks,{})
        finally:
            self.release.set(); scan.cancel(); await asyncio.gather(scan,return_exceptions=True)

    async def test_committed_policy_can_wake_periodic_scan_from_admin_thread(self):
        entered = asyncio.Event()
        with patch.object(self.lifecycle.keeper,'scan',AsyncMock(side_effect=entered.set)) as scan:
            await asyncio.to_thread(self.lifecycle.keeper.wake)
            await asyncio.wait_for(entered.wait(),3)
            scan.assert_awaited_once()

    async def test_closing_host_cancels_keeper_without_erasing_retained_intent(self):
        task = self.lifecycle.keeper.task
        await self.lifecycle.close()
        self.assertTrue(task.done())
        self.assertTrue((await self.state())['wanted'])
        self.assertTrue((await self.state())['retained'])
