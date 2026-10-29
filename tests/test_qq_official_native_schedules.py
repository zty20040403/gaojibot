import asyncio
from dataclasses import replace
from unittest.mock import patch

from src.bot_storage.database import DatabaseError
from src.plugins.ai_chat.adapters.qq_official.outbox_types import SendReservation
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.credentials import Credentials
from src.qq_official.token import TokenManager
from src.qq_official.http import RestClient
from src.plugins.ai_chat.adapters.qq_official.native_schedules import NativeScheduleDenied,digest


from tests.qq_official_native_schedule_fixture import NativeFixture

class NativeTests(NativeFixture):
    def test_reminder_source_job_atomic_and_idempotent(self):
        first=self.reminder();self.assertEqual(first,self.reminder())
        self.assertEqual(len(self.query('SELECT * FROM reminders')),1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_native_schedule_occurrences')),1)
        original=self.jobs.enqueue
        def fail(**kwargs):original(**kwargs);raise RuntimeError('TEST_ROLLBACK')
        with patch.object(self.jobs,'enqueue',fail),self.assertRaises(RuntimeError):self.reminder('rollback')
        self.assertEqual(len(self.query('SELECT * FROM reminders')),1)

    def test_missing_identity_does_not_substitute_appid(self):
        self.query('UPDATE qq_official_accounts SET bot_native_id=NULL,bot_identity_source=NULL,bot_identity_verified_at=NULL,bot_identity_contract=NULL,bot_identity_revision=0')
        with self.assertRaisesRegex(NativeScheduleDenied,'verified_bot_identity_required'):self.routine()
        self.assertFalse(self.query('SELECT * FROM agent_routines'))

    def test_routine_explicit_update_cas_and_actual_bot(self):
        item=self.routine()
        self.assertEqual(self.query('SELECT bot_id FROM agent_routines')[0]['bot_id'],'TEST_REAL_BOT')
        self.assertEqual(self.routine('same-name').status,'conflict')
        self.assertEqual(self.routine('bad-id',routine_id=item.identifier+1,expected_version=1).status,'not_found')
        self.assertEqual(self.routine('bad-version',routine_id=item.identifier,expected_version=99).status,'conflict')
        updated=self.routine('edit',routine_id=item.identifier,expected_version=1,prompt='new contract')
        self.assertEqual((updated.status,updated.revision),('updated',2))
        self.assertEqual(len(self.query("SELECT * FROM qq_official_native_schedule_sources WHERE state='active'")),1)

    def test_source_inbound_mismatch_cannot_create_or_cancel(self):
        item=self.reminder()
        wrong=replace(self.source,inbound=replace(self.source.inbound,native_message_id='OTHER'))
        with self.assertRaises(NativeScheduleDenied):self.engine.cancel(wrong,kind='reminder',identifier=item.identifier)
        self.assertEqual(self.query('SELECT status FROM reminders')[0]['status'],'pending')

    def test_policy_change_and_rebind_stop_later_work(self):
        self.reminder();self.finish_foreground();self.now+=10;job=self.claim_native()
        self.policy.configuration=replace(self.policy.configuration,revision=2)
        with self.assertRaisesRegex(NativeScheduleDenied,'schedule_policy_changed'):self.engine.begin(job)
        self.policy.configuration=replace(self.policy.configuration,revision=1)
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1')
        with self.assertRaisesRegex(NativeScheduleDenied,'schedule_source_changed'):self.engine.begin(job)
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))

    def test_routine_contract_survives_completed_foreground_and_original_600_seconds(self):
        self.routine();self.finish_foreground();self.now+=660
        ids=self.engine.materialize_due();self.assertEqual(len(ids),1)
        job=self.claim_native();state,work=self.engine.begin(job)
        self.assertEqual(state,'execute');self.assertEqual(work.actor,self.actor)
        self.assertLess(work.execution_deadline,work.result_deadline)
        self.assertGreater(self.query('SELECT authorization_until FROM qq_official_native_schedule_sources')[0][0],self.now+30000000)

    def test_user_explicit_contract_not_silently_shortened(self):
        with self.assertRaisesRegex(NativeScheduleDenied,'authorization_contract_exceeds_deployment'):
            self.routine(authorization_until=self.now+366*86400)
        self.assertFalse(self.query('SELECT * FROM agent_routines'))

    def test_generated_recovery_only_enqueues_without_model_repeat(self):
        self.routine();self.finish_foreground();self.now+=60;self.engine.materialize_due();job=self.claim_native()
        state,work=self.engine.begin(job);self.assertEqual(state,'execute')
        self.engine.capture(job,MessageBody((TextNode(0,'frozen'),)))
        self.assertTrue(self.jobs.defer(job,delay_seconds=1,now=self.now))
        self.now+=1;nextjob=self.claim_native()
        result=asyncio.run(self.engine.execute(nextjob))
        self.assertEqual(result['status'],'queued');self.assertEqual(self.calls,0)
        self.assertEqual(len(self.query('SELECT * FROM delivery_plans')),1)
        self.assertEqual(self.engine.enqueue(nextjob),result['plan_id'])

    def test_running_recovery_and_old_job_result_are_not_replayed(self):
        self.routine();self.finish_foreground();self.now+=60;self.engine.materialize_due();old=self.claim_native()
        self.assertEqual(self.engine.begin(old)[0],'execute')
        self.assertTrue(self.jobs.defer(old,delay_seconds=1,now=self.now));self.now+=1;new=self.claim_native()
        with self.assertRaises(NativeScheduleDenied):self.engine.capture(old,MessageBody((TextNode(0,'late'),)))
        self.assertEqual(self.engine.begin(new)[0],'needs_attention')
        self.assertEqual(len(self.query('SELECT * FROM routine_budget_reservations')),1)
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))

    def test_cancel_exact_id_and_actor_leaves_other_pending(self):
        first=self.reminder();second=self.reminder('second')
        self.assertEqual(self.engine.cancel(self.source,kind='reminder',identifier=99999).status,'not_found')
        self.assertEqual(self.engine.cancel(self.source,kind='reminder',identifier=first.identifier).status,'cancelled')
        self.assertEqual(self.query('SELECT status FROM reminders WHERE reminder_id=?',(second.identifier,))[0][0],'pending')

    def test_no_policy_or_disabled_policy_does_not_create_schedule(self):
        self.engine.policy=None
        with self.assertRaisesRegex(NativeScheduleDenied,'schedule_policy_unavailable'):self.reminder()
        self.assertFalse(self.query('SELECT * FROM reminders'))

    def test_real_routine_model_and_m3_receipt_then_no_duplicate(self):
        self.routine();self.finish_foreground();self.now+=60;self.engine.materialize_due();job=self.claim_native()
        result=asyncio.run(self.engine.execute(job));self.assertEqual(result['status'],'queued');self.assertEqual(self.calls,1)
        self.window(source='TEST_FRESH')
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(result['plan_id'],))[0][0]
        reservation=self.outbox.prepare(self.token,delivery,LocalRatePermit(self.account.key,self.endpoint.key,'group','passive','unverified',float(self.now)))
        self.assertIsInstance(reservation,SendReservation);self.assertTrue(self.outbox.mark_dispatching(self.token,reservation))
        payload=self.outbox.request_started(reservation)
        async def send():
            async with FakePlatform(clock=lambda:self.now) as fake:
                fake.add_window(kind='group',target=self.endpoint.native_conversation_id,message_id='TEST_FRESH')
                tokens=TokenManager(Credentials(fake.app_id,fake.app_secret),base_url=fake.base_url,allow_test_server=True)
                rest=RestClient(tokens,allow_test_server=True)
                try:
                    receipt=await rest.send_message('group',self.endpoint.native_conversation_id,payload)
                    self.assertEqual(len(fake.received),1)
                    return receipt
                finally:await rest.aclose();await tokens.aclose()
        receipt=asyncio.run(send())
        receipt_id=self.outbox.capture_receipt(reservation,receipt)
        outcome=self.outbox.finalize(self.token,receipt_id,ledger=self.ledger,journal=self.journal)
        self.assertEqual(outcome.status,'committed')
        self.assertEqual(asyncio.run(self.engine.execute(job))['status'],'handed_off')
        self.assertEqual(self.calls,1);self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')),1)
        self.policy.configuration=replace(self.policy.configuration,enabled=False)
        self.assertEqual(self.outbox.finalize(self.token,receipt_id,ledger=self.ledger).status,'committed')

    def test_immutable_source_deadline_actor_and_once_bound_plan(self):
        self.reminder();source=self.query('SELECT * FROM qq_official_native_schedule_sources')[0]
        for column,value in (('authorization_until',source['authorization_until']+1),('actor_key','OTHER'),('spec_json','{}'),('execution_id','OTHER')):
            with self.subTest(column=column),self.assertRaises(Exception):
                self.query(f'UPDATE qq_official_native_schedule_sources SET {column}=? WHERE source_id=?',(value,source['source_id']))
        self.finish_foreground();self.now+=10;job=self.claim_native();asyncio.run(self.engine.execute(job))
        occurrence=self.query('SELECT * FROM qq_official_native_schedule_occurrences')[0]
        for column,value in (('plan_id',None),('scheduled_at',self.now+1),('result_json','{}'),('job_id',self.job.job_id)):
            with self.subTest(column=column),self.assertRaises(Exception):
                self.query(f'UPDATE qq_official_native_schedule_occurrences SET {column}=? WHERE occurrence_id=?',(value,occurrence['occurrence_id']))

    def test_cross_actor_domain_and_cross_account_source_insert_rejected(self):
        self.reminder();source=dict(self.query('SELECT * FROM qq_official_native_schedule_sources')[0])
        for changes in ({'actor_key':'OTHER_ACTOR'},{'account_key':'OTHER_ACCOUNT'},{'endpoint_key':'OTHER_ENDPOINT'}):
            row={**source,**changes,'source_id':digest(str(changes))[:32],'request_key':digest(str(changes))}
            with self.subTest(changes=changes),self.assertRaises(Exception):
                self.query('INSERT INTO qq_official_native_schedule_sources ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))

    def test_begin_does_not_lock_job_before_source_and_parallel_cancel_finishes(self):
        import concurrent.futures
        import threading
        item=self.reminder();self.now+=10;job=self.claim_native()
        entered,release=threading.Event(),threading.Event()
        original=self.engine.current
        def slow_current(cursor,row,operation):
            result=original(cursor,row,operation)
            entered.set()
            if not release.wait(3):raise AssertionError('TEST_BARRIER_TIMEOUT')
            return result
        with patch.object(self.engine,'current',slow_current),concurrent.futures.ThreadPoolExecutor(2) as pool:
            beginning=pool.submit(self.engine.begin,job)
            try:
                self.assertTrue(entered.wait(3))
                # Independent real transaction can still acquire the exact job
                # row while begin is at the source/route boundary.
                with self.tx() as cursor:
                    cursor.execute('SELECT job_id FROM durable_jobs WHERE job_id=? FOR UPDATE NOWAIT',(job.job_id,))
                cancelling=pool.submit(self.engine.cancel,self.source,kind='reminder',identifier=item.identifier)
            finally:release.set()
            self.assertEqual(beginning.result(timeout=5)[0],'result_ready')
            self.assertEqual(cancelling.result(timeout=5).status,'cancelled')
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))

    def test_original_authorization_expiry_terminal_cleanup_without_new_route_grant(self):
        self.reminder();self.finish_foreground();self.now+=10;job=self.claim_native()
        # A late claim uses its real lease; the source's original contract is
        # not edited to manufacture an expiration or grant renewal.
        self.now=self.policy.configuration.authorization_until
        self.jobs.defer(job,delay_seconds=0,now=self.now-1)
        # Original job lease is already expired: actual store recovery/claim.
        current=self.claim_native()
        self.assertEqual(self.engine.begin(current)[0],'expired')
        self.assertEqual(self.query('SELECT state FROM qq_official_native_schedule_occurrences')[0][0],'cancelled')
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))


    def test_multirevision_materialize_cancel_locks_entire_family_before_parent(self):
        import concurrent.futures
        import threading
        import psycopg
        first=self.routine()
        second=self.routine('edit-two',routine_id=first.identifier,expected_version=1,prompt='second original contract')
        self.now+=60
        self.policy.configuration=replace(self.policy.configuration,revision=2)
        reached,release=threading.Event(),threading.Event()
        original=self.engine._cancel
        def stop_before_family_cancel(cursor,kind,identifier):
            if threading.current_thread().name.startswith('materializer'):
                reached.set()
                if not release.wait(5):raise AssertionError('TEST_BARRIER_TIMEOUT')
            return original(cursor,kind,identifier)
        with patch.object(self.engine,'_cancel',stop_before_family_cancel),concurrent.futures.ThreadPoolExecutor(1,thread_name_prefix='materializer') as a,concurrent.futures.ThreadPoolExecutor(1) as b:
            future=a.submit(self.engine.materialize_due)
            try:
                self.assertTrue(reached.wait(5))
                sources=self.query('SELECT source_id FROM qq_official_native_schedule_sources ORDER BY source_id')
                self.assertEqual(len(sources),2)
                for source in sources:
                    with self.assertRaises(DatabaseError) as caught:
                        with self.tx() as c:c.execute('SELECT source_id FROM qq_official_native_schedule_sources WHERE source_id=? FOR UPDATE NOWAIT',(source[0],))
                    self.assertIsInstance(caught.exception.__cause__,psycopg.errors.LockNotAvailable)
                cancel=b.submit(self.engine.cancel,self.source,kind='routine',identifier=first.identifier,expected_version=second.revision)
            finally:release.set()
            self.assertEqual(future.result(timeout=5),())
            self.assertEqual(cancel.result(timeout=5).status,'cancelled')
        self.assertFalse(self.query('SELECT * FROM routine_runs'))

    def test_expired_reminder_begin_cancel_uses_same_domain_order(self):
        import concurrent.futures
        import threading
        item=self.reminder()
        self.now+=10
        job=self.claim_native()
        # Result expiry is independent of the still-live creation execution in
        # this controlled race; never mutate a frozen source/occurrence deadline.
        original_now=self.engine.now
        endpoint=self.query('SELECT result_deadline FROM qq_official_native_schedule_occurrences')[0][0]
        self.query('UPDATE durable_jobs SET lease_until=? WHERE job_id=?',(endpoint+100,job.job_id))
        entered,release=threading.Event(),threading.Event()
        original=self.engine._lock_source_family
        def lock_family(cursor,kind,identifier):
            result=original(cursor,kind,identifier)
            if threading.current_thread().name.startswith('expirer'):
                entered.set()
                if not release.wait(5):raise AssertionError('TEST_BARRIER_TIMEOUT')
            return result
        def now():return endpoint if threading.current_thread().name.startswith('expirer') else original_now()
        with patch.object(self.engine,'_lock_source_family',lock_family),patch.object(self.engine,'now',now),concurrent.futures.ThreadPoolExecutor(1,thread_name_prefix='expirer') as a,concurrent.futures.ThreadPoolExecutor(1) as b:
            future=a.submit(self.engine.begin,job)
            try:
                self.assertTrue(entered.wait(5))
                with self.tx() as c:
                    c.execute('SELECT reminder_id FROM reminders WHERE reminder_id=? FOR UPDATE NOWAIT',(item.identifier,))
                    c.execute('SELECT job_id FROM durable_jobs WHERE job_id=? FOR UPDATE NOWAIT',(job.job_id,))
                cancel=b.submit(self.engine.cancel,self.source,kind='reminder',identifier=item.identifier)
            finally:release.set()
            self.assertEqual(future.result(timeout=5)[0],'expired')
            self.assertEqual(cancel.result(timeout=5).status,'cancelled')
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))

    def test_late_reminder_receipt_projects_sent_after_policy_revoked(self):
        self.reminder();self.finish_foreground();self.now+=10;job=self.claim_native()
        result=asyncio.run(self.engine.execute(job))
        self.assertEqual(self.query('SELECT status FROM reminders')[0][0],'sending')
        self.window(source='TEST_FRESH')
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(result['plan_id'],))[0][0]
        reservation=self.outbox.prepare(self.token,delivery,LocalRatePermit(self.account.key,self.endpoint.key,'group','passive','unverified',float(self.now)))
        self.assertTrue(self.outbox.mark_dispatching(self.token,reservation))
        self.outbox.request_started(reservation)
        receipt_id=self.outbox.capture_receipt(reservation,{'id':'TEST_REAL_RECEIPT_ONLY'})
        self.policy.configuration=replace(self.policy.configuration,enabled=False)
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked',route_epoch=route_epoch+1")
        self.assertEqual(self.outbox.finalize(self.token,receipt_id,ledger=self.ledger,journal=self.journal).status,'committed')
        self.engine.reconcile()
        self.assertEqual(self.query('SELECT status FROM reminders')[0][0],'sent')
        self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')),1)
