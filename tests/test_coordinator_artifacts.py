"""Real shared Coordinator/storage; only model, capture/validator and artifact port are fake."""
from pathlib import Path
import asyncio
from dataclasses import replace
import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests import qq_official_turn_fixture as fixtures
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence
from src.plugins.ai_chat.agent.artifact_delivery import ArtifactDeliveryView, ArtifactDeliveryError
from src.plugins.ai_chat.agent.file_outbox import attempt_file
from src.plugins.ai_chat.agent.progress import task_progress
from src.plugins.ai_chat.agent_loop import AgentLoopEvent
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.subagents import AgentExecutionHooks, SubAgentCoordinator, SubAgentStore, TaskStep


class CoordinatorArtifacts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixtures.ExecutionFixture('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store = SubAgentStore(self.f.database)
        self.addCleanup(self.store.close)
        self.profile = ModelProfile(name='test', provider='test', protocol='openai-chat', model='test',
            base_url='http://127.0.0.1', api_key_required=False)
        self.coordinator = SubAgentCoordinator(self.store,
            ModelCatalog({'test': self.profile}, default_profile='test'), logger=Mock())
        self.packet = ContextPacket(self.f.endpoint.scope.key, 'TEST_ARTIFACT_CONTEXT', None,
            self.f.message.canonical_message_id, '生成测试 PDF', endpoint=self.f.endpoint, requester_actor=self.f.actor)
        self.task = self.store.create_task(scope_key=self.packet.scope_key, conversation_id=self.packet.conversation_id,
            requester_user_id=None, trigger_message_id=self.packet.trigger_message_id, objective=self.packet.objective,
            max_parallelism=1, max_steps=3, endpoint=self.f.endpoint, requester_actor=self.f.actor)
        self.store.update_control(self.task.task_id, expected_version=0, dispatch={})
        self.store.set_task_state(self.task.task_id, 'running', plan={'contract': {'version': 1, 'delivery_required': True}})
        self.task = self.store.get(self.task.task_id)
        self.step = TaskStep('produce', 'coder', '生成测试 PDF', 'report.pdf')
        self.run = self.store.create_run(self.task.task_id, self.step, allowed_tools=[], model_profile='test')
        self.content = b'%PDF-1.7\nSYNTHETIC FIXTURE ONLY\n%%EOF'
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.artifact = {'handle': 's123abc:/workspace/report.pdf', 'name': 'report.pdf',
            'snapshot': self.digest, 'size': len(self.content), 'draft': True, 'draft_reason': 'MODEL FORGED DRAFT'}
        self.sequence = []
        async def capture(task_id, artifacts):
            self.assertEqual(task_id, self.task.task_id)
            self.sequence.append('capture')
            # Only this host fake capture is allowed to add authoritative snapshot and size.
            return [{**a, 'snapshot': self.digest, 'size': len(self.content)} for a in artifacts]
        async def validate(task_id, artifact):
            self.sequence.append('validate')
            self.assertEqual(artifact['snapshot'], hashlib.sha256(self.content).hexdigest())
            return {'ok': True, 'readable': True, 'checks': ['nonempty', 'sha256', 'format']}
        self.workspaces = SimpleNamespace(capture=capture, validate=validate,
            manager=SimpleNamespace(backend='oci'), prepare_delivery=AsyncMock(side_effect=AssertionError('legacy prepare')),
            executor=SimpleNamespace(send_file_content=AsyncMock(side_effect=AssertionError('legacy send'))))
        self.views = []
        async def register(request):
            self.sequence.append('register')
            self.requests.append(request)
            # Fake of root source-owned exporter: exact source and capture bytes checked here.
            self.assertEqual((request.endpoint, request.actor, request.task_id, request.run_id),
                (self.f.endpoint, self.f.actor, self.task.task_id, self.run.run_id))
            self.assertEqual((request.snapshot, request.size), (hashlib.sha256(self.content).hexdigest(), len(self.content)))
            self.assertFalse(hasattr(request, 'path'))
            self.assertFalse(hasattr(request, 'handle'))
            view = ArtifactDeliveryView(request.task_id, request.revision, request.endpoint, request.actor,
                'a'*32, request.snapshot, request.filename, request.size, request.draft, 'queued', self.f.now,
                draft_reason=request.draft_reason)
            self.views[:] = [view]
            return view
        self.requests = []
        self.register = register
        self.hooks = AgentExecutionHooks(workspaces=self.workspaces, artifact_delivery=register)
        self.store.official_file_projection = lambda task, revision: tuple(self.views)
        self.tools = {}

    async def model(self, *args, **kwargs):
        review = bool(self.sequence and self.sequence[-1] == 'validate')
        if review:
            self.sequence.append('independent_review')
            await kwargs['event_sink'](AgentLoopEvent('tool_finished', 1,
                tool_name='sandbox_exec', result='{"returncode":0}'))
            return json.dumps({'status': 'success', 'summary': '独立内容检查通过', 'artifacts': [],
                'metadata': {'artifact_reviews': [{'artifact_key': self.digest, 'status': 'passed', 'reason': 'checked fixture'}]}})
        self.sequence.append('produce')
        a = dict(self.artifact)
        a.pop('snapshot'); a.pop('size')
        return json.dumps({'status': 'success', 'summary': '已生成', 'artifacts': [a]})

    async def produce_and_validate(self):
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=self.model), \
             patch('src.plugins.ai_chat.subagents.tool_enabled', return_value=True):
            outcome = await self.coordinator._run_step(self.task, self.step, self.run, context=self.packet,
                upstream={}, selected_profile=self.profile, tools_by_name={}, execute_tool=AsyncMock(), hooks=self.hooks)
            self.assertEqual(outcome.state, 'success')
            validation = await self.coordinator._validate_workflow(self.task, {'produce': outcome}, context=self.packet,
                selected_profile=self.profile, tools_by_name={}, execute_tool=AsyncMock(), hooks=self.hooks,
                parent_trace=None, progress=None)
        self.assertEqual(validation['status'], 'passed', validation)
        return outcome, validation

    async def deliver(self, outcome, validation, hooks=None):
        with patch('src.plugins.ai_chat.subagents.tool_enabled', return_value=True), \
             patch.object(self.store, 'queue_file', side_effect=AssertionError('legacy queue')), \
             patch.object(self.store, 'begin_delivery', side_effect=AssertionError('legacy begin')):
            return await self.coordinator._deliver_requested_artifacts(self.task, {'produce': outcome},
                execute_tool=AsyncMock(side_effect=AssertionError('legacy tool')), delivered_artifacts=set(),
                progress=None, hooks=hooks or self.hooks, validation=validation)

    async def test_actual_coordinator_capture_independent_review_registration(self):
        outcome, validation = await self.produce_and_validate()
        rows = await self.deliver(outcome, validation)
        self.assertEqual(self.sequence, ['produce', 'capture', 'validate', 'independent_review', 'register'])
        self.assertFalse(self.requests[0].draft)
        self.assertEqual(self.requests[0].draft_reason, '')
        self.assertFalse(rows[0]['ok'])
        self.assertFalse(rows[0]['message_accepted'])
        self.assertEqual(rows[0]['state'], 'queued')
        self.assertEqual(self.f.query('SELECT * FROM subagent_deliveries'), [])

    async def test_actual_workflow_completion_does_not_require_file_receipt(self):
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=self.model), \
             patch('src.plugins.ai_chat.subagents.tool_enabled', return_value=True), \
             patch.object(self.coordinator, '_supervisor_text', new=AsyncMock(return_value='内容验收通过，文件等待投递')):
            await self.coordinator._execute_workflow(self.task, steps=[self.step], runs={'produce':self.run},
                context=self.packet, selected_profile=self.profile, tools_by_name={}, execute_tool=AsyncMock(),
                parent_trace=None, progress=None, hooks=self.hooks)
        task = self.store.get(self.task.task_id)
        self.assertEqual(task.status, 'completed')
        self.assertEqual(task.result['delivery_state'], 'pending')
        self.assertEqual(task.result['validation']['acceptance']['status'], 'passed')

    async def test_draft_uses_independent_verdict_and_safe_format_checks(self):
        outcome, validation = await self.produce_and_validate()
        validation['status'] = 'failed'
        validation['artifacts'][0].update(status='failed', reason='独立验收发现内容缺项')
        rows = await self.deliver(outcome, validation)
        self.assertTrue(self.requests[0].draft)
        self.assertEqual(self.requests[0].draft_reason, '独立验收发现内容缺项')
        self.assertTrue(rows[0]['filename'].startswith('草稿-未完成草稿-'))
        self.assertNotIn('MODEL', repr(self.requests[0]))
        self.requests.clear()
        validation['checks'][0]['checks'] = ['nonempty']
        rows = await self.deliver(outcome, validation)
        self.assertEqual(rows[0]['state'], 'validation_failed')
        self.assertEqual(self.requests, [])

    async def test_uncaptured_or_changed_snapshot_never_reaches_callback(self):
        outcome, validation = await self.produce_and_validate()
        outcome.result['artifacts'][0]['size'] += 1
        with self.assertRaisesRegex(ArtifactDeliveryError, 'capture_mismatch'):
            await self.deliver(outcome, validation)
        self.assertEqual(self.requests, [])

    async def test_missing_hook_never_enters_legacy_route(self):
        outcome, validation = await self.produce_and_validate()
        rows = await self.deliver(outcome, validation, AgentExecutionHooks(workspaces=self.workspaces))
        self.assertEqual(rows[0]['state'], 'unsupported')
        self.assertEqual(self.requests, [])

    async def test_callback_cancellation_is_not_swallowed(self):
        outcome, validation = await self.produce_and_validate()
        async def cancel(request):
            raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.deliver(outcome, validation, replace(self.hooks, artifact_delivery=cancel))
        self.assertEqual(self.f.query('SELECT * FROM subagent_deliveries'), [])

    async def test_callback_lease_loss_before_and_after_await(self):
        outcome, validation = await self.produce_and_validate()
        alive = True
        fence = JobFence(self.f.job.job_id, self.f.job.lease_owner, self.f.job.attempts, lambda: alive)
        token = active_job_fence.set(fence)
        try:
            alive = False
            with self.assertRaises(LeaseLost):
                await self.deliver(outcome, validation)
            self.assertEqual(self.requests, [])
            alive = True
            async def lose(request):
                nonlocal alive
                view = await self.register(request)
                alive = False
                return view
            with self.assertRaises(LeaseLost):
                await self.deliver(outcome, validation, replace(self.hooks, artifact_delivery=lose))
        finally:
            active_job_fence.reset(token)
        self.assertEqual(len(self.views), 1)  # Durable root registration can exist despite caller losing authority.
        self.assertFalse(any(p['phase']=='artifact_delivery' for p in self.store.checkpoints(self.task.task_id)))

    async def test_revision_change_during_registration_cannot_be_checkpointed(self):
        outcome, validation = await self.produce_and_validate()
        async def revised(request):
            value = await self.register(request)
            self.store.update_control(self.task.task_id, expected_version=1, revision=2)
            return value
        with self.assertRaisesRegex(ArtifactDeliveryError, 'source_changed'):
            await self.deliver(outcome, validation, replace(self.hooks, artifact_delivery=revised))
        self.assertFalse(any(p['phase']=='artifact_delivery' for p in self.store.checkpoints(self.task.task_id)))

    async def test_cross_source_projection_and_callback_result_rejected(self):
        outcome, validation = await self.produce_and_validate()
        async def wrong(request):
            view = await self.register(request)
            return replace(view, task_id=request.task_id+1)
        with self.assertRaisesRegex(ArtifactDeliveryError, 'source_mismatch'):
            await self.deliver(outcome, validation, replace(self.hooks, artifact_delivery=wrong))
        original = self.views[0]
        account = self.f.endpoint.account
        from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
        targets = [replace(self.f.endpoint, native_conversation_id='TEST_OTHER'),
            replace(self.f.endpoint, account=replace(account, app_id='TEST_OTHER_APP')),
            replace(self.f.endpoint, account=replace(account, environment='sandbox')),
            EndpointKey(account, 'private', self.f.actor.native_user_id)]
        for endpoint in targets:
            actor = ActorKey(endpoint.account,
                'group_member' if endpoint.kind=='group' else 'c2c_user',
                endpoint.native_conversation_id if endpoint.kind=='group' else '', self.f.actor.native_user_id)
            self.views[:] = [replace(original, endpoint=endpoint, actor=actor)]
            with self.subTest(endpoint=endpoint.kind), self.assertRaisesRegex(ArtifactDeliveryError, 'source_mismatch'):
                self.store.deliveries(self.task.task_id)

    async def test_projection_live_receipt_never_rewrites_task_acceptance(self):
        outcome, validation = await self.produce_and_validate()
        await self.deliver(outcome, validation)
        frozen={'validation': {'acceptance': {'status':'failed'}}, 'deliveries': [{'state':'queued'}]}
        self.store.set_task_state(self.task.task_id, 'partial', result=frozen)
        self.assertEqual(self.store.projected_result(self.task.task_id)['delivery_state'], 'pending')
        self.views[:] = [replace(self.views[0], state='message_accepted')]
        projection = self.store.projected_result(self.task.task_id)
        self.assertEqual(projection['delivery_state'], 'message_accepted')
        self.assertEqual(projection['validation']['acceptance']['status'], 'failed')
        self.assertEqual(self.store.get(self.task.task_id).status, 'partial')
        self.assertEqual(self.store.get(self.task.task_id).result, frozen)
        progress = task_progress(self.store.get(self.task.task_id), [], [], [], self.store.deliveries(self.task.task_id),
            SimpleNamespace(status='committed', updated_at=self.f.now), revision=1)
        self.assertTrue(progress['files_confirmed'])
        self.views[:] = [replace(self.views[0], state='delivery_unknown')]
        self.assertFalse(self.store.deliveries(self.task.task_id)[0]['payload']['ok'])

    async def test_text_task_empty_projection_is_not_unavailable(self):
        self.store.official_file_projection=None
        self.store.set_task_state(self.task.task_id, 'completed', plan={'contract': {'delivery_required':False}})
        self.assertEqual(self.store.deliveries(self.task.task_id), [])
        self.assertEqual(self.store.projected_result(self.task.task_id)['delivery_state'], 'not_requested')
        result=task_progress(self.store.get(self.task.task_id), [], [], [], [], None, revision=1)
        self.assertTrue(result['files_confirmed'])

    async def test_legacy_selectors_and_mutations_exclude_official_rows(self):
        self.store.update_control(self.task.task_id, expected_version=1, dispatch={'TEST':'metadata'})
        self.store.start_run(self.run.run_id)
        self.store.finish_run(self.run.run_id, 'succeeded', result={})
        payload={'artifact':self.artifact,'filename':'report.pdf','size':len(self.content),
            'handle':self.artifact['handle'],'attempts':1,'upload_started_at':1,'ok':False,'state':'queued'}
        with self.store._transaction() as cursor:
            cursor.execute("""INSERT INTO subagent_deliveries(task_id,revision,delivery_key,state,payload_json,updated_at)
                VALUES (?,1,?,'queued',?,1)""", (self.task.task_id,self.digest,json.dumps(payload)))
        delivery={'key':self.digest,'revision':1,'state':'queued','payload':payload,'updated_at':1}
        self.assertEqual(self.store.queued_file_tasks(), [])
        mutations=[lambda:self.store.queue_file(self.task.task_id,self.artifact,'report.pdf'),
            lambda:self.store.claim_file(self.task.task_id,delivery),
            lambda:self.store.defer_file_preparation(self.task.task_id,delivery,'x'),
            lambda:self.store.defer_file_availability(self.task.task_id,delivery,'x'),
            lambda:self.store.settle_file_attempt(self.task.task_id,delivery,payload,{'ok':True}),
            lambda:self.store.begin_delivery(self.task.task_id,self.digest,payload),
            lambda:self.store.finish_delivery(self.task.task_id,self.digest,'acknowledged',payload)]
        for mutate in mutations:
            with self.assertRaisesRegex(ValueError,'legacy_file_route_unavailable'):
                mutate()
        with self.assertRaisesRegex(ValueError,'legacy_file_route_unavailable'):
            await attempt_file(self.store,self.task.task_id,delivery,prepare=AsyncMock(side_effect=AssertionError),
                send=AsyncMock(side_effect=AssertionError),readiness=AsyncMock(side_effect=AssertionError))
        for state, selector in [('unknown',self.store.uncertain_deliveries),('rejected',self.store.rejected_file_tasks),
                                ('acknowledged',self.store.unsettled_file_receipts)]:
            with self.store._transaction() as cursor:
                cursor.execute('UPDATE subagent_deliveries SET state=? WHERE task_id=?',(state,self.task.task_id))
            self.assertEqual(selector(), [])
        self.store.set_task_state(self.task.task_id,'partial')
        self.assertIsNone(self.store.sync_file_receipt_summary(self.task.task_id,1))
        self.assertEqual(self.store.deliveries(self.task.task_id), [])


class ImportBoundary(unittest.TestCase):
    def test_clean_shared_hook_import_has_no_nonebot_or_legacy_executor(self):
        code = "import sys; sys.path[:0] = " + repr([str(Path(__file__).resolve().parents[1])]) + "; "
        code += "import src.plugins.ai_chat.subagents; "
        code += "assert not any(n=='nonebot' or n.startswith('nonebot.') or n.startswith('nonebot.adapters') for n in sys.modules); "
        code += "assert 'src.plugins.ai_chat.agent_tools' not in sys.modules"
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], env={**os.environ, 'PYTHON_DOTENV_DISABLED':'1'},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
