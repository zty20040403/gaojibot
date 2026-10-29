"""Real source rows, route journal and physical adapter; transport is synthetic."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from nonebot.adapters.onebot.v11 import Adapter, Bot

from src.plugins.ai_chat.adapters.onebot_migration_runtime import (
    DedicatedBaseline, OneBotMigrationRuntime, SourceJournal, job_digest,
)
from src.plugins.ai_chat.adapters.onebot_route import (
    PhysicalCall, RouteGuardedOneBotAdapter, TransportObservation,
    install_boundary, uninstall_boundary,
)
from src.plugins.ai_chat.adapters.onebot_route_context import (
    active_source_route, guarded_handler, source_context,
)
from src.plugins.ai_chat.agent.control import assert_job_owned, RouteAuthorityLost
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests.qq_official_migration_journal_fixture import JournalFixture


class QuietLogger:
    def error(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass


class WireBot(Bot):
    _calling_api_hook = set()
    _called_api_hook = set()


class OneBotRouteTests(JournalFixture):
    def setUp(self):
        super().setUp()
        self.sources = SourceJournal(self.routes, self.tx, clock=lambda: self.now)
        self.holder = SimpleNamespace(account=self.source.account, runtime_id=self.baseline.runtime_id)
        self.boundary = OneBotMigrationRuntime(sources=self.sources,
            baselines={self.source.account.key: self.holder}, canonical_scope=lambda event: self.source_scope)
        self.addCleanup(uninstall_boundary, self.boundary)
        def effect(cursor, proof, **kwargs):
            self.sources.require(cursor, proof, **{k:v for k,v in kwargs.items() if k != 'now'})
        self.ports.effect = effect

    def source_proof(self, *, reference='TEST_HANDLER', kind='handler', job_id=None, fingerprint='f'*64, inventory=False):
        with self.tx() as cursor:
            identifier = self.sources.create(cursor, baseline=self.holder, endpoint=self.source,
                scope=self.source_scope, actor=self.source_actor, kind=kind, ref=reference, revision=1,
                fingerprint=fingerprint, deadline=self.now+100, job_id=job_id, inventory=inventory)
            if job_id is not None:
                return identifier
            return self.sources.claim(cursor, identifier, baseline=self.holder)

    def call(self, *, target=None, api='send_group_msg'):
        return PhysicalCall(api, self.source.account.app_id, group_id=target or self.source.native_conversation_id)

    def test_forged_and_copied_proofs_do_not_grant_access(self):
        proof = self.source_proof()
        with self.assertRaises(PermissionError):
            with self.tx() as cursor:
                self.sources.require(cursor, replace(proof))
        self.assertTrue(self.sources.current(proof))
        self.assertNotIn(self.source_actor.native_user_id, repr(proof))

    def test_freeze_invalidates_captured_source_and_secondary_target(self):
        proof = self.source_proof()
        plan = self.plan()
        self.freeze(plan)
        for call in (self.call(), PhysicalCall('send_private_msg', self.source.account.app_id, user_id=99)):
            with self.assertRaises(MigrationDenied):
                self.boundary._begin(call, proof)
        self.assertEqual([], self.query('SELECT * FROM migration_dispatch_activities'))

    def test_intent_commits_before_wire_and_receipt_survives_freeze(self):
        proof = self.source_proof()
        plan = self.plan()
        permit = self.boundary._begin(self.call(), proof)
        self.assertEqual(proof.source_id, self.query('SELECT source_id FROM migration_dispatch_activities')[0][0])
        self.freeze(plan)
        self.boundary._observe(permit, TransportObservation(True, message_id='NATIVE_RECEIPT'))
        self.assertEqual('accepted', self.query('SELECT state FROM migration_dispatch_activities')[0][0])
        with self.assertRaises(MigrationDenied):
            self.boundary._begin(self.call(), proof)

    def test_success_without_native_receipt_stays_unknown(self):
        proof = self.source_proof()
        permit = self.boundary._begin(self.call(api='upload_group_file'), proof)
        self.boundary._observe(permit, TransportObservation(True))
        self.assertEqual('unknown', self.query('SELECT state FROM migration_dispatch_activities')[0][0])
        self.sources.finish(proof, uncertain=True)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.sources.claim(cursor, proof.source_id, baseline=self.holder)

    def test_cross_account_and_missing_source_denied(self):
        proof = self.source_proof()
        with self.assertRaises(PermissionError): self.boundary._begin(self.call(), None)
        with self.assertRaises(MigrationDenied):
            self.boundary._begin(PhysicalCall('send_group_msg','OTHER_BOT',group_id=700),proof)
        self.assertIsNone(self.boundary._begin(PhysicalCall('get_login_info','BOOTSTRAP'), None))

    def test_old_handler_replay_cannot_begin_twice(self):
        proof = self.source_proof()
        self.sources.finish(proof)
        with self.assertRaises(MigrationDenied): self.source_proof()
        self.assertEqual(1, self.query('SELECT COUNT(*) FROM migration_work_sources')[0][0])

    def test_pending_job_inventory_is_original_snapshot_and_generation(self):
        job, _ = self.jobs.enqueue(kind='test.onebot',idempotency_key='TEST_JOB',payload={'purpose':'TEST'},
                                   scope_key=self.source_scope.key, now=self.now)
        with self.tx() as cursor:
            fingerprint = job_digest(cursor.execute('SELECT * FROM durable_jobs WHERE job_id=?',(job.job_id,)).fetchone())
        source_id = self.source_proof(kind='job',job_id=job.job_id,fingerprint=fingerprint,inventory=True)
        claimed = self.jobs.claim_due('TEST_WORKER',now=self.now,kinds=('test.onebot',))[0]
        with self.tx() as cursor:
            proof = self.sources.claim(cursor,source_id,baseline=self.holder,job=claimed)
        self.assertTrue(self.sources.current(proof))
        self.jobs.defer(claimed,now=self.now)
        with self.assertRaises(MigrationDenied): self.sources.current(proof)
        self.query('UPDATE durable_jobs SET next_attempt_at=? WHERE job_id=?',(self.now,job.job_id))
        second = self.jobs.claim_due('TEST_WORKER',now=self.now,kinds=('test.onebot',))[0]
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.sources.claim(cursor,source_id,baseline=self.holder,job=second)

    def test_late_inventory_after_plan_is_denied(self):
        self.plan()
        job,_=self.jobs.enqueue(kind='test.onebot',idempotency_key='late',now=self.now)
        with self.tx() as cursor:
            fingerprint=job_digest(cursor.execute('SELECT * FROM durable_jobs WHERE job_id=?',(job.job_id,)).fetchone())
        with self.assertRaisesRegex(MigrationDenied,'inventory_after_migration'):
            self.source_proof(kind='job',job_id=job.job_id,fingerprint=fingerprint,inventory=True)

    def test_taskgroup_inherits_fence_and_freeze_stops_provider_boundary(self):
        proof=self.source_proof();plan=self.plan()
        async def run():
            with source_context(proof,lambda:self.sources.current(proof)):
                seen=[]
                async def child():
                    seen.append(active_source_route.get())
                    assert_job_owned()
                async with asyncio.TaskGroup() as group: group.create_task(child())
                self.freeze(plan)
                with self.assertRaises(RouteAuthorityLost): await child()
                self.assertEqual([proof,proof],seen)
            self.assertIsNone(active_source_route.get())
        asyncio.run(run())

    def test_actual_adapter_never_calls_wire_after_freeze(self):
        proof=self.source_proof();plan=self.plan();calls=[]
        adapter=object.__new__(RouteGuardedOneBotAdapter)
        bot=WireBot(adapter,self.source.account.app_id)
        async def wire(*args,**kwargs): calls.append(kwargs);return {'message_id':123}
        async def run():
            install_boundary(self.boundary)
            with patch.object(Adapter,'_call_api',wire), source_context(proof,lambda:self.sources.current(proof)):
                await bot.send_group_msg(group_id=700,message='TEST_PRIVATE_BODY')
                self.freeze(plan)
                with self.assertRaises(RouteAuthorityLost):
                    await bot.send_group_msg(group_id=700,message='DO_NOT_SEND')
        asyncio.run(run())
        self.assertEqual(1,len(calls))
        saved=str(self.query('SELECT receipt_json FROM migration_dispatch_activities'))
        self.assertNotIn('TEST_PRIVATE_BODY',saved)

    def test_actual_worker_context_installed_in_handler_task_and_no_reclaim(self):
        job,_=self.jobs.enqueue(kind='test.onebot',idempotency_key='worker',now=self.now)
        with self.tx() as cursor:
            fingerprint=job_digest(cursor.execute('SELECT * FROM durable_jobs WHERE job_id=?',(job.job_id,)).fetchone())
        self.source_proof(kind='job',job_id=job.job_id,fingerprint=fingerprint,inventory=True)
        plan=self.plan();calls=[]
        worker=DurableJobWorker(self.jobs,logger=QuietLogger(),worker_id='TEST_WORKER',source_context=self.boundary.job_context)
        async def handler(job):
            calls.append(active_source_route.get())
            self.freeze(plan)
            assert_job_owned()
        worker.register('test.onebot',handler)
        with patch('src.plugins.ai_chat.storage.jobs.time.time',return_value=self.now):
            asyncio.run(worker.run_once())
            self.assertEqual(0,asyncio.run(worker.run_once()))
        self.assertEqual(1,len(calls));self.assertIsNotNone(calls[0])
        self.assertEqual('failed',self.jobs.get(job.job_id).status)
        self.assertEqual('needs_attention',self.query('SELECT state FROM migration_work_sources')[0][0])

    def test_handler_wrapper_binds_actual_task_and_default_is_exact(self):
        calls=[]
        async def handler(event):calls.append((event,active_source_route.get()))
        event=SimpleNamespace(self_id=self.source.account.app_id,group_id=700,user_id=42,message_id=9,
                              dict=lambda:{'message_id':9,'message':'TEST_SECRET_BODY'})
        async def run():
            await guarded_handler(handler,SimpleNamespace(migration_routes=None))(event)
            from nonebot.internal.matcher import current_bot
            token=current_bot.set(SimpleNamespace(self_id=self.source.account.app_id))
            try:await guarded_handler(handler,SimpleNamespace(migration_routes=self.boundary))(event)
            finally:current_bot.reset(token)
        asyncio.run(run())
        self.assertIsNone(calls[0][1]);self.assertIsNotNone(calls[1][1])
        self.assertNotIn('TEST_SECRET_BODY',str(self.query('SELECT * FROM migration_work_sources')))

    def test_real_dedicated_baseline_is_exclusive_and_dies_with_session(self):
        account=AccountKey('onebot-v11',self.source.account.app_id+'1','test')
        first=DedicatedBaseline(self.database.dsn,account,deployment_digest='a'*64)
        second=DedicatedBaseline(self.database.dsn,account,deployment_digest='a'*64)
        self.addCleanup(first.close);self.addCleanup(second.close)
        self.assertTrue(first.acquire());self.assertFalse(second.acquire())
        with self.tx() as cursor:
            evidence=first.evidence(cursor,first,account=account,now=self.now)
            from src.plugins.ai_chat.adapters.qq_official.migration_routes import live_session
            self.assertTrue(live_session(cursor,evidence.backend_pid,evidence.advisory_key))
        first.close()
        self.assertTrue(second.acquire())

    def test_local_execute_receipt_is_verified_from_actual_lifecycle_row_after_freeze(self):
        import tempfile
        from pathlib import Path
        from src.plugins.ai_chat.projects import ProjectStore, ProjectAccess
        from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle
        from src.plugins.ai_chat.adapters.onebot_route_context import install_source_runtime, uninstall_source_runtime
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        store=ProjectStore(self.database,Path(temporary.name));self.addCleanup(store.close)
        principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST')
        scope=self.source_scope.with_actor(self.source_actor.native_user_id)
        access=ProjectAccess(scope,principal,member=True)
        project=store.create(access,'TEST retained project',now=self.now)
        lifecycle=ProjectLifecycle(store,self.jobs)
        lifecycle.request(access,project['project_id'],'wake',expected_version=project['version'],request_key='TEST_WAKE',now=self.now)
        pending=self.query("SELECT * FROM durable_jobs WHERE kind='project.lifecycle'")[0]
        source_id=self.source_proof(kind='job',job_id=pending['job_id'],fingerprint=job_digest(pending),inventory=True)
        job=self.jobs.claim_due('TEST_LIFECYCLE',kinds=('project.lifecycle',),now=self.now)[0]
        with self.tx() as cursor:proof=self.sources.claim(cursor,source_id,baseline=self.holder,job=job)
        step=lifecycle.next_step(job,now=self.now)
        with self.tx() as cursor:
            permit=self.sources.begin_effect(cursor,proof,scope=scope,domain='project.lifecycle',reference=f"{job.job_id}:{step['step_index']}")
        self.ports.effect_receipt=self.sources.effect_receipt
        self.freeze(self.plan())
        install_source_runtime(self.boundary);self.addCleanup(uninstall_source_runtime,self.boundary)
        evidence={**{k:step['arguments'][k] for k in ('project_id','generation','fence')},
                  'verified':True,'observation':'TEST actual helper create receipt',
                  'volume_id':'TEST_VOLUME','quota_bytes':step['arguments']['quota_bytes'],'quota_mode':'hard'}
        lifecycle.acknowledge(job,step,evidence,now=self.now)
        row=self.query('SELECT state,receipt_json FROM migration_dispatch_activities WHERE activity_id=?',(permit.activity_id,))[0]
        self.assertEqual('accepted',row['state']);self.assertIn('execution_receipt',row['receipt_json'])
        self.assertNotIn('native_id',row['receipt_json'])
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.routes.observe_effect(cursor,activity_id=permit.activity_id,source=('project.lifecycle','999:0'),now=self.now)

    def test_startup_without_factory_rejects_existing_managed_database(self):
        from src.plugins.ai_chat.onebot_runtime import OneBotRuntime
        from src.bot_storage import DatabaseCalls
        from unittest.mock import Mock
        from pathlib import Path
        self.plan()
        runtime=OneBotRuntime(SimpleNamespace(),state_dir=Path('/tmp'),cache_dir=Path('/tmp'),
                              project_root=Path('/tmp'),logger=Mock())
        calls=DatabaseCalls()
        async def run():
            try:
                with self.assertRaisesRegex(RuntimeError,'migration_service_factory'):
                    await runtime._start_migration_service(SimpleNamespace(database=self.database,db_call=calls))
                self.assertIsNone(runtime.migration_routes)
            finally:await calls.close()
        asyncio.run(run())

    def _domain_task(self, parent):
        from src.plugins.ai_chat.subagents import SubAgentStore
        from src.plugins.ai_chat.agent.contracts import ContextPacket
        store=SubAgentStore(self.database);self.addCleanup(store.close)
        store.source_recorder=self.boundary.capture_domain
        from tests.test_routine_tools import event
        original=event().model_copy(update={'self_id':int(self.source.account.app_id),'user_id':42,
            'group_id':700,'message_id':7,'time':self.now})
        dispatch={'bot_id':self.source.account.app_id,'profile':'TEST_PROFILE','deadline':self.now+90,
                  'event':original.model_dump(mode='json')}
        packet=ContextPacket(self.source_scope.key,self.source_scope.key+':user:42',42,None,'TEST task')
        with source_context(parent,lambda:self.sources.current(parent)):
            task=store.prepare_task(packet=packet,decision=None,dispatch=dispatch,max_parallelism=1,max_steps=1,now=self.now)
        return store,task

    def _derive_task(self, task):
        with self.tx() as cursor:
            job,_=self.jobs.enqueue(kind='subagent.workflow',idempotency_key=f'TEST_TASK_{task.task_id}',
                payload={'task_id':task.task_id,'revision':1},scope_key=self.source_scope.key,now=self.now,transaction_cursor=cursor)
            self.boundary.derive_job(cursor,job,kind='subagent',identifier=task.task_id,revision=1,now=self.now)
        return job

    def test_real_task_acceptance_and_later_job_share_original_source_epoch(self):
        parent=self.source_proof();store,task=self._domain_task(parent)
        self.sources.finish(parent)
        job=self._derive_task(task)
        row=self.query("SELECT * FROM migration_work_sources WHERE source_kind='subagent'")[0]
        self.assertEqual(parent.source_id,row['parent_source_id'])
        self.assertEqual(self.now+90,row['deadline_at'])
        claimed=self.jobs.claim_due('TEST_TASK_WORKER',kinds=('subagent.workflow',),now=self.now)[0]
        proof=self.boundary._job(claimed)
        with source_context(proof,lambda:self.sources.current(proof)):
            store.set_task_state(task.task_id,'running',now=self.now)
            self.assertTrue(self.sources.current(proof))
        self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?',(task.task_id,))
        with self.assertRaises(MigrationDenied):self.sources.current(proof)
        self.assertEqual(job.job_id,claimed.job_id)

    def test_freeze_between_task_acceptance_and_enqueue_rolls_back_job(self):
        parent=self.source_proof();_,task=self._domain_task(parent)
        self.sources.finish(parent);self.freeze(self.plan())
        with self.assertRaises(MigrationDenied):self._derive_task(task)
        self.assertEqual([],self.query("SELECT * FROM durable_jobs WHERE kind='subagent.workflow'"))
        self.assertEqual(0,self.query("SELECT route_epoch FROM migration_work_sources WHERE source_kind='subagent'")[0][0])

    def test_real_routine_materialization_preserves_parent_deadline(self):
        from src.plugins.ai_chat.quota import UsageStore
        from src.plugins.ai_chat.routines import RoutineStore
        usage=UsageStore(self.database);self.addCleanup(usage.close)
        store=RoutineStore(self.database,jobs=self.jobs,outbox=self.deliveries,usage=usage);self.addCleanup(store.close)
        store.source_recorder=self.boundary.capture_domain
        store.source_job_recorder=self.boundary.derive_job
        store.source_schedule_guard=self.boundary.guard_due_routines
        parent=self.source_proof()
        scope=self.source_scope.with_actor('42')
        with source_context(parent,lambda:self.sources.current(parent)):
            routine=store.set(scope,name='TEST routine',prompt='TEST prompt',cron='* * * * *',timezone='UTC',now=self.now)
        self.sources.finish(parent)
        self.now=routine.next_due_at
        runs=store.materialize_due(now=self.now)
        self.assertEqual(1,len(runs))
        child=self.query("SELECT * FROM migration_work_sources WHERE source_kind='job'")[0]
        self.assertEqual(parent.deadline_at,child['deadline_at'])
        self.freeze(self.plan())
        self.now+=60
        self.assertEqual([],store.materialize_due(now=self.now))

    def test_real_delivery_capture_then_claim_links_exact_existing_attempt(self):
        from src.plugins.ai_chat.message_ir import MessageBody,TextNode
        parent=self.source_proof();self.deliveries.source_recorder=self.boundary.capture_delivery
        with source_context(parent,lambda:self.sources.current(parent)):
            delivery,_=self.deliveries.enqueue(idempotency_key='TEST_DELIVERY',source_scope_key=self.source_scope.key,
                target_scope=self.source_scope,body=MessageBody((TextNode(0,'TEST body'),)),now=self.now)
        self.sources.finish(parent)
        claimed=self.deliveries.claim_due(now=self.now)[0]
        async def run():
            bot=SimpleNamespace(self_id=self.source.account.app_id)
            async with self.boundary.outgoing_context('delivery',delivery.delivery_id,bot):
                permit=self.boundary._begin(self.call(),active_source_route.get())
                self.boundary._observe(permit,TransportObservation(True,message_id='TEST_RECEIPT'))
        asyncio.run(run())
        activity=self.query('SELECT * FROM migration_dispatch_activities')[0]
        attempt=self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery.delivery_id,))[0]
        self.assertEqual(attempt['attempt_id'],activity['attempt_id'])
        self.assertEqual(claimed.attempts,attempt['attempt'])

    def test_managed_model_loop_without_any_source_is_denied(self):
        from src.plugins.ai_chat.adapters.onebot_route_context import install_source_runtime,uninstall_source_runtime
        from src.plugins.ai_chat.llm_gateway import _assert_model_job_owned
        install_source_runtime(self.boundary);self.addCleanup(uninstall_source_runtime,self.boundary)
        with self.assertRaisesRegex(RouteAuthorityLost,'model_source_unavailable'):_assert_model_job_owned()
        _assert_model_job_owned(inference=False)

    def _terminal_dispatcher(self):
        from src.plugins.ai_chat.agent.background import SubAgentDispatcher
        parent=self.source_proof()
        store,task=self._domain_task(parent)
        self.sources.finish(parent)
        job=self._derive_task(task)
        claimed=self.jobs.claim_due('TEST_TERMINAL',kinds=('subagent.workflow',),now=self.now)[0]
        proof=self.boundary._job(claimed)
        with source_context(proof,lambda:self.sources.current(proof)):
            store.set_task_state(task.task_id,'completed',result={'answer':'TEST persisted result'},now=self.now)
        self.sources.finish(proof)
        self.jobs.mark_succeeded(job.job_id,claimed.lease_owner,now=self.now,lease_generation=claimed.lease_generation)
        dispatcher=object.__new__(SubAgentDispatcher)
        dispatcher.store=store
        dispatcher.context=SimpleNamespace(delivery_store=self.deliveries,migration_service=self.boundary)
        self.deliveries.source_recorder=self.boundary.capture_delivery
        return dispatcher,task

    def test_completed_task_final_enqueue_retains_original_source_without_model_authority(self):
        from src.plugins.ai_chat.agent.control import active_route_fence
        dispatcher,task=self._terminal_dispatcher()
        with patch('src.plugins.ai_chat.delivery.time',SimpleNamespace(time=lambda:self.now)):
            delivery=dispatcher.enqueue_final(task.task_id)
            replay=dispatcher.enqueue_final(task.task_id)
        self.assertEqual(delivery.delivery_id,replay.delivery_id)
        self.assertIsNone(active_source_route.get())
        self.assertIsNone(active_route_fence.get())
        parent=self.query("SELECT * FROM migration_work_sources WHERE source_kind='subagent'")[0]
        child=self.query("SELECT * FROM migration_work_sources WHERE source_kind='delivery'")[0]
        self.assertEqual(child['parent_source_id'],parent['source_id'])
        self.assertEqual(child['deadline_at'],parent['deadline_at'])
        self.deliveries.claim_due(now=self.now)
        async def send():
            async with self.boundary.outgoing_context('delivery',delivery.delivery_id,SimpleNamespace(self_id=self.source.account.app_id)):
                self.boundary._begin(self.call(),active_source_route.get())
        asyncio.run(send())
        self.assertEqual(len(self.query('SELECT * FROM migration_dispatch_activities')),1)

    def test_terminal_result_freeze_or_original_expiry_does_not_issue_a_new_source(self):
        dispatcher,task=self._terminal_dispatcher()
        self.freeze(self.plan())
        with self.assertRaises(MigrationDenied):dispatcher.enqueue_final(task.task_id)
        self.assertEqual([],self.query('SELECT * FROM deliveries'))
        self.assertEqual([],self.query("SELECT * FROM migration_work_sources WHERE source_kind='delivery'"))

    def test_terminal_result_cannot_extend_deadline_on_reconciliation(self):
        dispatcher,task=self._terminal_dispatcher()
        self.now+=91
        with self.assertRaises(MigrationDenied):dispatcher.enqueue_final(task.task_id)
        self.assertEqual([],self.query('SELECT * FROM deliveries'))

    def test_unbound_terminal_delivery_and_changed_payload_cannot_borrow_result_context(self):
        from src.plugins.ai_chat.adapters.onebot_results import terminal_delivery_source
        from src.plugins.ai_chat.message_ir import MessageBody,TextNode
        dispatcher,task=self._terminal_dispatcher()
        body=MessageBody((TextNode(0,'TEST expected'),))
        kwargs=dict(idempotency_key=f'subagent-final:{task.task_id}:1',source_scope_key=self.source_scope.key,
                    target_scope=self.source_scope,body=body,reply_to_native_message_id='7',now=self.now)
        with self.assertRaises(MigrationDenied):self.deliveries.enqueue(**kwargs)
        with terminal_delivery_source(self.boundary,task.task_id,1,body=body,source_scope_key=self.source_scope.key,
                target_scope=self.source_scope,source_message=None,reply_to='7'):
            with self.assertRaises(MigrationDenied):
                self.deliveries.enqueue(**(kwargs|{'body':MessageBody((TextNode(0,'TEST changed'),))}))
        self.assertEqual([],self.query('SELECT * FROM deliveries'))
