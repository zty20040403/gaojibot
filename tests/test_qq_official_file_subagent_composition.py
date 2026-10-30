"""Actual Coordinator/registry/capture/file manifests under real account/job leases.

Only the model and the owned guest are fake. No QQ requests, listeners, or
legacy dispatcher are used; fixture classes contain no inherited test methods.
"""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.execution import EntryDecision, TaskContract
from src.plugins.ai_chat.agent_loop import AgentLoopEvent
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import body_from_json
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig, FileServices
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.subagent_composition import OfficialSubAgentService
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import job_kind, submit
from tests.qq_official_file_fixture import FileFixture
from tests.test_owned_sandbox import FakeManager


class ReviewGuest(FakeManager):
    """The real manager's snapshot import API, with bytes kept in the fake guest."""

    async def install_readonly_file(self, owner, sandbox_id, path, content):
        self.own(owner, sandbox_id)['files'][path] = bytes(content)
        self.event('install_readonly', owner, sandbox_id)
        return len(content)

    async def exec(self, owner, sandbox_id, command, timeout_seconds=None, packages=None):
        if command.startswith('inspect-captured '):
            path = command.removeprefix('inspect-captured ')
            data = self.own(owner, sandbox_id)['files'][path]
            if path.endswith('.pdf'):
                assert data.startswith(b'%PDF-') and b'TEST authored content' in data
            else:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    assert archive.read('main.py') == b'TEST authored content'
            self.event('inspect_captured', owner, sandbox_id)
            return SimpleNamespace(returncode=0, stdout='TEST captured content verified', stderr='', manifest=None)
        return await super().exec(owner, sandbox_id, command, timeout_seconds, packages)


