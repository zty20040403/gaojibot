"""Official background composition restores authority and read-only tools from PG."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.adapters.qq_official.subagent_composition import OfficialSubAgentService
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import TEXT_TOOLS, job_kind
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from tests import test_qq_official_subagents as task_fixture


class OfficialSubAgentCompositionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = task_fixture.RuntimeTests(
            'test_submission_is_atomic_and_strictly_idempotent')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.task = self.fixture.submission()
        self.job = self.f.jobs.claim_due('TEST_COMPOSITION_WORKER',
            kinds=(job_kind(self.f.account),), now=self.f.now)[0]

    async def asyncSetUp(self):
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.runtime = SimpleNamespace(
            store=SimpleNamespace(clock=lambda: self.f.now, admitted=self.f.admission),
            deliveries=self.fixture.deliveries, _calls=self.fixture.calls,
            jobs=self.f.jobs, outbox=self.fixture.outbox, sender=None,
        )
        self.app = SimpleNamespace(
            subagent_coordinator=self.fixture.coordinator, subagent_store=self.fixture.store,
            model_catalog=self.fixture.coordinator.model_catalog, logger=Mock(),
            settings=replace(settings, search_enabled=True), db_call=self.fixture.calls,
            long_term_memory=None, message_ledger=self.f.ledger,
            context_store=None, pin_store=None, semantic_recall=None,
            skill_registry=SimpleNamespace(get=lambda _name: None, list=lambda: []),
            self_source=Mock(),
        )
        self.service = OfficialSubAgentService(self.runtime, self.f.token, self.app)

    def complete_task(self):
        self.fixture.store.set_task_state(self.task.task_id, 'completed',
                                         result={'answer': 'TEST restored result'})

    async def test_web_tool_falls_back_to_restored_objective(self):
        async def run(task_id, **kwargs):
            result = json.loads(await kwargs['execute_tool']('web_search', {}))
            self.assertTrue(result['ok'])
            self.complete_task()

        with patch.object(self.service.coordinator, 'resume', side_effect=run):
            with patch('src.plugins.ai_chat.tools.sources.search_web', new_callable=AsyncMock,
                       return_value=[]) as search:
                result = await self.service.execute(self.job)
        search.assert_awaited_once()
        self.assertEqual(search.await_args.args[0], self.task.objective)
        self.assertIsNotNone(result['plan_id'])

    async def test_restored_registry_has_only_read_tools_and_rejects_unadvertised_writes(self):
        async def run(task_id, **kwargs):
            names = {item['function']['name'] for item in kwargs['tools']}
            self.assertTrue(names <= TEXT_TOOLS)
            self.assertIn('context_search', names)
            self.assertFalse(names & {'memory_add', 'memory_remove', 'delegate_agent',
                                      'run_subagents', 'sandbox_exec', 'send_message'})
            for name in ('memory_add', 'memory_remove', 'host_inspect', 'send_message'):
                result = json.loads(await kwargs['execute_tool'](name, {'content': 'TEST_WRITE'}))
                self.assertEqual(result['error'], 'tool_unavailable')
            result = json.loads(await kwargs['execute_tool']('context_search',
                {'query': 'TEST', 'scope_key': 'another scope'}))
            self.assertFalse(result['ok'])
            self.complete_task()

        with patch.object(self.service.coordinator, 'resume', side_effect=run) as resume:
            await self.service.execute(self.job)
        resume.assert_awaited_once()
        self.assertIsNone(resume.await_args.kwargs['requester_user_id'])
        self.assertEqual(resume.await_args.kwargs['endpoint'], self.f.endpoint)
        self.assertEqual(resume.await_args.kwargs['requester_actor'], self.f.actor)
        self.assertEqual(self.f.query('SELECT count(*) AS n FROM delivery_attempts'), [{'n': 0}])

    async def test_removed_saved_profile_defers_without_default_profile_substitution(self):
        other = replace(self.fixture.profile, name='other', model='TEST_OTHER_MODEL')
        self.app.model_catalog = ModelCatalog({'other': other}, default_profile='other')
        restarted = OfficialSubAgentService(self.runtime, self.f.token, self.app)
        with patch.object(restarted.coordinator, 'resume', new_callable=AsyncMock) as resume:
            with self.assertRaises(JobDeferred) as caught:
                await restarted.execute(self.job)
        self.assertEqual(str(caught.exception), 'official_subagent_model_unavailable')
        resume.assert_not_awaited()
        self.assertEqual(self.fixture.store.get(self.task.task_id).status, 'queued')
        self.assertEqual(self.f.query('SELECT plan_id FROM qq_official_subagent_dispatches'), [{'plan_id': None}])

    async def test_stale_job_generation_is_rejected_before_restoring_model_or_tools(self):
        self.f.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=? RETURNING job_id',
                     (self.job.job_id,))
        with patch.object(self.service.coordinator, 'resume', new_callable=AsyncMock) as resume:
            with patch('src.plugins.ai_chat.adapters.qq_official.subagent_composition.neutral_registry_for') as registry:
                with self.assertRaises(JobDeferred) as caught:
                    await self.service.execute(self.job)
        self.assertGreater(caught.exception.delay_seconds, 0)
        resume.assert_not_awaited()
        registry.assert_not_called()
        self.assertEqual(self.f.query('SELECT plan_id FROM qq_official_subagent_dispatches'), [{'plan_id': None}])

    async def test_revoked_actual_route_is_rechecked_before_model_or_tools(self):
        self.f.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=? RETURNING endpoint_key",
                     (self.f.endpoint.key,))
        with patch.object(self.service.coordinator, 'resume', new_callable=AsyncMock) as resume:
            with patch('src.plugins.ai_chat.adapters.qq_official.subagent_composition.neutral_registry_for') as registry:
                result = await self.service.execute(self.job)
        self.assertEqual(result['status'], 'suppressed')
        self.assertEqual(self.fixture.store.get(self.task.task_id).status, 'cancelled')
        resume.assert_not_awaited()
        registry.assert_not_called()

    async def test_restart_reconciles_terminal_result_even_when_model_was_removed(self):
        self.complete_task()
        other = replace(self.fixture.profile, name='other', model='TEST_OTHER_MODEL')
        self.app.model_catalog = ModelCatalog({'other': other}, default_profile='other')
        self.runtime.sender = SimpleNamespace(wake=Mock())
        restarted = OfficialSubAgentService(self.runtime, self.f.token, self.app)
        with patch.object(restarted.coordinator, 'resume', new_callable=AsyncMock) as resume:
            await restarted.reconcile()
            await restarted.reconcile()
        resume.assert_not_awaited()
        self.runtime.sender.wake.assert_called_once()
        rows = self.f.query('SELECT plan_id,endpoint_key,purpose FROM delivery_plans')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['endpoint_key'], self.f.endpoint.key)
        self.assertEqual(rows[0]['purpose'], 'background_result')
        self.assertEqual(self.f.query('SELECT count(*) AS n FROM delivery_attempts'), [{'n': 0}])

    async def test_revoked_route_does_not_leave_immediately_reclaimable_job(self):
        self.f.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=? RETURNING endpoint_key",
                     (self.f.endpoint.key,))
        worker = DurableJobWorker(self.f.jobs, logger=self.app.logger,
            database_calls=self.fixture.calls, worker_id=self.job.lease_owner)
        worker.register(job_kind(self.f.account), self.service.execute)
        try:
            await worker._execute(self.job)
        except asyncio.CancelledError:
            pass
        job = self.f.jobs.get(self.job.job_id)
        self.assertFalse(job.status == 'pending' and job.next_attempt_at <= int(time.time()),
                         'Permanent route loss must not create an immediate uncharged retry loop')
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.result['status'], 'suppressed')
        self.assertEqual(self.fixture.store.get(self.task.task_id).status, 'cancelled')
        self.assertEqual(self.f.query('SELECT plan_id FROM qq_official_subagent_dispatches'), [{'plan_id': None}])

    async def test_account_loss_delays_then_expires_queue_without_changing_domain_state(self):
        before = self.f.query('SELECT * FROM subagent_tasks')
        manifest = self.f.query('SELECT * FROM qq_official_subagent_dispatches')
        self.f.account_lease.close()
        new_lease = self.f.lease()
        self.assertIsNotNone(new_lease.acquire())
        with self.assertRaises(JobDeferred) as caught:
            await self.service.execute(self.job)
        self.assertGreater(caught.exception.delay_seconds, 0)
        self.assertLessEqual(caught.exception.delay_seconds, 30)
        self.f.now = manifest[0]['execution_deadline_at']
        worker = DurableJobWorker(self.f.jobs, logger=self.app.logger,
            database_calls=self.fixture.calls, worker_id=self.job.lease_owner)
        worker.register(job_kind(self.f.account), self.service.execute)
        await worker._execute(self.job)
        job = self.f.jobs.get(self.job.job_id)
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.result['status'], 'needs_attention')
        self.assertEqual(job.result['reason'], 'subagent_expired_without_authority')
        self.assertEqual(self.f.query('SELECT * FROM subagent_tasks'), before)
        self.assertEqual(self.f.query('SELECT * FROM qq_official_subagent_dispatches'), manifest)

    async def test_direct_dispatcher_route_loss_settles_without_a_cancel_retry(self):
        self.f.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=? RETURNING endpoint_key",
                     (self.f.endpoint.key,))
        result = await self.service._dispatcher().execute(self.job)
        self.assertEqual(result['status'], 'suppressed')
        self.assertEqual(self.fixture.store.get(self.task.task_id).status, 'cancelled')

    async def test_external_cancellation_still_propagates_without_suppressing_task(self):
        with patch.object(self.service.coordinator, 'resume', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.execute(self.job)
        self.assertIsNone(self.f.query('SELECT suppressed_reason FROM qq_official_subagent_dispatches')[0]['suppressed_reason'])
        self.assertEqual(self.fixture.store.get(self.task.task_id).status, 'queued')

    async def test_expired_stale_job_cannot_acknowledge_new_job_generation(self):
        deadline = self.f.query('SELECT execution_deadline_at FROM qq_official_subagent_dispatches')[0]['execution_deadline_at']
        self.f.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=? RETURNING job_id',
                     (self.job.job_id,))
        self.f.now = deadline
        worker = DurableJobWorker(self.f.jobs, logger=self.app.logger,
            database_calls=self.fixture.calls, worker_id=self.job.lease_owner)
        worker.register(job_kind(self.f.account), self.service.execute)
        await worker._execute(self.job)
        current = self.f.jobs.get(self.job.job_id)
        self.assertEqual(current.status, 'running')
        self.assertEqual(current.lease_generation, self.job.lease_generation + 1)
        self.assertEqual(self.fixture.store.get(self.task.task_id).status, 'queued')
        self.assertIsNone(self.f.query('SELECT suppressed_reason FROM qq_official_subagent_dispatches')[0]['suppressed_reason'])

    async def test_route_loss_during_model_tools_terminates_before_tool_reads(self):
        async def run(task_id, **kwargs):
            self.f.query('UPDATE qq_official_endpoints SET generation=generation+1 WHERE endpoint_key=? RETURNING endpoint_key',
                         (self.f.endpoint.key,))
            await kwargs['execute_tool']('context_search', {'query': 'TEST'})
            self.fail('An obsolete route must not return a tool result')

        with patch.object(self.service.coordinator, 'resume', side_effect=run):
            with patch.object(self.f.ledger, 'search_in_scope') as read:
                outcome = await self.service.execute(self.job)
        self.assertEqual(outcome['status'], 'suppressed')
        self.assertEqual(self.fixture.store.get(self.task.task_id).status, 'cancelled')
        read.assert_not_called()
