"""Real VM-only HTTP, persistent wake worker and FakePlatform; local model/guest."""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests import test_vm_program_http as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.live_conversations import ConversationTasks
from src.plugins.ai_chat.conversation_window import ConversationStore
from src.plugins.ai_chat.model_budget import active_model_budget
from src.plugins.ai_chat.quota import UsageStore


class VMWakeHTTPTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    ensure_current = fixture.VMProgramHTTPTests.ensure_current
    make_sender = fixture.VMProgramHTTPTests.make_sender
    fast_sleep = fixture.VMProgramHTTPTests.fast_sleep

    async def asyncSetUp(self):
        await fixture.VMProgramHTTPTests.asyncSetUp(self)
        self.runner = self.lifecycle.programs
        self.runtime = self.service.runtime
        self.runtime.conversation_tasks = ConversationTasks()
        app = self.runner.model.app
        app.usage_store,app.conversations = UsageStore(self.database),ConversationStore(self.database)
        self.addCleanup(app.usage_store.close); self.addCleanup(app.conversations.close)
        app.message_ledger,app.db_call = self.ledger,self.calls
        app.model_preferences.get_scope_enabled_override = lambda _:True
        app.model_preferences.get_scope_default = lambda _:None
        app.skill_registry = SimpleNamespace(prompt_index=lambda:'')
        self.prompts = []
        async def model(prompt,_history,_tools,_execute,**options):
            self.prompts.append(prompt)
            budget = active_model_budget()
            await budget.before_request()
            _,receipt = budget.prepare(options['profile'],{'messages':[{'role':'user','content':prompt}],'max_tokens':64})
            await budget.reserve_request(receipt)
            receipt.finish({'prompt_tokens':30,'completion_tokens':10})
            return 'TEST model answer'
        self.runner.model.llm = model
        self.sending = asyncio.create_task(self.sender.run())
        self.addAsyncCleanup(self.close_workers)

    async def close_workers(self):
        await self.runner.close()
        self.sending.cancel()
        await asyncio.gather(self.sending,return_exceptions=True)

    async def post(self, key='TEST_REQUEST', **changes):
        return await self.client.post(self.base+'/vm/wake',json=dict(text='TEST_WAKE',request_id=key,program='TEST_PROGRAM')|changes)

    async def wait_state(self, action_id, state):
        async with asyncio.timeout(5):
            while True:
                result = await self.client.get(self.base+'/vm/actions/'+action_id)
                self.assertEqual(result.status_code,200,result.text)
                if result.json()['state']==state:
                    return result.json()
                await asyncio.sleep(0.01)

    async def test_wake_runs_once_and_only_actual_reply_enters_history(self):
        reply = await self.post()
        self.assertEqual(reply.status_code,202,reply.text)
        self.assertNotIn('token_hash',reply.json())
        self.assertNotIn('payload_json',reply.json())
        action = reply.json()['action_id']
        await self.wait_state(action,'completed')
        self.assertEqual((await self.post()).json()['action_id'],action)
        self.assertEqual(len(self.prompts),1)
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(len(self.query('SELECT * FROM messages')),1)
        self.assertEqual(self.query('SELECT source,input_tokens,output_tokens FROM usage_events'),
            [dict(source='program_wake',input_tokens=30,output_tokens=10)])
        self.assertEqual(self.query('SELECT state FROM qq_official_program_wakes'),[{'state':'completed'}])
        task_token,_ = self.service.gateway.open(source={'job_id':1},current=AsyncMock())
        forbidden = await self.client.post(self.base+'/vm/wake',headers={'Authorization':'Bearer '+task_token},json={})
        self.assertEqual(forbidden.status_code,401)
        self.assertEqual((await self.post('TEST_BAD',actor_key='TEST_OWNER')).status_code,400)

    async def test_overflow_is_one_next_hour_turn_and_keeps_all_members(self):
        self.query('INSERT INTO conversation_settings(scope_key,program_wake_limit) VALUES (?,1)',(self.endpoint.scope.key,))
        first = (await self.post('TEST_FIRST')).json()
        await self.wait_state(first['action_id'],'completed')
        left = (await self.post('TEST_LEFT',program='TEST_LEFT')).json()
        right = (await self.post('TEST_RIGHT',program='TEST_RIGHT')).json()
        self.assertEqual((left['state'],right['state'],left['wake_id']),('coalesced','coalesced',right['wake_id']))
        self.assertEqual(len(self.prompts),1)
        self.now = (self.now//3600+1)*3600
        self.runner.changed.set()
        await self.wait_state(left['action_id'],'completed')
        await self.wait_state(right['action_id'],'completed')
        self.assertEqual(len(self.prompts),2)
        self.assertIn('TEST_LEFT',self.prompts[-1]); self.assertIn('TEST_RIGHT',self.prompts[-1])
        self.assertEqual(len(self.fake.received),2)

    async def test_stop_cancels_inflight_model_and_start_never_replays_it(self):
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def slow(*args,**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.runner.model.llm = slow
        action = (await self.post()).json()['action_id']
        await asyncio.wait_for(entered.wait(),3)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
        self.runtime.conversation_tasks.interrupt(self.endpoint.key,before_generation=1)
        await asyncio.wait_for(cancelled.wait(),3)
        await self.wait_state(action,'cancelled')
        self.assertEqual((await self.post('TEST_STOPPED')).status_code,403)
        self.query('UPDATE conversation_settings SET stopped=FALSE')
        self.runner.model.llm = AsyncMock(return_value='[silence]')
        fresh = (await self.post('TEST_FRESH')).json()['action_id']
        self.assertTrue((await self.wait_state(fresh,'completed'))['silent'])
        await self.wait_state(action,'cancelled')
        self.runner.model.llm.assert_awaited_once()
        self.assertEqual(self.fake.received,[])

    def budget(self,value):
        from src.plugins.ai_chat.conversation_config import change
        with self.tx() as cursor:
            change(cursor,self.endpoint,'daily_budget_usd',value,actor='TEST_ADMIN',now=self.now)

    async def test_budget_denial_is_explicit_at_http_but_model_free_say_still_works(self):
        self.budget('0')
        response = await self.post('TEST_BUDGET_BLOCKED')
        self.assertEqual(response.status_code,403,response.text)
        self.assertEqual(response.json()['error'],'conversation_daily_budget_exceeded')
        self.assertEqual(self.prompts,[])
        said = await self.client.post(self.base+'/vm/say',json={'text':'TEST_FREE_SAY','program':'TEST_PROGRAM','request_id':'TEST_FREE'})
        self.assertEqual(said.status_code,200,said.text)
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(self.query('SELECT * FROM qq_official_program_wakes'),[])

    async def test_budget_restore_during_http_body_wait_rejects_captured_old_epoch(self):
        entered,release = asyncio.Event(),asyncio.Event()
        async def body():
            yield b'{"text":"TEST_BODY",'
            entered.set()
            await release.wait()
            yield b'"program":"TEST_PROGRAM","request_id":"TEST_SLOW_BODY"}'
        posting = asyncio.create_task(self.client.post(self.base+'/vm/wake',content=body()))
        try:
            await asyncio.wait_for(entered.wait(),3)
            # Wait until server-side bearer admission has captured the epoch;
            # the client beginning upload alone is not proof of authentication.
            async with asyncio.timeout(3):
                while not any(scope.requests for identity,scope in self.lifecycle.api.scopes.items() if identity.operation=='wake'):
                    await asyncio.sleep(0.01)
            self.budget('0'); self.budget('off'); release.set()
            response = await asyncio.wait_for(posting,5)
            self.assertEqual(response.status_code,403,response.text)
            self.assertEqual(response.json()['error'],'conversation_daily_budget_replaced')
            self.assertEqual(self.query('SELECT * FROM qq_official_program_actions'),[])
            self.assertEqual(self.prompts,[])
        finally:
            release.set()
            if not posting.done():posting.cancel()
            await asyncio.gather(posting,return_exceptions=True)

    async def test_budget_interrupts_inflight_model_and_restore_does_not_replay_it(self):
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def slow(*args,**kwargs):
            entered.set()
            try:await asyncio.Future()
            finally:cancelled.set()
        self.runner.model.llm = slow
        action = (await self.post()).json()['action_id']
        await asyncio.wait_for(entered.wait(),3)
        self.budget('0'); self.budget('off')
        await asyncio.wait_for(cancelled.wait(),3)
        self.assertEqual((await self.wait_state(action,'cancelled'))['error_code'],'conversation_daily_budget_replaced')
        self.runner.model.llm = AsyncMock(return_value='[silence]')
        fresh = (await self.post('TEST_FRESH_BUDGET')).json()['action_id']
        self.assertTrue((await self.wait_state(fresh,'completed'))['silent'])
        self.runner.model.llm.assert_awaited_once()
        self.assertEqual(self.fake.received,[])

    async def test_foreground_lock_delays_program_and_expired_work_is_not_started(self):
        lock = self.runtime.turn_locks.setdefault(self.endpoint.key,asyncio.Lock())
        async with lock:
            action = (await self.post()).json()['action_id']
            await self.runner.tick()
            self.assertEqual(self.prompts,[])
            self.now += 301
            await self.runner.tick()
            await self.wait_state(action,'cancelled')
        self.assertEqual(self.prompts,[])
        self.assertEqual(self.query('SELECT * FROM usage_events'),[])

    async def test_stop_during_claim_drain_does_not_leave_an_orphan_running_wake(self):
        entered,release = threading.Event(),threading.Event()
        original = self.runner.execution.claim
        def claim(wake_id):
            result = original(wake_id)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('TEST claim barrier timed out')
            return result
        self.runner.execution.claim = claim
        try:
            action = (await self.post()).json()['action_id']
            self.assertTrue(await asyncio.to_thread(entered.wait,3))
            self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
            self.runtime.conversation_tasks.interrupt(self.endpoint.key,before_generation=1)
            release.set()
            await self.wait_state(action,'cancelled')
            self.assertEqual(self.prompts,[])
        finally:
            release.set()

    async def test_disabling_program_interrupts_without_a_control_command(self):
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def slow(*args,**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.runner.model.llm = slow
        action = (await self.post()).json()['action_id']
        await asyncio.wait_for(entered.wait(),3)
        self.query('INSERT INTO program_controls VALUES (?,?,FALSE,?)',(self.endpoint.scope.key,'TEST_PROGRAM',self.now))
        await asyncio.wait_for(cancelled.wait(),3)
        self.assertEqual((await self.wait_state(action,'cancelled'))['error_code'],'program_disabled')
        self.assertEqual(self.fake.received,[])
