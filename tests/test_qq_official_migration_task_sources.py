"""Real stored task/item selection, Coordinator recovery and M3 loopback.

The fixture helpers create genuine migration, dispatch, job and account rows.
No project engine is fabricated; absent project authority must remain closed
until that real domain is installed by the host.
"""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_qq_official_migration_subagents as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.migration_host_ports import MigrationPlanGuard
from src.plugins.ai_chat.adapters.qq_official.migration_native_sources import NativeSources
from src.plugins.ai_chat.adapters.qq_official.migration_task_sources import MigrationTaskSources
from src.plugins.ai_chat.adapters.qq_official.subagent_composition import OfficialSubAgentService
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import job_kind
from src.plugins.ai_chat.platform_types import BackgroundToolSource


class TaskSourceFixture(OutboxFixture, unittest.IsolatedAsyncioTestCase):
    owned = fixture.SubAgentContinuationTests.owned
    new_engine = fixture.SubAgentContinuationTests.new_engine
    ensure_current = fixture.SubAgentContinuationTests.ensure_current
    asyncTearDown = fixture.SubAgentContinuationTests.asyncTearDown
    rotate_mapping = fixture.SubAgentContinuationTests.rotate_mapping
    start = fixture.SubAgentContinuationTests.start
    freeze = fixture.SubAgentContinuationTests.freeze
    convert = fixture.SubAgentContinuationTests.convert
    complete = fixture.SubAgentContinuationTests.complete
    tick = fixture.SubAgentContinuationTests.tick
    source_task = fixture.SubAgentContinuationTests.source_task
    latest = fixture.SubAgentContinuationTests.latest

    async def asyncSetUp(self):
        await fixture.SubAgentContinuationTests.asyncSetUp(self)
        self.domains = {'subagent': self.engine}
        self.router = MigrationTaskSources(token=self.token, domains=self.domains, journal=self.migration)
        self.dispatcher.migration_authority = self.router
        self.outbox.source_guard = MigrationPlanGuard(native=NativeSources(self.routes),
            journal=self.migration, continuations=(self.router,))

    def active(self, *, completed=False):
        plan = self.start(self.source_task(completed=completed))
        item, binding = self.convert(plan)
        self.complete(plan, item)
        job = self.jobs.claim_due('TEST_ROUTED_TASK', kinds=(job_kind(self.account),), now=self.now)[0]
        return binding, job


class TaskSourceTests(TaskSourceFixture):
    async def test_real_coordinator_and_frozen_result_and_m3_use_same_routed_domain(self):
        await fixture.SubAgentContinuationTests.test_real_coordinator_resume_only_unstarted_step_then_m3(self)

    async def test_new_lease_does_not_replay_an_unknown_model_result(self):
        await fixture.SubAgentContinuationTests.test_lost_model_result_is_quarantined_on_new_job_lease(self)

    async def test_completed_result_recovery_routes_without_a_second_model(self):
        await fixture.SubAgentContinuationTests.test_enqueue_failure_retries_completed_bytes_without_new_model(self)

    async def test_restore_uses_actual_background_source_and_cancellation_rechecks(self):
        from src.plugins.ai_chat.config import settings
        binding, job = self.active()
        runtime = SimpleNamespace(store=SimpleNamespace(clock=lambda:self.now,admitted=self.engine.admission),
            deliveries=self.deliveries, jobs=self.jobs, outbox=self.outbox, _calls=self.calls)
        app = SimpleNamespace(subagent_coordinator=self.coordinator, subagent_store=self.store,
            model_catalog=self.coordinator.model_catalog, logger=Mock(), settings=settings)
        service = OfficialSubAgentService(runtime,self.token,app,migration_authority=self.router)
        source, objective = service._restore(job)
        self.assertIs(type(source),BackgroundToolSource)
        self.assertEqual(source.source_ref,f"task:{binding['target_task_id']}")
        self.assertEqual((source.endpoint,source.actor),(self.endpoint,self.actor))
        self.assertEqual(objective,self.old_task.objective)
        self.store.request_cancel(binding['target_task_id'])
        with self.assertRaises(LeaseLost):
            service._restore(job)

    async def test_missing_or_wrong_account_engine_never_falls_back_to_another_kind(self):
        binding, job = self.active()
        with self.tx() as cursor:
            self.domains['project_task'] = self.engine
            self.domains.pop('subagent')
            with self.assertRaises(LeaseLost):
                self.router.begin_job(cursor,job=job,now=self.now)
            self.domains['subagent'] = self.engine
            with patch.object(self.engine,'token',replace(self.token)):
                with self.assertRaises(LeaseLost):
                    self.router.tool_source(cursor,task_id=binding['target_task_id'],job=job,now=self.now)

    async def test_caller_profile_or_identity_cannot_change_persisted_source_selection(self):
        binding, job = self.active()
        with self.tx() as cursor:
            actual = dict(cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',
                (binding['target_task_id'],)).fetchone())
            task = dict(cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=?',
                (binding['target_task_id'],)).fetchone())
            for changes in ({'tool_profile':'official-projects-v1'}, {'account_key':'TEST_FOREIGN_ACCOUNT'},
                            {'job_id':job.job_id+1}, {'migration_item_id':'TEST_UNRELATED_ITEM'}):
                with self.subTest(fields=tuple(changes)), self.assertRaises(LeaseLost):
                    self.router.assert_current(cursor,binding={**actual,**changes},task=task,job=job,now=self.now)
            with self.assertRaises(LeaseLost):
                self.router.tool_source(cursor,task_id=task['task_id'],
                    job=replace(job,payload={'task_id':task['task_id']+1,'revision':1}),now=self.now)
            self.assertEqual(self.router.tool_source(cursor,task_id=task['task_id'],job=job,now=self.now).actor,self.actor)

    async def test_cancelled_final_cannot_reach_another_domain_or_transport(self):
        plan=self.start(self.source_task(completed=True));item,binding=self.convert(plan);self.complete(plan,item)
        await self.tick()
        saved=self.latest(item)
        self.query("UPDATE subagent_tasks SET status='cancelled' WHERE task_id=?",(binding['target_task_id'],))
        with self.tx() as cursor, self.assertRaises((LeaseLost,ValueError)):
            self.router.current_plan_source(cursor,saved['plan_id'],self.now)
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(saved['plan_id'],))[0][0]
        outcome=await self.sender.send_delivery(delivery)
        self.assertEqual((outcome.status,outcome.reason),('cancelled','authorization_revoked'))
        self.assertEqual(self.sender.reason,'ready')
        self.assertEqual(self.fake.received,[])


