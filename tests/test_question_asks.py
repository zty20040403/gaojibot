"""Bound source waits on real PG/queue; this is not frontend/factory acceptance."""
import asyncio
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from src.bot_storage.async_calls import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost,assert_epoch
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.adapters.qq_official.question_asks import QuestionAsks
from src.plugins.ai_chat.adapters.qq_official.question_runtime import QuestionRuntime
from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
from tests.conversation_question_fixture import QuestionFixture


class QuestionAsksTests(QuestionFixture,unittest.IsolatedAsyncioTestCase):
    link_approver = 'TEST_QUESTION_LINK_APPROVER'

    def setUp(self):
        super().setUp(); self.calls = DatabaseCalls(); self.addAsyncCleanup(self.calls.close)
        self.active = True
        async def current(token):
            if not self.active or token is not self.token:raise LeaseLost('TEST_RUNTIME_CLOSED')
            def check():
                with self.tx() as cursor:assert_epoch(cursor,token)
            await self.calls(check)
        self.runtime = SimpleNamespace(store=SimpleNamespace(transaction=self.tx,admitted=self.admission),jobs=self.jobs,
            _calls=self.calls,_ensure_current=AsyncMock(side_effect=current),logger=Mock())
        self.service = QuestionRuntime(self.runtime,self.token,LinkIngress(self.auth,admission=self.admission))
        self.service.worker = object()  # Source port only; target reducer uses a real claimed job.
        self.binding = ExecutionBinding(turn_source,self.token,self.handle,self.job,self.admission)
        self.session = QuestionAsks(self.service,self.binding)

    async def issued(self):
        for _ in range(200):
            rows = await self.calls(self.query,'SELECT * FROM qq_official_conversation_questions ORDER BY created_at,question_id')
            if rows:return rows[0]
            await asyncio.sleep(.01)
        self.fail('TEST_QUESTION_NOT_SUBMITTED')

    def answer(self,*,refuse=False):
        job = self.jobs.claim_due('TEST_TARGET_WORKER',kinds=(account_job_kind('question',self.account.key),),now=self.now,limit=1)[0]
        with self.tx() as cursor:
            self.service.questions.claim(cursor,self.token,job)
            return self.service.questions.respond(cursor,self.token,job,refuse=refuse,answer=None if refuse else 'TEST_PRIVATE_SOURCE_ANSWER')

    async def start(self,question='TEST_PRIVATE_SOURCE_QUESTION'):
        task = asyncio.create_task(self.session.ask(self.edge['link_id'],question)); row = await self.issued()
        self.addAsyncCleanup(self.cancel,task)
        return task,row

    @staticmethod
    async def cancel(task):
        if not task.done():task.cancel()
        try:await task
        except asyncio.CancelledError:pass

    async def test_wait_is_private_real_queue_and_original_source_consumes_once(self):
        task,row = await self.start(); self.assertFalse(task.done()); self.assertIsNone(row['consumed_at'])
        answered = await self.calls(self.answer); result = await asyncio.wait_for(task,3)
        self.assertEqual((result.state,result.answer,result.question_id),('answered',answered['answer'],row['question_id']))
        self.assertNotIn(answered['answer'],repr(result)); self.assertEqual(result.payload()['answer'],answered['answer'])
        duplicate = await self.session.ask(self.edge['link_id'],row['question'])
        self.assertEqual((duplicate.state,duplicate.answer),('unavailable',''))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM qq_official_conversation_questions')[0]['n'],1)
        for table in ('messages','qq_official_inbound_events','delivery_plans','deliveries','durable_jobs'):
            data = repr(self.query('SELECT * FROM '+table)); self.assertNotIn(row['question'],data); self.assertNotIn(result.answer,data)

    async def test_refusal_is_explicit_and_has_no_answer(self):
        task,_ = await self.start(); await self.calls(self.answer,refuse=True)
        result = await asyncio.wait_for(task,3); self.assertEqual((result.state,result.reason,result.answer),('refused','declined',''))

    async def test_non_linked_raw_target_private_alias_and_invalid_request_do_not_enqueue(self):
        for target,text in (('f'*32,'TEST_Q'),(self.target.key,'TEST_Q'),(self.target.scope.key,'TEST_Q'),(self.edge['link_id'],'')):
            with self.subTest(target=target):
                result = await self.session.ask(target,text); self.assertEqual((result.state,result.answer),('unavailable',''))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM qq_official_conversation_questions')[0]['n'],0)

    async def test_target_stop_and_restore_does_not_reissue_old_intent_or_give_an_answer(self):
        task,row = await self.start()
        with self.tx() as cursor:
            change(cursor,self.target,'stop',now=self.now,actor=self.actor.key,authorized=True)
            change(cursor,self.target,'start',now=self.now,actor=self.actor.key,authorized=True)
        result = await asyncio.wait_for(task,3); self.assertEqual((result.state,result.reason,result.answer),('cancelled','target_unavailable',''))
        duplicate = await self.session.ask(self.edge['link_id'],row['question']); self.assertEqual(duplicate.state,'cancelled')
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM qq_official_conversation_questions')[0]['n'],1)

    async def test_original_deadline_expires_without_extending_or_consuming(self):
        task,row = await self.start(); self.assertEqual(row['deadline_at'],min(self.deadline,row['created_at']+7200))
        self.now = row['deadline_at']; result = await asyncio.wait_for(task,3)
        self.assertEqual((result.state,result.reason,result.answer),('expired','expired',''))
        actual = self.query('SELECT * FROM qq_official_conversation_questions')[0]
        self.assertEqual((actual['deadline_at'],actual['state'],actual['consumed_at']),(row['deadline_at'],'expired',None))

    async def test_cancel_wait_closes_question_and_queue_without_generic_defer_revival(self):
        task,row = await self.start(); task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        actual = self.query('SELECT * FROM qq_official_conversation_questions')[0]
        self.assertEqual(actual['state'],'cancelled'); self.assertEqual(self.jobs.get(row['job_id']).status,'cancelled')
        self.assertEqual(self.jobs.claim_due('TEST_NO_REPLAY',kinds=(account_job_kind('question',self.account.key),),now=self.now),[])

    async def test_double_cancel_during_committed_submit_keeps_id_and_closes_same_queue(self):
        committed,release = Event(),Event(); original = self.service.submit
        def blocked(*args):
            result = original(*args); committed.set(); release.wait(3); return result
        with patch.object(self.service,'submit',side_effect=blocked):
            task = asyncio.create_task(self.session.ask(self.edge['link_id'],'TEST_CANCEL_DURING_DB'))
            self.addAsyncCleanup(self.cancel,task)
            self.assertTrue(await asyncio.to_thread(committed.wait,2)); task.cancel(); await asyncio.sleep(.01); task.cancel()
            self.assertFalse(task.done()); release.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,3)
        row = self.query('SELECT * FROM qq_official_conversation_questions')[0]
        self.assertEqual(row['state'],'cancelled'); self.assertEqual(self.jobs.get(row['job_id']).status,'cancelled')

    async def test_second_cancel_cannot_drop_cleanup_waiting_for_a_database_slot(self):
        single = DatabaseCalls(concurrency=1); self.addAsyncCleanup(single.close); self.runtime._calls = single
        task,row = await self.start(); occupied,release = Event(),Event()
        def occupy():occupied.set(); release.wait(3)
        gate = asyncio.create_task(single(occupy))
        self.assertTrue(await asyncio.to_thread(occupied.wait,2))
        task.cancel(); await asyncio.sleep(.03); task.cancel(); await asyncio.sleep(.03)
        self.assertFalse(task.done()); release.set(); await gate
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,3)
        self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions')[0]['state'],'cancelled')
        self.assertEqual(self.jobs.get(row['job_id']).status,'cancelled')

    async def test_clock_after_consumption_permission_lookup_drops_body_but_keeps_fact(self):
        original = self.service.questions.authority._target
        def cross(cursor,token,row):
            target = original(cursor,token,row)
            if row.get('consumed_at') is not None:self.now = row['deadline_at']
            return target
        with patch.object(self.service.questions.authority,'_target',side_effect=cross):
            task,_ = await self.start(); await self.calls(self.answer); result = await asyncio.wait_for(task,3)
        self.assertEqual((result.state,result.answer),('unavailable',''))
        actual = self.query('SELECT * FROM qq_official_conversation_questions')[0]
        self.assertEqual(actual['state'],'answered'); self.assertIsNotNone(actual['consumed_at'])

    async def test_shutdown_after_consume_keeps_fact_but_never_returns_private_answer(self):
        original = self.session.poll
        def stop_after(*args):
            result = original(*args)
            if result is not None and result.state=='answered':self.active = False
            return result
        with patch.object(self.session,'poll',side_effect=stop_after):
            task,row = await self.start(); await self.calls(self.answer)
            result = await asyncio.wait_for(task,3)
        self.assertEqual((result.state,result.answer),('unavailable',''))
        actual = self.query('SELECT * FROM qq_official_conversation_questions')[0]
        self.assertEqual(actual['state'],'answered'); self.assertIsNotNone(actual['consumed_at'])
        self.assertEqual(self.jobs.get(row['job_id']).status,'succeeded')

    async def test_target_budget_changes_after_consume_before_return_drop_body_not_fact(self):
        original = self.session.poll
        def budget_after(*args):
            result = original(*args)
            if result is not None and result.state=='answered':
                self.query('INSERT INTO conversation_settings(scope_key,daily_budget_usd,budget_generation) VALUES (?,0,1) ON CONFLICT(scope_key) DO UPDATE SET daily_budget_usd=0,budget_generation=conversation_settings.budget_generation+1',(self.target.scope.key,))
            return result
        with patch.object(self.session,'poll',side_effect=budget_after):
            task,_ = await self.start(); await self.calls(self.answer); result = await asyncio.wait_for(task,3)
        self.assertEqual((result.state,result.answer),('unavailable',''))
        actual = self.query('SELECT * FROM qq_official_conversation_questions')[0]
        self.assertEqual(actual['state'],'answered'); self.assertIsNotNone(actual['consumed_at'])

    async def test_foreign_binding_cannot_poll_or_close_and_token_copy_is_not_runtime_token(self):
        task,row = await self.start()
        foreign = QuestionAsks(self.service,replace(self.binding,handle=replace(self.handle,endpoint=self.target)))
        for method in (foreign.poll,foreign.close):
            with self.assertRaises(PermissionError):await self.calls(method,row['question_id'])
        with self.assertRaises(ValueError):QuestionAsks(self.service,replace(self.binding,token=replace(self.token)))
        self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions')[0]['state'],'queued')
