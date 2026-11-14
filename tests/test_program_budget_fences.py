"""Wake budget epochs before guest/body waits, separate from read/free-send auth."""
import asyncio
import json
import unittest

from starlette.exceptions import HTTPException

from src.plugins.ai_chat import daily_budgets
from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramDenied
from src.plugins.ai_chat.adapters.qq_official.program_execution import ProgramExecution
from src.plugins.ai_chat.adapters.qq_official.program_wakes import ProgramWakes
from tests import test_vm_action_auth as fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class ProgramBudgetFenceTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    prepare = fixture.VMActionAuthTests.prepare
    token_file = fixture.VMActionAuthTests.token_file
    asyncSetUp = fixture.VMActionAuthTests.asyncSetUp

    def configure(self,value):
        with self.tx() as cursor:
            return change(cursor,self.endpoint,'daily_budget_usd',value,actor='TEST_ADMIN',now=self.now)

    async def test_zero_budget_rejects_before_guest_but_does_not_disable_reads_or_free_say(self):
        self.configure('0')
        count = self.manager._guest_exec.await_count
        with self.assertRaises(HTTPException) as caught:
            await self.authority.authorize(self.value,write='wake')
        self.assertEqual((caught.exception.status_code,caught.exception.detail),(403,'conversation_daily_budget_exceeded'))
        self.assertEqual(self.manager._guest_exec.await_count,count)
        read = await self.authority.authorize(self.value)
        say = await self.authority.authorize(self.value,write=True)
        await read.current(); await say.current()
        self.assertFalse(read.revoked); self.assertFalse(say.revoked)

    async def test_rapid_budget_restore_during_guest_check_cannot_reauthorize_old_wake(self):
        original = self.manager._guest_exec.side_effect
        async def guest(*args,**kwargs):
            if json.loads(kwargs['input_bytes'])['op']=='inspect':
                self.configure('0'); self.configure('off')
            return await original(*args,**kwargs)
        self.manager._guest_exec.side_effect = guest
        with self.assertRaises(HTTPException) as caught:
            await self.authority.authorize(self.value,write='wake')
        self.assertEqual((caught.exception.status_code,caught.exception.detail),(403,'conversation_daily_budget_replaced'))
        self.manager._guest_exec.side_effect = original
        fresh = await self.authority.authorize(self.value,write='wake')
        self.assertEqual(fresh.generation,(0,3))
        await fresh.current()

    async def test_active_wake_is_cancelled_but_other_scopes_survive_and_new_epoch_is_distinct(self):
        wake = await self.authority.authorize(self.value,write='wake')
        read = await self.authority.authorize(self.value)
        say = await self.authority.authorize(self.value,write=True)
        waiting = asyncio.create_task(asyncio.Event().wait()); wake.requests.add(waiting)
        self.configure('0')
        result = await asyncio.wait_for(asyncio.gather(waiting,return_exceptions=True),3)
        self.assertIsInstance(result[0],asyncio.CancelledError)
        self.assertEqual(wake.denial,(403,'conversation_daily_budget_exceeded'))
        await read.current(); await say.current()
        self.configure('off')
        fresh = await self.authority.authorize(self.value,write='wake')
        self.assertIsNot(wake,fresh); self.assertEqual(fresh.generation,(0,3))
        self.assertFalse(read.revoked); self.assertFalse(say.revoked)

    async def test_queued_wake_and_same_request_id_never_revive_next_day_or_after_restore(self):
        wakes = ProgramWakes(self.actions)
        admission = self.authority.wakes.capture(self.current)
        row,_ = wakes.submit(self.current,generation=admission,program='TEST_PROGRAM',request_id='TEST_OLD',text='TEST')
        self.configure('0'); self.configure('off')
        with self.assertRaisesRegex(ProgramDenied,'daily_budget_replaced'):
            wakes.submit(self.current,generation=admission,program='TEST_PROGRAM',request_id='TEST_LATE',text='TEST')
        fresh = self.authority.wakes.capture(self.current)
        with self.assertRaisesRegex(ProgramDenied,'daily_budget_replaced'):
            wakes.submit(self.current,generation=fresh,program='TEST_PROGRAM',request_id='TEST_OLD',text='TEST')
        self.now = daily_budgets.interval(self.now)[1]
        with self.tx() as cursor,self.assertRaisesRegex(ProgramDenied,'daily_budget_replaced'):
            self.actions.check(cursor,row)
        self.assertIsNone(ProgramExecution(wakes).claim(row['wake_id']))
        saved = self.query('SELECT state,error_code FROM qq_official_program_actions WHERE action_id=?',(row['action_id'],))[0]
        self.assertEqual(saved,dict(state='cancelled',error_code='conversation_daily_budget_replaced'))
        new,_ = wakes.submit(self.current,generation=self.authority.wakes.capture(self.current),program='TEST_PROGRAM',
            request_id='TEST_NEW',text='TEST')
        self.assertEqual(new['budget_generation'],3)

    async def test_next_hour_buffers_use_budget_epoch_and_do_not_mix_old_and_fresh_inputs(self):
        self.query('INSERT INTO conversation_settings(scope_key,program_wake_limit) VALUES (?,1)',(self.endpoint.scope.key,))
        wakes = ProgramWakes(self.actions)
        old = self.authority.wakes.capture(self.current)
        wakes.submit(self.current,generation=old,program='TEST_PROGRAM',request_id='TEST_FIRST',text='TEST')
        pending,_ = wakes.submit(self.current,generation=old,program='TEST_PROGRAM',request_id='TEST_BUFFER',text='TEST')
        self.configure('0'); self.configure('off')
        fresh,_ = wakes.submit(self.current,generation=self.authority.wakes.capture(self.current),program='TEST_PROGRAM',
            request_id='TEST_FRESH_BUFFER',text='TEST')
        self.assertEqual(pending['state'],'coalesced'); self.assertEqual(fresh['state'],'coalesced')
        self.assertNotEqual(pending['wake_id'],fresh['wake_id'])
        self.now = (self.now//3600+1)*3600
        execution = ProgramExecution(wakes)
        self.assertIsNone(execution.claim(pending['wake_id']))
        self.assertIsNotNone(execution.claim(fresh['wake_id']))