class FileSubAgentCompositionTests(FileFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.tasks = SubAgentStore(self.database)
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(self.tasks.close)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.admission,
            pepper=b'TEST_FILE_SUBAGENT_PEPPER' * 2, clock=lambda: self.now)
        self.profile = ModelProfile(name='test', provider='test', protocol='openai-chat',
            model='test', base_url='http://127.0.0.1', api_key_required=False)
        self.catalog = ModelCatalog({'test': self.profile}, default_profile='test')
        self.coordinator = SubAgentCoordinator(self.tasks, self.catalog, logger=Mock(), max_parallelism=1)
        self.jobs.lease_seconds = 3600
        self.manager = ReviewGuest()
        temp = tempfile.TemporaryDirectory(prefix='qq-subagent-files-')
        self.addCleanup(temp.cleanup)
        self.private = Path(temp.name)
        self.sequence = []
        self.model_calls = 0
        self.file_service = None

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.runtime = SimpleNamespace(_calls=self.calls, deliveries=self.deliveries,
            jobs=self.jobs, outbox=self.outbox, sender=None, _stopping=False, _token=self.token,
            store=SimpleNamespace(clock=lambda: self.now, admitted=self.admission), file_service=None)
        self.app = SimpleNamespace(subagent_coordinator=self.coordinator, subagent_store=self.tasks,
            model_catalog=self.catalog, logger=Mock(),
            settings=replace(settings, search_enabled=False, subagent_max_parallelism=1),
            db_call=self.calls, long_term_memory=None, message_ledger=self.ledger,
            context_store=None, pin_store=None, semantic_recall=None,
            skill_registry=SimpleNamespace(get=lambda _name: None, list=lambda: []), self_source=Mock())

    async def asyncTearDown(self):
        if self.file_service is not None:
            await self.file_service.close()
        await self.calls.close()

    def configure_files(self, *, link=True):
        config = FileServiceConfig(self.private, self.manager, generation_enabled=True,
            route_authority=lambda *_: True, min_free_bytes=0,
            link_origin='https://files.example' if link else None,
            link_reachable=(lambda: True) if link else None)
        self.file_service = FileServices(self.runtime, self.token, None, config)
        self.runtime.file_service = self.file_service
        self.outbox.protected_payloads = self.file_service.payloads

    def submit_task(self, *, role='document', kind='pdf', tool_profile='official-files-v1'):
        self.kind = kind
        objective = '生成并交付 TEST ' + kind.upper()
        packet = ContextPacket(self.endpoint.scope.key, 'TEST_FILE_CONVERSATION', None,
            self.message.canonical_message_id, objective, endpoint=self.endpoint, requester_actor=self.actor)
        files = tool_profile == 'official-files-v1'
        decision = EntryDecision('delegate', 'execution' if files else 'explanation', 'TEST delegate', '',
            TaskContract(objective, ('result.' + kind,), (), ('核对实际内容',), files,
                ({'criterion_index': 0, 'kind': 'evidence'},), version=3 if files else 1),
            ({'id': 'produce', 'agent': role, 'objective': objective, 'deliverable': 'result.' + kind,
              'depends_on': [], 'required_tools': ['sandbox_create', 'sandbox_exec'] if files else []},))
        with self.tx() as cursor:
            self.task = submit(cursor, token=self.token, source_api=execution, source_handle=self.handle,
                source_job=self.job, tool_call_id='TEST_FILE_SUBMIT', tool_name='delegate_agent',
                packet=packet, decision=decision, profile='test', now=self.now,
                execution_deadline=self.now + 1200, result_wait_seconds=600, admission=self.admission,
                coordinator=self.coordinator, jobs=self.jobs, tool_profile=tool_profile)
        self.task_job = self.jobs.claim_due('TEST_FILES_WORKER', kinds=(job_kind(self.account),), now=self.now)[0]
        self.service = OfficialSubAgentService(self.runtime, self.token, self.app)

    async def invoke(self, execute_tool, event_sink, name, arguments):
        sequence = len(self.sequence) + 1
        token = active_tool_call_id.set('TEST_FILE_TOOL_' + str(sequence))
        try:
            raw = await execute_tool(name, arguments)
        finally:
            active_tool_call_id.reset(token)
        self.sequence.append(name)
        # Model-loop events faithfully contain the result of the actual tool.
        await event_sink(AgentLoopEvent('tool_finished', sequence, tool_name=name, arguments=arguments, result=raw))
        result = json.loads(raw)
        self.assertTrue(result.get('ok', True), result)
        return result

    async def file_model(self, prompt, messages, tools, execute_tool, **kwargs):
        self.model_calls += 1
        names = {item['function']['name'] for item in tools}
        self.assertIn('sandbox_create', names)
        self.assertNotIn('host_exec', names)
        self.assertNotIn('project_create', names)
        invoke = lambda name, args: self.invoke(execute_tool, kwargs['event_sink'], name, args)
        created = await invoke('sandbox_create', {'runtime': 'python'})
        sid = created['sandbox_id']
        if '独立验收人' in prompt:
            imported = await invoke('import_agent_artifact', {'step_id': 'produce', 'artifact_index': 0, 'sandbox_id': sid})
            checked = await invoke('sandbox_exec', {'sandbox_id': sid,
                'command': 'inspect-captured ' + imported['path'].removeprefix('/workspace/')})
            return json.dumps({'status': 'success', 'summary': 'TEST independently inspected bytes', 'artifacts': [],
                'findings': [], 'completed': [{'description': 'TEST inspected captured bytes',
                    'evidence_refs': [checked['_task_evidence']['ref']]}], 'authorization': [], 'next_verification': [],
                'metadata': {'artifact_reviews': [{'artifact_key': imported['sha256'], 'status': 'passed', 'reason': 'TEST content inspected'}],
                    'criterion_reviews': [{'criterion_index': 0, 'status': 'passed', 'reason': 'TEST inspected actual captured bytes',
                        'evidence_refs': [checked['_task_evidence']['ref']]}]}})
        await invoke('sandbox_write_file', {'sandbox_id': sid, 'path': 'input.md', 'content': 'TEST authored content'})
        generated = await invoke('sandbox_exec', {'sandbox_id': sid, 'command': 'generate-' + self.kind})
        denied = json.loads(await execute_tool('send_file_from_sandbox', {'sandbox_id': sid, 'path': 'result.' + self.kind}))
        self.assertFalse(denied['ok'])  # The model cannot bypass host review/register.
        return json.dumps({'status': 'success', 'summary': 'TEST generated bytes',
            'findings': [], 'completed': [{'description': 'TEST generated requested file',
                'evidence_refs': [generated['_task_evidence']['ref']]}], 'authorization': [], 'next_verification': [],
            'artifacts': [{'handle': sid + ':/workspace/result.' + self.kind, 'name': 'result.' + self.kind}]})

    async def run_model(self, model=None, *, worker=True):
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=model or self.file_model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek', new=AsyncMock(return_value='TEST generated and checked; delivery pending')), \
             patch('src.plugins.ai_chat.storage.jobs.time', SimpleNamespace(time=lambda: self.now)):
            if not worker:
                return await self.service.execute(self.task_job)
            worker = DurableJobWorker(self.jobs, logger=self.app.logger, database_calls=self.calls,
                worker_id=self.task_job.lease_owner)
            worker.register(job_kind(self.account), self.service.execute)
            await worker._execute(self.task_job)
            return self.jobs.get(self.task_job.job_id).result

    async def generation_case(self, role, kind):
        self.configure_files()
        self.submit_task(role=role, kind=kind)
        result = await self.run_model()
        self.assertEqual(self.jobs.get(self.task_job.job_id).status, 'succeeded')
        task = self.tasks.get(self.task.task_id)
        self.assertEqual(task.status, 'completed', task.result)
        self.assertIsNone(task.requester_user_id)
        self.assertEqual((task.endpoint, task.requester_actor), (self.endpoint, self.actor))
        self.assertEqual(self.model_calls, 2)
        self.assertIn('import_agent_artifact', self.sequence)
        self.assertTrue(any(call[0] == 'inspect_captured' for call in self.manager.calls))
        rows = self.query('SELECT * FROM qq_official_file_artifacts')
        self.assertEqual(len(rows), 1, task.result)
        artifact = rows[0]
        self.assertEqual((artifact['source_task_id'], artifact['requester_actor_key']), (task.task_id, self.actor.key))
        self.assertFalse(artifact['draft'])
        content = self.file_service.artifacts.blobs.read(artifact['sha256'], max_bytes=artifact['byte_size'])
        self.assertEqual(hashlib.sha256(content).hexdigest(), artifact['sha256'])
        if kind == 'pdf':
            self.assertTrue(content.startswith(b'%PDF-'))
            self.assertIn(b'TEST authored content', content)
        else:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                self.assertEqual(archive.read('main.py'), b'TEST authored content')
        self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries'), [{'state': 'stored'}])
        self.assertEqual(self.query('SELECT * FROM subagent_deliveries'), [])
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'), [])
        self.assertEqual(self.query('SELECT purpose FROM delivery_plans'), [{'purpose': 'background_result'}])
        self.assertTrue(result['plan_id'])
        self.assertEqual(self.tasks.deliveries(task.task_id)[0]['payload']['message_accepted'], False)
        self.assertEqual(self.manager.items, {})
        return task, artifact

    async def test_document_pdf_real_coordinator_registry_capture_and_durable_registration(self):
        await self.generation_case('document', 'pdf')

    async def test_coder_zip_real_coordinator_registry_capture_and_durable_registration(self):
        await self.generation_case('coder', 'zip')

    async def test_file_profile_without_services_defers_before_model_or_guest(self):
        self.submit_task()
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new_callable=AsyncMock) as model:
            with self.assertRaises(JobDeferred) as caught:
                await self.service.execute(self.task_job)
        self.assertEqual(str(caught.exception), 'official_task_files_unavailable')
        model.assert_not_awaited()
        self.assertEqual(self.manager.calls, [])

    async def test_unverified_native_without_link_does_not_open_generation(self):
        self.configure_files(link=False)
        self.submit_task()
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new_callable=AsyncMock) as model:
            with self.assertRaises(JobDeferred) as caught:
                await self.service.execute(self.task_job)
        self.assertEqual(str(caught.exception), 'official_task_files_unavailable')
        model.assert_not_awaited()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.query('SELECT * FROM qq_official_generation_capacity'), [])

    async def test_existing_text_profile_does_not_gain_file_tools_when_services_arrive(self):
        self.submit_task(role='analyst', tool_profile='official-text-v1')
        self.configure_files()
        self.service = OfficialSubAgentService(self.runtime, self.token, self.app)
        async def model(prompt, messages, tools, execute_tool, **kwargs):
            names = {item['function']['name'] for item in tools}
            self.assertNotIn('sandbox_create', names)
            self.assertEqual(json.loads(await execute_tool('sandbox_create', {}))['error'], 'tool_unavailable')
            return '{"status":"success","summary":"TEST text only","artifacts":[]}'
        await self.run_model(model)
        self.assertEqual(self.jobs.get(self.task_job.job_id).status, 'succeeded')
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'), [])

    async def test_changed_saved_profile_never_opens_tools_or_calls_model(self):
        self.configure_files()
        self.submit_task()
        with self.tx() as cursor:
            row = cursor.execute('SELECT dispatch_json FROM subagent_controls WHERE task_id=?', (self.task.task_id,)).fetchone()
            dispatch = json.loads(row['dispatch_json'])
            dispatch['tool_profile'] = 'official-text-v1'
            cursor.execute('UPDATE subagent_controls SET dispatch_json=? WHERE task_id=?', (json.dumps(dispatch), self.task.task_id))
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new_callable=AsyncMock) as model:
            with self.assertRaises(JobDeferred):
                await self.service.execute(self.task_job)
        model.assert_not_awaited()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'), [])

    async def test_schema_rejects_unknown_tool_profile(self):
        self.submit_task()
        from src.bot_storage.database import DatabaseError
        with self.assertRaises(DatabaseError), self.tx() as cursor:
            cursor.execute("UPDATE qq_official_subagent_dispatches SET tool_profile='official-unknown-v999' WHERE task_id=?", (self.task.task_id,))
        self.assertEqual(self.query('SELECT tool_profile FROM qq_official_subagent_dispatches'), [{'tool_profile': 'official-files-v1'}])

    async def test_final_summary_recovery_reads_current_file_state_without_rerunning_model(self):
        self.configure_files()
        self.submit_task()
        # Failure after the Coordinator completed/captured/registered but before
        # the text result is handed to M3. The actual job lease remains current.
        with patch.object(self.outbox, 'enqueue', side_effect=RuntimeError('TEST handoff unavailable')):
            with self.assertRaises(JobDeferred):
                await self.run_model(worker=False)
        task = self.tasks.get(self.task.task_id)
        self.assertEqual(task.status, 'completed')
        self.assertEqual(task.result['delivery_state'], 'pending')
        self.assertEqual(self.query('SELECT result_body_json FROM qq_official_subagent_dispatches'), [{'result_body_json': None}])
        self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries'), [{'state': 'stored'}])
        # A file worker/maintenance transition happens while the result is
        # waiting. Leave the task's historical completion JSON untouched.
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_file_deliveries SET state='expired',reason_code='file_deadline',updated_at=?", (self.now,))
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new_callable=AsyncMock) as model:
            result = await self.service.execute(self.task_job)
        model.assert_not_awaited()
        self.assertTrue(result['plan_id'])
        row = self.query('SELECT result_body_json FROM qq_official_subagent_dispatches')[0]
        body = body_from_json(row['result_body_json'])
        text = ''.join(node.text for node in body.nodes)
        self.assertIn('过期', text)
        self.assertNotIn('已发送', text)
        self.assertEqual(self.tasks.get(self.task.task_id).result, task.result)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_artifacts')), 1)
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'), [])