class NativeTaskRouteTests(unittest.IsolatedAsyncioTestCase):
    from tests.test_qq_official_subagents import RuntimeTests as _Fixture
    setUp = _Fixture.setUp
    asyncSetUp = _Fixture.asyncSetUp
    asyncTearDown = _Fixture.asyncTearDown
    submission = _Fixture.submission
    compute_301_with_real_worker = _Fixture.compute_301_with_real_worker

    def dispatcher(self):
        dispatcher=self._Fixture.dispatcher(self)
        # No migration exists for this genuine M4 execution. This sentinel
        # cannot validate a source; the original native authority stays active.
        dispatcher.migration_authority=MigrationTaskSources(token=self.f.token,domains={},journal=object())
        return dispatcher

    async def test_real_native_task_keeps_original_authority_with_no_migration_domains(self):
        await self.compute_301_with_real_worker()


class FileTaskRouteTests(OutboxFixture, unittest.IsolatedAsyncioTestCase):
    from tests.test_qq_official_migration_artifacts import MigrationArtifactTests as _Fixture
    owned = _Fixture.owned
    new_engine = _Fixture.new_engine
    ensure_current = _Fixture.ensure_current
    rotate_mapping = _Fixture.rotate_mapping
    start = _Fixture.start
    freeze = _Fixture.freeze
    convert = _Fixture.convert
    complete = _Fixture.complete
    tick = _Fixture.tick
    latest = _Fixture.latest
    invoke = _Fixture.invoke
    file_model = _Fixture.file_model
    source_task = _Fixture.source_task
    produce = _Fixture.produce
    asyncTearDown = _Fixture.asyncTearDown

    async def asyncSetUp(self):
        from src.plugins.ai_chat.adapters.qq_official.migration_services import _Artifacts
        await self._Fixture.asyncSetUp(self)
        self.router=MigrationTaskSources(token=self.token,domains={'subagent':self.engine},journal=self.migration)
        self.service.migration_authority=self.router
        self.account_session=SimpleNamespace(token=self.token,artifact_authority=self.authority,project_sessions=None,
            domains=self.router.domains)
        self.artifact_router=_Artifacts(SimpleNamespace(official={self.account.key:self.account_session}),None)
        self.ports.bound_artifact=self.artifact_router.validate_bound_artifact

    def native_handoff(self):
        result=self._Fixture.native_handoff(self)
        self.outbox.source_guard.continuations=(self.router,)
        return result

    async def test_real_file_finalization_uses_issued_account_artifact_source_and_type7(self):
        await self._Fixture.test_actual_coordinator_document_and_file_job_keep_null_native_union(self)
        from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
        with self.tx() as cursor:
            proof=self.migration._issue_item(cursor,item_id=self.item,operation='artifact',now=self.now)
            with self.assertRaises(MigrationDenied):
                self.artifact_router.validate_bound_artifact(cursor,replace(proof),self.artifact['artifact_id'],now=self.now)
            self.account_session.artifact_authority=None
            with self.assertRaises(MigrationDenied):
                self.artifact_router.validate_bound_artifact(cursor,proof,self.artifact['artifact_id'],now=self.now)
