"""Real route/source/job transactions; no external transports or models."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import psycopg

from src.plugins.ai_chat.adapters.onebot_migration_runtime import (
    OneBotMigrationRuntime, SourceJournal, job_digest,
)
from src.plugins.ai_chat.adapters.onebot_route import PhysicalCall, TransportObservation
from src.plugins.ai_chat.adapters.onebot_route_context import (
    require_model_source, source_context,
)
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.agent.control import RouteAuthorityLost
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routines import ROUTINE_JOB_KIND, RoutineStore
from src.plugins.ai_chat.routine_worker import RoutineDispatcher
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from tests.qq_official_migration_journal_fixture import JournalFixture
from tests.test_onebot_migration_route import QuietLogger


class SourceRecoveryTests(JournalFixture):
    def setUp(self):
        super().setUp()
        self.sources = SourceJournal(self.routes, self.tx, clock=lambda: self.now)
        self.holder = SimpleNamespace(account=self.source.account, runtime_id=self.baseline.runtime_id)
        self.boundary = OneBotMigrationRuntime(sources=self.sources,
            baselines={self.source.account.key: self.holder}, canonical_scope=lambda event: self.source_scope)
        self.ports.effect = lambda cursor, proof, **kw: self.sources.require(
            cursor, proof, **{k: v for k, v in kw.items() if k != 'now'}) and None

    def create_source(self, *, kind='handler', job=None):
        with self.tx() as cursor:
            fingerprint = job_digest(cursor.execute('SELECT * FROM durable_jobs WHERE job_id=?',
                (job.job_id,)).fetchone()) if job else 'f' * 64
            return self.sources.create(cursor, baseline=self.holder, endpoint=self.source_endpoint,
                scope=self.source_scope, actor=self.source_actor, kind=kind,
                ref=str(job.job_id) if job else 'handler', revision=1, fingerprint=fingerprint,
                deadline=self.now + 100, job_id=job.job_id if job else None)

    @property
    def source_endpoint(self):
        # JournalFixture uses `source` for the actual endpoint.
        return self.__dict__['source']

    def generic_job(self):
        job, _ = self.jobs.enqueue(kind='test.recovery', idempotency_key='TEST_RECOVERY',
            scope_key=self.source_scope.key, now=self.now)
        source_id = self.create_source(kind='job', job=job)
        return source_id, self.jobs.claim_due('TEST_WORKER', now=self.now, kinds=(job.kind,))[0]

    def claim(self, source_id, job):
        with self.tx() as cursor:
            return self.sources.claim(cursor, source_id, baseline=self.holder, job=job)

    def test_worker_defer_reclaims_same_source_with_new_lease_only(self):
        source_id, job = self.generic_job()
        proofs = []

        async def handler(actual):
            from src.plugins.ai_chat.adapters.onebot_route_context import active_source_route
            proofs.append(active_source_route.get())
            if len(proofs) == 1:
                raise JobDeferred('test temporarily unavailable', 0)
            return {'ok': True}

        async def run():
            worker = DurableJobWorker(self.jobs, logger=QuietLogger(), worker_id=job.lease_owner,
                source_context=self.boundary.job_context)
            worker.register(job.kind, handler)
            await worker._execute(job)
            self.assertEqual('ready', self.query('SELECT state FROM migration_work_sources WHERE source_id=?', (source_id,))[0][0])
            second = self.jobs.claim_due(job.lease_owner, now=self.now, kinds=(job.kind,))[0]
            self.assertGreater(second.lease_generation, job.lease_generation)
            await worker._execute(second)

        with patch('time.time', return_value=self.now):
            asyncio.run(run())
        self.assertEqual('succeeded', self.jobs.get(job.job_id).status)
        self.assertEqual(proofs[0].route_epoch, proofs[1].route_epoch)
        self.assertEqual(proofs[0].deadline_at, proofs[1].deadline_at)

    def test_reclaimed_source_rejects_old_proof_and_old_finally(self):
        source_id, job = self.generic_job()
        first = self.claim(source_id, job)
        self.sources.finish(first, deferred=True)
        self.jobs.defer(job, delay_seconds=0, now=self.now)
        second = self.jobs.claim_due(job.lease_owner, now=self.now, kinds=(job.kind,))[0]
        current = self.claim(source_id, second)
        with self.assertRaises(MigrationDenied):
            self.sources.current(first)
        self.sources.finish(first, uncertain=True)
        self.assertTrue(self.sources.current(current))
        self.assertEqual('executing', self.query('SELECT state FROM migration_work_sources WHERE source_id=?', (source_id,))[0][0])

    def test_deferred_ready_source_survives_runtime_restart_without_epoch_or_deadline_refresh(self):
        source_id, job = self.generic_job()
        first = self.claim(source_id, job)
        self.sources.finish(first, deferred=True)
        self.jobs.defer(job, delay_seconds=0, now=self.now)
        old_runtime = self.holder.runtime_id
        self.source_connection.close()
        connection = psycopg.connect(self.database.dsn, autocommit=True)
        self.addCleanup(connection.close)
        connection.execute('SELECT pg_advisory_lock(%s)', (self.baseline.advisory_key,))
        pid = connection.execute('SELECT pg_backend_pid()').fetchone()[0]
        self.baseline = replace(self.baseline, runtime_id='a' * 32, backend_pid=pid)
        self.holder.runtime_id = self.baseline.runtime_id
        with self.tx() as cursor:
            self.routes.register_baseline(cursor, account=self.source.account, source=self.baseline_source, now=self.now)
        current = self.claim(source_id, self.jobs.claim_due('NEW_WORKER', now=self.now, kinds=(job.kind,))[0])
        self.assertEqual((first.deadline_at, first.route_epoch), (current.deadline_at, current.route_epoch))
        self.assertEqual(old_runtime, self.query('SELECT baseline_runtime_id FROM migration_work_sources WHERE source_id=?', (source_id,))[0][0])

    def test_unknown_dispatch_never_becomes_retryable(self):
        source_id, job = self.generic_job()
        proof = self.claim(source_id, job)
        permit = self.boundary._begin(PhysicalCall('send_group_msg', self.source.account.app_id,
            group_id=self.source.native_conversation_id), proof)
        self.boundary._observe(permit, TransportObservation(True))
        self.sources.finish(proof, deferred=True)
        self.assertEqual('needs_attention', self.query('SELECT state FROM migration_work_sources WHERE source_id=?', (source_id,))[0][0])
        self.jobs.defer(job, delay_seconds=0, now=self.now)
        with self.assertRaises(MigrationDenied):
            self.claim(source_id, self.jobs.claim_due('NEW_WORKER', now=self.now, kinds=(job.kind,))[0])

    def test_cancel_and_generic_failure_remain_attention(self):
        source_id, job = self.generic_job()
        async def run():
            with self.assertRaises(asyncio.CancelledError):
                async with self.boundary.job_context(job):
                    raise asyncio.CancelledError()
        asyncio.run(run())
        self.assertEqual('needs_attention', self.query('SELECT state FROM migration_work_sources WHERE source_id=?', (source_id,))[0][0])
        self.jobs.defer(job, delay_seconds=0, now=self.now)
        with self.assertRaises(MigrationDenied):
            self.claim(source_id, self.jobs.claim_due('NEW_WORKER', now=self.now, kinds=(job.kind,))[0])

    def test_original_deadline_blocks_deferred_claim_even_without_migration(self):
        source_id, job = self.generic_job()
        proof = self.claim(source_id, job)
        self.sources.finish(proof, deferred=True)
        self.jobs.defer(job, delay_seconds=0, now=self.now)
        self.now = proof.deadline_at
        with self.assertRaisesRegex(MigrationDenied, 'work_source_not_ready'):
            self.claim(source_id, self.jobs.claim_due('NEW_WORKER', now=self.now, kinds=(job.kind,))[0])

    def test_original_deadline_and_freeze_block_deferred_claim(self):
        source_id, job = self.generic_job()
        proof = self.claim(source_id, job)
        self.sources.finish(proof, deferred=True)
        self.jobs.defer(job, delay_seconds=0, now=self.now)
        second = self.jobs.claim_due('NEW_WORKER', now=self.now, kinds=(job.kind,))[0]
        self.freeze(self.plan())
        with self.assertRaises(MigrationDenied):
            self.claim(source_id, second)
        self.now = proof.deadline_at
        with self.assertRaises(MigrationDenied):
            self.claim(source_id, second)

    def routine_result(self):
        usage = UsageStore(self.database)
        self.addCleanup(usage.close)
        store = RoutineStore(self.database, jobs=self.jobs, outbox=self.deliveries, usage=usage)
        self.addCleanup(store.close)
        store.source_recorder = self.boundary.capture_domain
        store.source_job_recorder = self.boundary.derive_job
        store.source_schedule_guard = self.boundary.guard_due_routines
        self.deliveries.source_recorder = self.boundary.capture_delivery
        parent_id = self.create_source()
        with self.tx() as cursor:
            proof = self.sources.claim(cursor, parent_id, baseline=self.holder)
        with source_context(proof, lambda: self.sources.current(proof)):
            spec = store.set(self.source_scope.with_actor('42'), name='TEST routine', prompt='TEST prompt',
                cron='* * * * *', timezone='UTC', now=self.now)
        self.sources.finish(proof)
        self.now = spec.next_due_at
        run = store.materialize_due(now=self.now)[0]
        job = self.jobs.claim_due('TEST_WORKER', now=self.now, kinds=(ROUTINE_JOB_KIND,))[0]
        source_id = self.query('SELECT source_id FROM migration_work_sources WHERE job_id=?', (job.job_id,))[0][0]
        proof = self.claim(source_id, job)
        with source_context(proof, lambda: self.sources.current(proof)):
            running = store.begin_attempt(job, now=self.now)
            store.save_result(job, running.attempt_id, MessageBody((TextNode(0, 'frozen result'),)), now=self.now)
        self.sources.finish(proof)
        self.jobs.mark_succeeded(job.job_id, job.lease_owner, lease_generation=job.lease_generation, now=self.now)
        self.assertEqual(1, store.reconcile_terminal_jobs(now=self.now))
        second = self.jobs.claim_due('NEW_WORKER', now=self.now, kinds=(ROUTINE_JOB_KIND,))[0]
        return store, run, source_id, second

    def test_generated_routine_recovery_queues_frozen_result_without_model(self):
        store, run, source_id, job = self.routine_result()
        proof = self.claim(source_id, job)
        async def db_call(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        app = SimpleNamespace(routine_store=store, db_call=db_call, job_worker=SimpleNamespace(register=lambda *a, **k: None),
            settings=SimpleNamespace(routine_timeout_seconds=600))
        dispatcher = RoutineDispatcher(SimpleNamespace(context=app))
        with source_context(proof, lambda: self.sources.current(proof)):
            with self.assertRaises(RouteAuthorityLost):
                require_model_source()
            with self.assertRaises(MigrationDenied):
                self.boundary._begin(PhysicalCall('send_group_msg', self.source.account.app_id,
                    group_id=self.source.native_conversation_id), proof)
            with patch('time.time', return_value=self.now), patch('src.plugins.ai_chat.routine_worker.get_bot', side_effect=AssertionError('no fresh execution')):
                result = asyncio.run(dispatcher.execute(job))
                again = asyncio.run(dispatcher.execute(job))
        self.assertTrue(result['recovered'])
        self.assertEqual(result, again)
        self.assertEqual(1, len(self.query('SELECT * FROM deliveries')))
        self.assertEqual(f'routine-final:{run.run_id}', self.deliveries.get(result['delivery_id']).idempotency_key)

    def test_result_recovery_rejects_changed_frozen_body(self):
        _, run, source_id, job = self.routine_result()
        proof = self.claim(source_id, job)
        self.query("UPDATE routine_runs SET body_json='{}' WHERE run_id=?", (run.run_id,))
        with self.assertRaises(MigrationDenied):
            self.sources.current(proof)

    def test_generated_result_never_restores_execution_permission_on_defer(self):
        _, _, source_id, job = self.routine_result()
        proof = self.claim(source_id, job)
        self.sources.finish(proof, deferred=True)
        self.jobs.defer(job, delay_seconds=0, now=self.now)
        current = self.claim(source_id, self.jobs.claim_due('NEXT', now=self.now, kinds=(job.kind,))[0])
        self.assertEqual(proof.result_digest, current.result_digest)
        with source_context(current, lambda: self.sources.current(current)):
            with self.assertRaises(RouteAuthorityLost):
                require_model_source()

    def test_result_recovery_cannot_enqueue_other_body_or_create_other_job(self):
        _, _, source_id, job = self.routine_result()
        proof = self.claim(source_id, job)
        with source_context(proof, lambda: self.sources.current(proof)):
            with self.assertRaisesRegex(MigrationDenied, 'result_delivery_mismatch'):
                self.deliveries.enqueue(idempotency_key='other-result', source_scope_key=self.source_scope.key,
                    target_scope=self.source_scope, body=MessageBody((TextNode(0, 'another result'),)), now=self.now)
        self.assertEqual([], self.query('SELECT * FROM deliveries'))
