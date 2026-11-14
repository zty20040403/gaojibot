"""One-attempt private waits on real PG/queue; no target model or QQ effect."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.adapters.qq_official.question_authority import QuestionAuthority
from src.plugins.ai_chat.adapters.qq_official.questions import Questions
from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
from src.plugins.ai_chat.platform_types import ActorKey
from tests.conversation_question_fixture import QuestionFixture


class QuestionsTests(QuestionFixture):
    link_approver = 'TEST_QUESTION_LINK_APPROVER'

    def setUp(self):
        super().setUp(); self.binding = ExecutionBinding(turn_source,self.token,self.handle,self.job,self.admission)
        self.authority = QuestionAuthority(LinkIngress(self.auth,admission=self.admission),admission=self.admission)
        self.questions = Questions(self.authority,self.jobs)

    def submit(self,text='TEST_PRIVATE_WAIT_QUESTION'):
        with self.tx() as cursor:return self.questions.submit(cursor,self.token,self.binding,self.edge['link_id'],text)

    def target_job(self):return self.jobs.claim_due('TEST_QUESTION_WORKER',kinds=(account_job_kind('question',self.account.key),),now=self.now,limit=1)[0]

    def claim(self,job=None,**kwargs):
        if job is None:return super().claim(**kwargs)
        with self.tx() as cursor:return self.questions.claim(cursor,self.token,job)

    def respond(self,job,**kwargs):
        with self.tx() as cursor:return self.questions.respond(cursor,self.token,job,**kwargs)

    def consume(self,row,binding=None):
        with self.tx() as cursor:return self.questions.consume(cursor,self.token,row['question_id'],binding or self.binding)

    def close(self,row,**kwargs):
        with self.tx() as cursor:return self.questions.close(cursor,self.token,row['question_id'],**kwargs)

    def current(self,job):
        with self.tx() as cursor:return self.questions.current(cursor,self.token,job)

    def test_duplicate_request_private_answer_and_exactly_one_consume(self):
        row,created = self.submit(); self.assertTrue(created)
        self.now += 1; duplicate,created = self.submit(); self.assertFalse(created); self.assertEqual(duplicate,row)
        job = self.target_job(); running,claimed = self.claim(job); self.assertTrue(claimed)
        self.assertEqual(self.claim(job),(running,False)); self.assertEqual(self.current(job),running)
        answer = self.respond(job,answer='TEST_PRIVATE_WAIT_ANSWER'); self.assertEqual(answer['state'],'answered')
        consumed = self.consume(answer); self.assertEqual(consumed['answer'],'TEST_PRIVATE_WAIT_ANSWER'); self.assertIsNone(self.consume(consumed))
        self.assertEqual(self.jobs.get(job.job_id).status,'succeeded')
        for table in ('messages','qq_official_inbound_events','durable_jobs','delivery_plans','deliveries'):
            data = repr(self.query('SELECT * FROM '+table))
            self.assertNotIn(row['question'],data); self.assertNotIn(answer['answer'],data)

    def test_refusal_is_private_terminal_and_not_reexecuted_after_generic_job_requeue(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job)
        refusal = self.respond(job,refuse=True)
        self.assertEqual((refusal['state'],refusal['answer'],refusal['reason_code']),('refused','','declined'))
        self.assertEqual(self.consume(refusal)['state'],'refused')
        self.assertFalse(self.jobs.requeue(job.job_id,now=self.now))
        self.assertEqual(self.close(refusal),self.query('SELECT * FROM qq_official_conversation_questions')[0])

    def test_bound_job_owner_generation_payload_lane_and_source_binding_cannot_answer(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job)
        for bad in (replace(job,lease_owner='TEST_OTHER_WORKER'),replace(job,lease_generation=2),replace(job,scope_key=self.endpoint.key),
            replace(job,kind='TEST_OTHER_KIND'),replace(job,payload={'question_id':row['question_id'],'role':'owner'}),self.binding):
            with self.subTest(kind=type(bad).__name__),self.assertRaises(PermissionError):self.respond(bad,answer='TEST_FORGED_ANSWER')
        self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions')[0]['state'],'running')

    def test_expired_worker_close_drains_queue_and_defer_or_requeue_never_replays_question(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job); self.now += 301
        with self.assertRaisesRegex(PermissionError,'question_worker_replaced'):self.current(job)
        closed = self.close(row,reason='model_unknown'); self.assertEqual(closed['state'],'unknown')
        self.assertFalse(self.jobs.defer(job,now=self.now)); self.assertEqual(self.jobs.get(job.job_id).status,'failed')
        self.assertTrue(self.jobs.requeue(job.job_id,now=self.now)); new = self.target_job()
        with self.assertRaisesRegex(PermissionError,'question_worker_replaced'):self.claim(new)
        self.assertEqual(self.close(row),closed); self.assertIsNone(self.consume(closed))

    def test_deferred_running_queue_is_closed_instead_of_resuming_a_model_request(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job)
        self.assertTrue(self.jobs.defer(job,now=self.now))
        with self.tx() as cursor:
            row = self.questions.get(cursor,self.token,row['question_id']); closed = self.questions.retire(cursor,self.token,row)
        self.assertEqual(closed['state'],'unknown'); self.assertEqual(self.jobs.get(job.job_id).status,'failed')
        self.assertEqual(self.jobs.claim_due('TEST_NO_REPLAY',kinds=(account_job_kind('question',self.account.key),),now=self.now),[])

    def test_submit_lookup_does_not_extend_original_deadline_or_leave_a_partial_queue(self):
        original = self.authority.capture; calls = 0
        def cross(*args):
            nonlocal calls
            fields = original(*args); calls += 1
            if calls==1:self.now = self.deadline
            return fields
        with patch.object(self.authority,'capture',side_effect=cross),self.assertRaisesRegex(PermissionError,'question_source_expired'):
            self.submit()
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_questions')[0]['n'],0)
        self.assertEqual(self.jobs.claim_due('TEST_NO_PARTIAL_QUEUE',kinds=(account_job_kind('question',self.account.key),),now=self.now),[])

    def test_target_stop_start_closes_original_wait_and_same_intent_does_not_reissue(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job)
        with self.tx() as cursor:change(cursor,self.target,'stop',now=self.now,actor=self.actor.key,authorized=True)
        closed = self.current(job); self.assertEqual((closed['state'],closed['reason_code']),('cancelled','target_unavailable'))
        with self.tx() as cursor:change(cursor,self.target,'start',now=self.now,actor=self.actor.key,authorized=True)
        self.assertEqual(self.submit(),(closed,False)); self.assertFalse(self.jobs.defer(job,now=self.now))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM qq_official_conversation_questions')[0]['n'],1)

    def test_budget_restore_keeps_closed_intent_but_allows_a_new_question(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job)
        self.query('''INSERT INTO conversation_settings(scope_key,daily_budget_usd,budget_generation) VALUES (?,0,1)
            ON CONFLICT(scope_key) DO UPDATE SET daily_budget_usd=0,budget_generation=conversation_settings.budget_generation+1''',(self.target.scope.key,))
        closed = self.current(job); self.assertEqual(closed['state'],'cancelled')
        self.query('UPDATE conversation_settings SET daily_budget_usd=NULL,budget_generation=budget_generation+1 WHERE scope_key=?',(self.target.scope.key,))
        self.assertEqual(self.submit(),(closed,False)); fresh,created = self.submit('TEST_NEW_EXPLICIT_QUESTION')
        self.assertTrue(created); self.assertNotEqual(fresh['question_id'],row['question_id'])

    def test_deadline_between_authority_lookup_and_claim_never_starts_target(self):
        row,_ = self.submit(); self.jobs.lease_seconds=10000; job = self.target_job(); original = self.authority.current
        def cross(*args):
            result = original(*args); self.now = row['deadline_at']; return result
        with patch.object(self.authority,'current',side_effect=cross):closed,claimed = self.claim(job)
        self.assertFalse(claimed); self.assertEqual((closed['state'],closed['started_at']),('expired',None))
        self.assertEqual(self.jobs.get(job.job_id).status,'cancelled')

    def test_deadline_after_authority_lookup_drops_answer_but_keeps_private_expiry(self):
        row,_ = self.submit(); self.jobs.lease_seconds=10000; job = self.target_job(); self.claim(job); original = self.authority.current
        def cross(*args):
            result = original(*args); self.now = row['deadline_at']; return result
        with patch.object(self.authority,'current',side_effect=cross):closed = self.respond(job,answer='TEST_LATE_ANSWER')
        self.assertEqual((closed['state'],closed['answer']),('expired','')); self.assertIsNone(self.consume(closed))

    def test_response_final_lease_check_does_not_accept_an_answer_after_worker_expiry(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job); original = self.questions.current
        def cross(*args):
            result = original(*args); self.now = job.lease_until; return result
        with patch.object(self.questions,'current',side_effect=cross),self.assertRaisesRegex(PermissionError,'question_worker_replaced'):
            self.respond(job,answer='TEST_LEASE_EXPIRED_ANSWER')
        self.assertEqual(self.close(row,reason='model_unknown')['state'],'unknown')

    def test_only_original_source_may_consume_and_late_result_does_not_mutate_terminal(self):
        row,_ = self.submit(); job = self.target_job(); self.claim(job); answered = self.respond(job,answer='TEST_ANSWER')
        fake = ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_FOREIGN_CALLER')
        for handle in (replace(self.handle,endpoint=self.target),replace(self.handle,actor=fake)):
            with self.subTest(kind=handle.actor.identity_kind),self.assertRaisesRegex(PermissionError,'question_source_unavailable'):
                self.consume(answered,replace(self.binding,handle=handle))
        self.now = answered['deadline_at']; self.assertIsNone(self.consume(answered))
        self.assertEqual(self.close(answered),answered); self.assertEqual(self.jobs.get(job.job_id).status,'succeeded')

    def test_restart_closes_pending_and_started_without_touching_completed_facts(self):
        pending,_ = self.submit(); started,_ = self.submit('TEST_STARTED'); done,_ = self.submit('TEST_DONE')
        first = self.target_job(); self.claim(first); completed = self.respond(first,answer='TEST_COMPLETED')
        second = self.target_job(); self.claim(second)
        self.account_lease.close(); self.token = self.lease().acquire()
        with self.tx() as cursor:self.assertEqual(self.questions.recover(cursor,self.token),2)
        rows = {r['question_id']:r for r in self.query('SELECT * FROM qq_official_conversation_questions')}
        self.assertEqual(rows[completed['question_id']],completed)
        self.assertEqual(rows[started['question_id']]['state'],'unknown'); self.assertEqual(rows[done['question_id']]['state'],'cancelled')
        self.assertEqual(self.jobs.claim_due('TEST_RESTARTED',kinds=(account_job_kind('question',self.account.key),),now=self.now),[])

    def test_concurrent_same_intent_and_per_source_bound_are_atomic(self):
        barrier = Barrier(2)
        def submit():barrier.wait(); return self.submit()
        with ThreadPoolExecutor(max_workers=2) as pool:results = list(pool.map(lambda _:submit(),range(2)))
        self.assertEqual(sorted(created for _,created in results),[False,True]); self.assertEqual(results[0][0],results[1][0])
        for i in range(3):self.submit('TEST_BOUNDED_'+str(i))
        with self.assertRaisesRegex(PermissionError,'question_queue_full'):self.submit('TEST_REJECT_FIFTH')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_questions')[0]['n'],4)
