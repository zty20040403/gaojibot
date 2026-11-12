"""Host-side resident state uses real owned metadata and cross-instance locks."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from src.plugins.ai_chat.vm_program_state import VMProgramState
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager

OWNER = 'qqo:conversation:TEST_OWNER'
REPORT = dict(ok=True,units=[{'name':'TEST.service','ActiveState':'active'}],errors={})


class VMProgramStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = VmSandboxManager(image='/TEST_IMAGE',root=str(self.root))
        self.now = 1800000000
        self.store = VMProgramState(self.manager,limit=2,clock=lambda:self.now)
        for sid in ('s000001','s000002','s000003'):
            self.manager._directory(sid).mkdir()
            self.manager._save_metadata(sid,dict(id=sid,owner_ref=OWNER,owner_hash=self.manager._owner_hash(OWNER),
                created_at=1,last_used=1))

    async def state(self, sid='s000001'):
        return (await self.manager._metadata(OWNER,sid))['programs']

    async def test_slots_are_reserved_across_store_instances_before_guest_io(self):
        other = VMProgramState(self.manager,limit=2,clock=lambda:self.now)
        values = await asyncio.gather(self.store.admit(OWNER,'s000001','TEST_BOOT',enabled=True),
            other.admit(OWNER,'s000002','TEST_BOOT',enabled=True),
            self.store.admit(OWNER,'s000003','TEST_BOOT',enabled=True))
        self.assertEqual(sum(value[0] for value in values),2)
        self.assertEqual([value[1] for value in values if not value[0]],['resident_vm_limit'])
        rejected = next(sid for sid,value in zip(('s000001','s000002','s000003'),values) if not value[0])
        accepted = next(sid for sid,value in zip(('s000001','s000002','s000003'),values) if value[0])
        await other.report(OWNER,accepted,'TEST_BOOT',dict(ok=True,units=[],errors={}),allowed=True)
        self.assertTrue((await self.store.admit(OWNER,rejected,'TEST_BOOT',enabled=True))[0])

    async def test_unknown_outcome_does_not_release_reservation_and_metadata_updates_cannot_clobber_it(self):
        stale = await self.manager._metadata(OWNER,'s000001')
        await self.store.admit(OWNER,'s000001','TEST_BOOT',enabled=True)
        await self.store.report(OWNER,'s000001','TEST_BOOT',dict(ok=False,units=[],errors={'_sync':'TEST_UNKNOWN'}),allowed=True)
        self.manager._save_metadata('s000001',dict(stale,last_used=self.now))
        self.assertTrue((await self.state())['retained'])
        self.assertEqual((await self.manager._metadata(OWNER,'s000001'))['last_used'],self.now)
        fresh = VMProgramState(self.manager,limit=2,clock=lambda:self.now)
        self.assertTrue((await fresh.admit(OWNER,'s000001','TEST_BOOT',enabled=True))[0])
        self.assertNotIn('failures',await self.state()) # Host restart of same boot is not a crash.

    async def test_two_failures_open_durable_circuit_without_counting_duplicate_observations(self):
        for _ in range(2):
            await self.store.starting(OWNER,'s000001')
            await self.store.failed(OWNER,'s000001')
            await self.store.failed(OWNER,'s000001')
        state = await self.state()
        self.assertEqual(len(state['failures']),2)
        self.assertTrue(state['suspended']); self.assertTrue(state['alert'])
        fresh = VMProgramState(self.manager,clock=lambda:self.now)
        self.assertEqual(await fresh.admit(OWNER,'s000001','TEST_SAFE_BOOT',enabled=True),(False,'vm_programs_suspended'))
        await fresh.reset(OWNER,'s000001')
        self.assertTrue((await fresh.admit(OWNER,'s000001','TEST_SAFE_BOOT',enabled=True))[0])

    async def test_window_expiry_planned_stop_and_unexpected_boot_are_distinct(self):
        await self.store.starting(OWNER,'s000001'); await self.store.failed(OWNER,'s000001')
        self.now += 1801
        await self.store.starting(OWNER,'s000001'); await self.store.failed(OWNER,'s000001')
        self.assertFalse((await self.state()).get('suspended',False))
        await self.store.admit(OWNER,'s000001','TEST_BOOT_1',enabled=True)
        await self.store.stopping(OWNER,'s000001')
        await self.store.admit(OWNER,'s000001','TEST_BOOT_2',enabled=True)
        self.assertEqual(len((await self.state())['failures']),1)
        self.assertEqual(await self.store.admit(OWNER,'s000001','TEST_UNEXPECTED_BOOT',enabled=True),(False,'vm_programs_suspended'))

    async def test_wrong_owner_and_unreadable_state_cannot_free_a_resident_slot(self):
        with self.assertRaisesRegex(Exception,'沙盒不属于当前会话'):
            await self.store.admit('qqo:conversation:TEST_OTHER','s000001','TEST_BOOT',enabled=True)
        await self.store.admit(OWNER,'s000001','TEST_BOOT',enabled=True)
        path = self.manager._directory('s000001')/'programs.json'
        path.write_text('TEST_CORRUPTED')
        with self.assertRaises(ValueError):
            await self.store.admit(OWNER,'s000002','TEST_BOOT',enabled=True)
        path.unlink(); path.symlink_to(self.root/'TEST_MISSING')
        # A dangling link is not an absent state file and must fail closed.
        with self.assertRaises(OSError):
            await self.store.admit(OWNER,'s000002','TEST_BOOT',enabled=True)

    async def test_disabled_or_stale_boot_report_cannot_grant_retention(self):
        allowed,_ = await self.store.admit(OWNER,'s000001','TEST_BOOT',enabled=False)
        self.assertFalse(allowed)
        state = await self.store.report(OWNER,'s000001','TEST_BOOT',REPORT,allowed=False)
        self.assertTrue(state['wanted']); self.assertFalse(state['retained'])
        metadata = await self.manager._metadata(OWNER,'s000001')
        self.assertTrue(self.store.keeps_disk(metadata)); self.assertFalse(self.store.keeps_awake(metadata))
        with self.assertRaisesRegex(PermissionError,'vm_program_boot_replaced'):
            await self.store.report(OWNER,'s000001','TEST_OLD_BOOT',REPORT,allowed=True)
