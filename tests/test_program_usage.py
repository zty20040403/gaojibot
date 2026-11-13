"""Real usage reservations; model receipts are explicit local doubles."""
from types import SimpleNamespace
import unittest

from tests.qq_official_outbox_fixture import OutboxFixture
from tests.test_vm_sessions import BOOT
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions, ProgramDenied
from src.plugins.ai_chat.adapters.qq_official.program_execution import ProgramExecution
from src.plugins.ai_chat.adapters.qq_official.program_usage import ProgramUsage
from src.plugins.ai_chat.adapters.qq_official.program_wakes import ProgramWakes
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions


class ProgramUsageTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sessions = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.current = sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        wakes = ProgramWakes(ProgramActions(sessions))
        action,_ = wakes.submit(self.current,generation=0,program='TEST_PROGRAM',request_id='TEST_WAKE',text='TEST')
        self.execution = ProgramExecution(wakes)
        self.run = self.execution.claim(action['wake_id'])
        self.usage = UsageStore(self.database)
        self.addCleanup(self.usage.close)
        async def calls(fn,*args,**kwargs):
            return fn(*args,**kwargs)
        async def current():
            return self.execution.current(self.run)
        self.recorder = ProgramUsage(self.execution,self.run,self.usage,calls,current=current)

    def reservation(self):
        return SimpleNamespace(provider='TEST_PROVIDER',model='TEST_MODEL',reserved=200,counts=None)

    async def test_unknown_and_interrupted_usage_keeps_a_persistent_nonzero_bound(self):
        await self.recorder.budget.before_request()
        await self.recorder.budget.reserve_request(self.reservation())
        self.recorder.settle()
        self.assertEqual(self.query('SELECT scope_key,source,input_tokens,output_tokens,program_wake_id FROM usage_events'),
            [dict(scope_key=self.current.scope_key,source='program_wake_bound',input_tokens=200,output_tokens=200,program_wake_id=self.run['wake_id'])])
        self.execution.recover()
        self.assertEqual(self.query('SELECT source FROM usage_events'),[{'source':'program_wake_bound'}])

    async def test_known_usage_is_settled_once_even_if_the_conversation_stopped(self):
        receipt = self.reservation()
        await self.recorder.reserve(receipt)
        receipt.counts = 100,20,40
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.current.scope_key,))
        self.recorder.settle(); self.recorder.settle()
        self.assertEqual(self.query('SELECT source,input_tokens,output_tokens,cached_tokens FROM usage_events'),
            [dict(source='program_wake',input_tokens=100,output_tokens=20,cached_tokens=40)])

    async def test_scope_quota_and_stop_are_checked_before_reserving_a_call(self):
        self.usage.daily_input_token_limit = 150
        with self.assertRaisesRegex(ProgramDenied,'program_model_quota_exhausted'):
            await self.recorder.reserve(self.reservation())
        self.assertEqual(self.query('SELECT * FROM usage_events'),[])
        self.usage.daily_input_token_limit = 0
        await self.recorder.reserve(self.reservation())
        self.query('INSERT INTO conversation_settings(scope_key,stop_generation) VALUES (?,1)',(self.current.scope_key,))
        with self.assertRaisesRegex(ProgramDenied,'conversation_stopped_or_replaced'):
            await self.recorder.reserve(self.reservation())
        self.assertEqual(len(self.query('SELECT * FROM usage_events')),1)
