import asyncio
from tests.qq_official_native_schedule_fixture import NativeFixture
from src.plugins.ai_chat.message_ir import MessageBody,TextNode

class ExpiredOccurrenceTests(NativeFixture):
    def queued_routine(self):
        item=self.routine();self.finish_foreground();self.now=item.next_due_at
        self.assertEqual(len(self.engine.materialize_due()),1)
        return dict(self.query('SELECT * FROM qq_official_native_schedule_occurrences')[0])

    def test_missed_execution_retires_run_and_next_cron_occurrence_can_execute(self):
        first=self.queued_routine();self.now=first['execution_deadline']+1
        job=self.claim_native();self.assertEqual(self.engine.begin(job)[0],'expired')
        self.assertTrue(self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation))
        original=self.query('SELECT * FROM qq_official_native_schedule_occurrences')[0]
        run=self.query('SELECT * FROM routine_runs')[0]
        self.assertEqual((original['state'],run['status']),('cancelled','cancelled'))
        self.assertEqual(run['last_error'],'execution_deadline_elapsed')
        self.assertEqual((original['execution_deadline'],original['result_deadline']),(first['execution_deadline'],first['result_deadline']))
        self.assertEqual(self.query('SELECT state FROM qq_official_native_schedule_sources')[0]['state'],'active')
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        self.assertEqual(self.calls,0)
        self.now+=600
        self.assertEqual(len(self.engine.materialize_due()),1)
        nextjob=self.claim_native();state,work=self.engine.begin(nextjob)
        self.assertEqual(state,'execute')
        self.assertNotEqual(nextjob.job_id,job.job_id)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_native_schedule_occurrences')),2)

    def test_reminder_at_execution_boundary_is_cancelled_without_enqueue(self):
        self.reminder();self.finish_foreground()
        original=self.query('SELECT * FROM qq_official_native_schedule_occurrences')[0]
        self.now=original['execution_deadline'];job=self.claim_native()
        self.assertEqual(self.engine.begin(job)[0],'expired')
        self.assertEqual(self.query('SELECT status FROM reminders')[0]['status'],'cancelled')
        self.assertEqual(self.query('SELECT state FROM qq_official_native_schedule_occurrences')[0]['state'],'cancelled')
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))

    def test_generated_result_after_execution_deadline_still_uses_original_delivery_deadline(self):
        first=self.queued_routine();job=self.claim_native()
        self.assertEqual(self.engine.begin(job)[0],'execute')
        self.engine.capture(job,MessageBody((TextNode(0,'already generated'),)))
        self.assertTrue(self.jobs.defer(job,delay_seconds=1,now=self.now))
        self.now=first['execution_deadline']+1;restored=self.claim_native()
        result=asyncio.run(self.engine.execute(restored))
        self.assertEqual(result['status'],'queued');self.assertEqual(self.calls,0)
        plan=self.query('SELECT * FROM delivery_plans')[0]
        self.assertEqual(plan['expires_at'],first['result_deadline'])
        self.assertEqual(self.query('SELECT status FROM routine_runs')[0]['status'],'enqueued')

    def test_interrupted_execution_remains_unknown_after_execution_deadline(self):
        first=self.queued_routine();job=self.claim_native()
        self.assertEqual(self.engine.begin(job)[0],'execute')
        self.assertTrue(self.jobs.defer(job,delay_seconds=1,now=self.now))
        self.now=first['execution_deadline']+1;restored=self.claim_native()
        self.assertEqual(self.engine.begin(restored)[0],'needs_attention')
        self.assertEqual(self.query('SELECT state FROM qq_official_native_schedule_occurrences')[0]['state'],'needs_attention')
        self.assertEqual(self.query('SELECT status FROM routine_runs')[0]['status'],'failed')
        self.assertEqual(len(self.query('SELECT * FROM routine_budget_reservations')),1)
        self.assertFalse(self.query('SELECT * FROM delivery_plans'));self.assertEqual(self.calls,0)
