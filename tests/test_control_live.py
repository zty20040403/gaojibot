"""Authenticated fake gateway -> ingress -> cancellation -> real loopback receipt."""
import asyncio
from unittest.mock import patch
import unittest

from tests import test_qq_official_turn_endtoend as base
from tests.test_qq_official_runtime_sender import verified_ready_platform
from src.plugins.ai_chat.adapters.qq_official.live_conversations import ConversationTasks
from src.plugins.ai_chat.adapters.qq_official.message_batch import MessageBatcher


class LiveControlTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.OfficialTurnEndToEndTests.setUp
    runtime = base.OfficialTurnEndToEndTests.runtime
    query = base.OfficialTurnEndToEndTests.query
    wait_db = base.OfficialTurnEndToEndTests.wait_db
    application = base.OfficialTurnEndToEndTests.application
    open_runtime = base.OfficialTurnEndToEndTests.open_runtime
    emit = base.OfficialTurnEndToEndTests.emit
    complete = base.OfficialTurnEndToEndTests.complete

    async def test_stop_bypasses_busy_turn_and_start_only_runs_new_input(self):
        app = self.application()
        entered,cancelled = asyncio.Event(),asyncio.Event()
        calls = []
        async def completion(**kwargs):
            calls.append(kwargs)
            if len(calls)==1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set(); raise
            return base.model_response('TEST new response',[],kwargs)
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake,app)
                await self.emit(fake,native='TEST_BUSY',content='TEST long question')
                await asyncio.wait_for(entered.wait(),5)
                await self.emit(fake,native='TEST_STOP',content='/stop')
                await self.complete('TEST_STOP')
                await asyncio.wait_for(cancelled.wait(),1)
                await self.emit(fake,native='TEST_PAUSED',content='TEST do not reply')
                await self.wait_db("SELECT canonical_message_id FROM qq_official_inbound_events WHERE native_message_id='TEST_PAUSED' AND status='processed'")
                await self.emit(fake,native='TEST_HELP',content='/help')
                await self.complete('TEST_HELP')
                self.assertEqual(len(calls),1)
                self.assertEqual(self.query("SELECT status FROM agent_turns"),[{'status':'aborted'}])
                await self.emit(fake,native='TEST_START',content='/start')
                await self.complete('TEST_START')
                await self.emit(fake,native='TEST_NEW',content='TEST new question')
                await self.complete('TEST_NEW')
                self.assertEqual(len(calls),2)
                self.assertEqual(len(fake.received),4)  # stop/help/start/new, never old work
                await runtime.stop()

    async def test_database_control_from_another_process_cancels_live_work(self):
        from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
        from src.plugins.ai_chat.platform_types import EndpointKey
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def completion(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set(); raise
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake,self.application())
                await self.emit(fake,native='TEST_BUSY_REMOTE',content='TEST long question')
                await asyncio.wait_for(entered.wait(),5)
                endpoint = EndpointKey(runtime.loaded.account,'private','TEST_RUNTIME_OWNER')
                with runtime.store.transaction() as cursor:
                    change(cursor,endpoint,'stop',now=int(runtime.store.clock()),actor='TEST_ADMIN',authorized=True)
                # Deliberately do not call the in-process notification callback.
                await asyncio.wait_for(cancelled.wait(),3)
                await self.wait_db("SELECT turn_id FROM agent_turns WHERE status='aborted'")
                self.assertEqual(fake.received,[])
                await runtime.stop()

    async def test_stop_forgets_unfinished_batch_without_replaying_after_start(self):
        app = self.application()
        calls = []
        async def completion(**kwargs):
            calls.append(kwargs)
            return base.model_response('TEST response',[],kwargs)
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=completion), patch(
                'src.plugins.ai_chat.adapters.qq_official.message_batch.MessageBatcher',
                side_effect=lambda dispatch:MessageBatcher(dispatch,idle=.4,maximum=1)):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake,app)
                await self.emit(fake,native='TEST_BATCH',content='那个')
                await self.wait_db("SELECT canonical_message_id FROM qq_official_inbound_events WHERE native_message_id='TEST_BATCH' AND status='processed'")
                await self.emit(fake,native='TEST_STOP',content='/stop')
                await self.complete('TEST_STOP')
                await self.emit(fake,native='TEST_START',content='/start')
                await self.complete('TEST_START')
                await asyncio.sleep(.5)
                self.assertEqual(calls,[])
                self.assertFalse(self.query("SELECT * FROM qq_official_turn_executions x JOIN qq_official_inbound_events i USING(inbox_id) WHERE i.native_message_id='TEST_BATCH'"))
                await runtime.stop()


class ConversationTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_tracking_job_cancel_and_generation_isolation(self):
        work = ConversationTasks()
        ready = asyncio.Queue()
        async def run(endpoint,generation,job):
            with work.track(endpoint,generation,kind='agent',job_id=job):
                with work.track(endpoint,generation,kind='result',job_id=job):
                    pass
                self.assertEqual(work.active[asyncio.current_task()].kind,'agent')
                await ready.put(True)
                await asyncio.Event().wait()
        old = asyncio.create_task(run('TEST_A',0,1))
        new = asyncio.create_task(run('TEST_A',1,2))
        other = asyncio.create_task(run('TEST_B',0,3))
        try:
            for _ in range(3):await ready.get()
            work.interrupt('TEST_A',before_generation=1)
            await asyncio.sleep(0)
            self.assertTrue(old.cancelled())
            self.assertFalse(new.done()); self.assertFalse(other.done())
            work.cancel_jobs({2})
            await asyncio.sleep(0)
            self.assertTrue(new.cancelled()); self.assertFalse(other.done())
        finally:
            for task in (old,new,other):task.cancel()
            await asyncio.gather(old,new,other,return_exceptions=True)
        self.assertEqual(work.active,{})
