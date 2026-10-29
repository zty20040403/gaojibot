"""Real current PostgreSQL stores; a READ ONLY transaction guards every preview."""
from dataclasses import replace
import hashlib
import unittest

import nonebot
nonebot.init()

from src.plugins.ai_chat.adapters.qq_official.migration_preview import MappingEvidence, PreviewDenied, Previewer, Selection, SourceRef
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.reminders import ReminderStore
from src.plugins.ai_chat.routines import RoutineStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentStore, TaskStep
from tests import test_project_store as project_fixture
from tests import test_project_file_outbox as file_fixture
from tests.qq_official_file_fixture import FileFixture
from src.bot_security.store import SecurityError, SecurityStore


class PreviewTests(unittest.TestCase):
    owned = project_fixture.ProjectStoreTests.owned
    new_store = project_fixture.ProjectStoreTests.new_store
    create = project_fixture.ProjectStoreTests.create
    snapshot = project_fixture.ProjectStoreTests.snapshot

    def setUp(self):
        # Reuse setup helpers, not the TestCase class or its test methods.
        project_fixture.ProjectStoreTests.setUp(self)
        self.tasks = self.owned(SubAgentStore(self.db))
        self.reminders = self.owned(ReminderStore(self.db))
        self.jobs = self.owned(DurableJobStore(self.db))
        self.outbox = self.owned(DeliveryStore(self.db, recover_interrupted=False))
        self.usage = self.owned(UsageStore(self.db))
        self.routines = self.owned(RoutineStore(self.db, jobs=self.jobs, outbox=self.outbox, usage=self.usage))
        source = EndpointKey(AccountKey('onebot-v11', '301', 'test'), 'group', '101')
        target = EndpointKey(AccountKey('qq-official', 'TEST_M5_PREVIEW', 'test'), 'group', 'TEST_GROUP')
        self.selection = Selection(source, self.scope, target)
        self.target_actor = ActorKey(target.account, 'group_member', target.native_conversation_id, 'TEST_ACTOR')
        self.routes = []
        self.project_allowed = True
        self.mapping_allowed = True
        self.preview = Previewer(route_guard=self.route, actor_mapping=self.mapping,
            project_guard=lambda *a: self.project_allowed)

    def route(self, cursor, selection, ref, facts):
        self.assertEqual(selection.source, self.selection.source)
        self.routes.append((ref, facts))
        return True  # Explicit trusted control-plane fixture, not a production default.

    def mapping(self, cursor, selection, ref, actor):
        if not self.mapping_allowed:
            return None
        return MappingEvidence('a' * 32, 3, actor, selection.target, self.target_actor)

    def inspect(self, ref, *, now=None, selection=None, preview=None):
        self.sql.rollback()
        cursor = self.sql.cursor()
        cursor.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        try:
            result = (preview or self.preview).preview(cursor, selection or self.selection, ref,
                now=self.now if now is None else now)
            self.assertEqual(cursor.execute('SHOW transaction_read_only').fetchone()[0], 'on')
            return result
        finally:
            self.sql.rollback()
            cursor.close()

    def reminder(self):
        item = self.reminders.create(self.scope, creator_native_user_id='201', creator_principal_id=self.principal,
            message='Finish the report.', scheduled_for=self.now + 60, now=self.now)
        return item, SourceRef('reminder', str(item.reminder_id))

    def task(self, *, dispatch=None):
        packet = ContextPacket(self.scope.key, 'TEST_CONVERSATION', 201, None, 'Prepare a report.')
        task = self.tasks.prepare_task(packet=packet, decision=None,
            dispatch=dispatch or {'bot_id': '301', 'deadline': self.now + 600, 'profile': 'TEST'},
            max_parallelism=2, max_steps=4)
        return task, SourceRef('subagent', str(task.task_id))

    def test_reminder_sent_is_weak_terminal_and_never_exported(self):
        item, ref = self.reminder()
        self.reminders.claim_due(now=self.now + 60)
        self.reminders.mark_sent(item.reminder_id, sent_at=self.now + 61)
        result = self.inspect(ref)
        self.assertEqual((result.disposition, result.reason), ('terminal', 'legacy_terminal_acceptance_unproven'))
        self.assertIsNone(result.export)
        self.assertTrue(any(facts['stored_bot_id'] is None for _, facts in self.routes))

    def test_expired_sending_and_recovered_pending_reminders_stay_unknown(self):
        item, ref = self.reminder()
        self.reminders.claim_due(now=self.now + 60, lease_seconds=10)
        before = self.sql.execute('SELECT status,attempts,lease_until FROM reminders').fetchone()
        result = self.inspect(ref, now=self.now + 1000)
        self.assertEqual(result.disposition, 'unknown')
        after = self.sql.execute('SELECT status,attempts,lease_until FROM reminders').fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.reminders.mark_failed(item.reminder_id, 'fixture error', now=self.now + 1000)
        self.assertEqual(self.inspect(ref).disposition, 'unknown')

    def test_pending_reminder_needs_explicit_mapping_and_preserves_original_schedule(self):
        item, ref = self.reminder()
        result = self.inspect(ref)
        self.assertEqual(result.disposition, 'unsent')
        self.assertEqual(result.export['scheduled_for'], item.scheduled_for)
        self.assertTrue(result.export['deadline_policy_required'])
        self.mapping_allowed = False
        self.assertEqual(self.inspect(ref).reason, 'explicit_actor_mapping_required')

    def test_route_scope_and_cross_account_mapping_fail_closed(self):
        _, ref = self.reminder()
        denied = Previewer(route_guard=lambda *a: False, actor_mapping=self.mapping, project_guard=lambda *a: True)
        with self.assertRaises(PreviewDenied):
            self.inspect(ref, preview=denied)
        bad_actor = ActorKey(AccountKey('qq-official', 'TEST_OTHER', 'test'), 'group_member', 'TEST_GROUP', 'TEST_ACTOR')
        bad = Previewer(route_guard=self.route, project_guard=lambda *a: True,
            actor_mapping=lambda c, s, r, a: MappingEvidence('a' * 32, 1, a, s.target, bad_actor))
        self.assertEqual(self.inspect(ref, preview=bad).reason, 'explicit_actor_mapping_required')
        other_source = EndpointKey(self.selection.source.account, 'group', '102')
        other = Selection(other_source, replace(self.scope, native_conversation_id='102'), self.selection.target)
        unrestricted_route = Previewer(route_guard=lambda *a: True, actor_mapping=self.mapping, project_guard=lambda *a: True)
        with self.assertRaises(PreviewDenied):
            self.inspect(ref, selection=other, preview=unrestricted_route)

    def test_selection_rejects_official_scope_with_same_native_id_in_other_account(self):
        source = self.selection.target
        other = EndpointKey(AccountKey('qq-official', 'TEST_OTHER_SCOPE_ACCOUNT', 'test'),
            source.kind, source.native_conversation_id)
        with self.assertRaises(PreviewDenied):
            Selection(source, other.scope, self.selection.target)

    def test_task_deadline_is_not_refilled_after_reopen_or_expiry(self):
        task, ref = self.task()
        first = self.inspect(ref)
        self.assertEqual(first.original_deadline, self.now + 600)
        self.assertEqual(first.export['execution_deadline'], self.now + 600)
        reopened = Previewer(route_guard=self.route, actor_mapping=self.mapping, project_guard=lambda *a: True)
        self.assertEqual(self.inspect(ref, now=self.now + 300, preview=reopened).safe_digest, first.safe_digest)
        expired = self.inspect(ref, now=self.now + 600)
        self.assertEqual((expired.disposition, expired.reason), ('terminal', 'execution_deadline_expired'))
        self.assertEqual(self.tasks.control(task.task_id)['dispatch']['deadline'], self.now + 600)

    def test_task_export_does_not_copy_event_credentials_context_or_http(self):
        secret = 'TEST_MUST_NOT_COPY'
        task, ref = self.task(dispatch={'bot_id': '301', 'deadline': self.now + 600,
            'event': {'raw_message': secret}, 'http_payload': {'msg_id': secret},
            'otp': secret, 'Authorization': secret, 'memory_context': secret})
        self.tasks.create_run(task.task_id, TaskStep('research', 'researcher', 'Read sources', 'Summary'),
            allowed_tools=['web_search'], model_profile='TEST')
        result = self.inspect(ref)
        self.assertEqual(result.disposition, 'unsent')
        self.assertNotIn(secret, result.export_json)
        for field in ('bot_id', 'Authorization', 'http_payload', 'msg_id', 'otp', 'memory_context'):
            self.assertNotIn(field, result.export)
        self.assertEqual(result.export['steps'][0]['required_tools'], ['web_search'])

    def test_successful_old_delivery_is_confirmed_and_never_reissued(self):
        task, ref = self.task()
        delivery, _ = self.outbox.enqueue(idempotency_key=f'subagent-final:{task.task_id}:1',
            source_scope_key=self.scope.key, target_scope=self.scope,
            body=MessageBody((TextNode(0, 'Already sent'),)), now=self.now)
        self.outbox.begin_direct_attempt(delivery.delivery_id, now=self.now)
        self.outbox.mark_committed(delivery.delivery_id, native_message_id='987654', now=self.now + 1)
        before = self.sql.execute('SELECT count(*) FROM deliveries').fetchone()[0]
        result = self.inspect(ref)
        self.assertEqual(result.disposition, 'confirmed')
        self.assertIsNone(result.export)
        self.assertEqual(self.sql.execute('SELECT count(*) FROM deliveries').fetchone()[0], before)
        self.assertEqual(self.outbox.get(delivery.delivery_id).target_platform, 'onebot-v11')

    def test_ambiguous_old_delivery_stays_unknown_even_if_task_complete(self):
        task, ref = self.task()
        self.tasks.set_task_state(task.task_id, 'completed', result={'answer': 'done'})
        delivery, _ = self.outbox.enqueue(idempotency_key=f'subagent-final:{task.task_id}:1',
            source_scope_key=self.scope.key, target_scope=self.scope,
            body=MessageBody((TextNode(0, 'maybe sent'),)), now=self.now)
        self.outbox.begin_direct_attempt(delivery.delivery_id, now=self.now)
        self.outbox.mark_ambiguous(delivery.delivery_id, 'fixture response loss', now=self.now + 1)
        self.assertEqual(self.inspect(ref).disposition, 'unknown')

    def test_two_finals_unknown_dominates_confirmed_independent_of_insert_order(self):
        for states in (('committed', 'ambiguous'), ('ambiguous', 'committed')):
            task, ref = self.task()
            for index, state in enumerate(states):
                delivery, _ = self.outbox.enqueue(
                    idempotency_key=f'subagent-final:{task.task_id}:1:project:TEST{index}',
                    source_scope_key=self.scope.key, target_scope=self.scope,
                    body=MessageBody((TextNode(0, 'TEST final'),)), now=self.now)
                self.outbox.begin_direct_attempt(delivery.delivery_id, now=self.now)
                if state == 'committed':
                    self.outbox.mark_committed(delivery.delivery_id, native_message_id='987654', now=self.now + 1)
                else:
                    self.outbox.mark_ambiguous(delivery.delivery_id, 'TEST unknown', now=self.now + 1)
            self.assertEqual(self.inspect(ref).disposition, 'unknown')

    def test_subagent_file_unknown_isolated_and_handle_not_exported(self):
        task, _ = self.task()
        digest = hashlib.sha256(b'TEST PDF bytes').hexdigest()
        artifact = {'snapshot': digest, 'size': 14, 'handle': 'TEST_OLD_NATIVE_HANDLE', 'name': 'report.pdf'}
        queued = self.tasks.queue_file(task.task_id, artifact, 'report.pdf')
        ref = SourceRef('file', f'subagent:{task.task_id}:1:{digest}')
        result = self.inspect(ref)
        self.assertEqual(result.disposition, 'unsent')
        self.assertNotIn('TEST_OLD_NATIVE_HANDLE', result.export_json)
        claim = self.tasks.claim_file(task.task_id, queued)
        self.tasks.settle_file_attempt(task.task_id, queued, claim, {'ok': False, 'error': 'TEST_UNKNOWN'})
        result = self.inspect(ref)
        self.assertEqual((result.disposition, result.export), ('unknown', None))

    def test_file_acknowledged_is_confirmed_even_after_parent_cancel(self):
        task, _ = self.task()
        digest = 'b' * 64
        queued = self.tasks.queue_file(task.task_id, {'snapshot': digest, 'size': 5, 'handle': 'OLD', 'name': 'x.pdf'}, 'x.pdf')
        claim = self.tasks.claim_file(task.task_id, queued)
        self.tasks.settle_file_attempt(task.task_id, queued, claim, {'ok': True, 'file_id': 'TEST_NATIVE'})
        self.tasks.request_cancel(task.task_id)
        result = self.inspect(SourceRef('file', f'subagent:{task.task_id}:1:{digest}'))
        self.assertEqual(result.disposition, 'confirmed')

    def routine(self):
        return self.routines.set(self.scope, name='Daily', prompt='Write summary', cron='* * * * *', now=self.now)

    def test_routine_schedule_and_generated_occurrence_export_without_dispatch(self):
        routine = self.routine()
        spec = self.inspect(SourceRef('routine', f'spec:{routine.routine_id}'))
        self.assertEqual(spec.disposition, 'unsent')
        run = self.routines.materialize_due(now=routine.next_due_at)[0]
        job = self.jobs.claim_due('TEST_WORKER', now=routine.next_due_at)[0]
        started = self.routines.begin_attempt(job, now=routine.next_due_at, timeout_seconds=30)
        self.routines.save_result(job, started.attempt_id, MessageBody((TextNode(0, 'Frozen summary'),)), now=routine.next_due_at + 1)
        generated = self.inspect(SourceRef('routine', 'run:' + run.run_id), now=routine.next_due_at + 100)
        self.assertEqual(generated.disposition, 'unsent')
        self.assertEqual(generated.export['result_text'], 'Frozen summary')
        self.assertEqual(generated.original_deadline, routine.next_due_at + 30)
        self.assertEqual(self.sql.execute('SELECT count(*) FROM deliveries').fetchone()[0], 0)

    def test_routine_effect_started_blocks_export(self):
        routine = self.routine()
        run = self.routines.materialize_due(now=routine.next_due_at)[0]
        job = self.jobs.claim_due('TEST_WORKER', now=routine.next_due_at)[0]
        started = self.routines.begin_attempt(job, now=routine.next_due_at)
        self.routines.claim_effect(job, started.attempt_id, 'send_file', {'path': 'TEST_SECRET'}, now=routine.next_due_at)
        result = self.inspect(SourceRef('routine', 'run:' + run.run_id), now=routine.next_due_at + 100)
        self.assertEqual(result.disposition, 'unknown')

    def project_task(self):
        pid = self.create()
        self.snapshot(pid)
        self.store.open(self.access, pid)
        binding = ProjectTaskStore(self.store, self.tasks)
        task = binding.prepare(self.access, pid,
            expected_version=self.store.get(self.access, pid)['version'], base_version=1,
            request_key=hashlib.sha256(b'TEST_PROJECT_TASK').hexdigest(),
            packet=ContextPacket(self.scope.key, 'TEST_PROJECT_CONVERSATION', 201, None, 'Update project'),
            decision=None, dispatch={'bot_id': '301', 'deadline': self.now + 600},
            max_parallelism=2, max_steps=4)
        return task, SourceRef('project_task', str(task.task_id))

    def test_project_task_keeps_immutable_baseline_and_requires_new_grant(self):
        task, ref = self.project_task()
        result = self.inspect(ref)
        self.assertEqual(result.disposition, 'unsent')
        binding = self.sql.execute('SELECT * FROM project_task_bindings WHERE task_id=?', (task.task_id,)).fetchone()
        self.assertEqual(result.export['manifest_sha256'], binding['manifest_sha256'])
        self.project_allowed = False
        self.assertEqual(self.inspect(ref).reason, 'current_project_grant_required')
        self.project_allowed = True
        self.sql.execute("UPDATE projects SET status='active' WHERE project_id=?", (task.project_id,))
        self.sql.commit()
        self.assertEqual(self.inspect(ref).reason, 'project_quiescent_evidence_required')

    def test_inventory_has_stable_pagination_and_no_domain_mutation(self):
        refs = []
        for _ in range(3):
            refs.append(self.reminder()[1])
        self.sql.rollback()
        cursor = self.sql.cursor()
        cursor.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        try:
            page = self.preview.inventory(cursor, self.selection, 'reminder', limit=2)
            tail = self.preview.inventory(cursor, self.selection, 'reminder', after=page[-1].key, limit=2)
            self.assertEqual(set(page + tail), set(refs))
        finally:
            self.sql.rollback()
            cursor.close()
        self.assertEqual(self.sql.execute('SELECT count(*) FROM durable_jobs').fetchone()[0], 0)

    def test_nonportable_cq_and_tool_revision_are_explicitly_unsupported(self):
        item, ref = self.reminder()
        self.sql.execute('UPDATE reminders SET message=? WHERE reminder_id=?', ('[CQ:reply,id=SECRET] hello', item.reminder_id))
        self.sql.commit()
        self.assertEqual(self.inspect(ref).reason, 'nonportable_text')
        task, ref = self.task()
        self.sql.execute('UPDATE subagent_controls SET revision=2 WHERE task_id=?', (task.task_id,))
        self.sql.commit()
        self.assertEqual(self.inspect(ref).reason, 'revision_actor_export_not_implemented')

    def test_rr_snapshot_does_not_authorize_return_after_session_revocation(self):
        security = self.owned(SecurityStore(self.db, b'TEST_PREVIEW_SECURITY' * 2, clock=lambda: self.now))
        security.bootstrap('preview_admin', 'TEST-preview-password-123', '12345678')
        session = security.login('preview_admin', 'TEST-preview-password-123', 'TEST_LOOPBACK')['session']
        current = security.session(session)  # Actual outer READ COMMITTED check.
        _, ref = self.reminder()
        self.sql.rollback()
        cursor = self.sql.cursor()
        cursor.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        try:
            self.assertIsNotNone(cursor.execute('SELECT 1 FROM admin_sessions WHERE session_hash=?',
                (current['session_hash'],)).fetchone())
            security.logout(session)  # A second actual connection commits revoke.
            self.assertIsNotNone(cursor.execute('SELECT 1 FROM admin_sessions WHERE session_hash=?',
                (current['session_hash'],)).fetchone())  # RR still sees old session.
            snapshot = self.preview.preview(cursor, self.selection, ref, now=self.now)
            self.assertEqual(snapshot.disposition, 'unsent')
        finally:
            self.sql.rollback()
            cursor.close()
        # The API must perform this second current check, not return the cached
        # snapshot's export because its old session happened to be visible.
        with self.assertRaises(SecurityError):
            security.session(session)



    def test_completed_task_final_text_is_frozen_and_changes_preview_digest(self):
        task, ref = self.task()
        self.tasks.set_task_state(task.task_id, 'completed', result={'answer':'Approved final.', 'secret':'DO_NOT_EXPORT'})
        first=self.inspect(ref)
        self.assertEqual(first.disposition,'unsent')
        self.assertEqual(first.export['frozen_final_text'],'Approved final.')
        self.assertEqual(first.export['source_task_status'],'completed')
        self.assertNotIn('DO_NOT_EXPORT',first.export_json)
        self.tasks.set_task_state(task.task_id,'completed',result={'answer':'Different final.'})
        self.assertNotEqual(first.safe_digest,self.inspect(ref).safe_digest)

    def test_pending_task_run_with_prior_model_attempt_is_unknown_without_tool_event(self):
        task,ref=self.task()
        run=self.tasks.create_run(task.task_id,TaskStep('research','researcher','Read sources','Summary'),
            allowed_tools=(),model_profile='TEST')
        self.sql.execute('UPDATE subagent_runs SET attempt=1 WHERE run_id=?',(run.run_id,))
        self.sql.commit()
        value=self.inspect(ref)
        self.assertEqual(value.disposition,'unknown')
        self.assertIsNone(value.export)


class ProjectFilePreviewTests(unittest.IsolatedAsyncioTestCase):
    call = file_fixture.ProjectFileOutboxTests.call
    queued = file_fixture.ProjectFileOutboxTests.queued

    async def asyncSetUp(self):
        await file_fixture.ProjectFileOutboxTests.asyncSetUp(self)
        source = EndpointKey(AccountKey('onebot-v11', '301', 'test'), 'group', '101')
        target = EndpointKey(AccountKey('qq-official', 'TEST_PROJECT_FILE_PREVIEW', 'test'), 'group', 'TEST_GROUP')
        self.selection = Selection(source, self.scope, target)
        actor = ActorKey(target.account, 'group_member', 'TEST_GROUP', 'TEST_ACTOR')
        self.preview = Previewer(route_guard=lambda *a: True, project_guard=lambda *a: True,
            actor_mapping=lambda c, s, r, a: MappingEvidence('a' * 32, 1, a, target, actor))

    def inspect(self, delivery):
        with self.deliveries.transaction() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            return self.preview.preview(cursor, self.selection, SourceRef('file', f'project:{delivery.delivery_id}'),
                now=1_900_000_000)

    async def test_real_project_file_cas_is_metadata_only_and_unresolved_upload_isolated(self):
        _, delivery = await self.queued(name='report.pdf', content='%PDF-1.4 TEST')
        result = self.inspect(delivery)
        self.assertEqual(result.disposition, 'unsent')
        self.assertEqual(result.export['origin'], 'project')
        self.assertTrue(result.export['bytes_and_source_revalidation_required'])
        self.assertNotIn(str(self.root), result.export_json)
        claimed = self.deliveries.claim_due()[0]
        self.runtime.files.begin_upload(claimed)
        result = self.inspect(delivery)
        self.assertEqual(result.disposition, 'unknown')
        self.assertEqual(self.uploads, [])  # No transport invocation by preview.


class OfficialFilePreviewTests(FileFixture):
    def setUp(self):
        super().setUp()

    def preview_file(self, artifact_id, *, now=None):
        target = EndpointKey(AccountKey('qq-official', 'TEST_M5_DEST', 'test'), 'group', 'TEST_DEST')
        actor = ActorKey(target.account, 'group_member', 'TEST_DEST', 'TEST_TARGET_ACTOR')
        selection = Selection(self.endpoint, self.endpoint.scope, target)
        preview = Previewer(route_guard=lambda *a: True, project_guard=lambda *a: True,
            actor_mapping=lambda c, s, r, a: MappingEvidence('a' * 32, 1, a, target, actor))
        with self.tx() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            return preview.preview(cursor, selection, SourceRef('file', 'official:' + artifact_id),
                now=self.now if now is None else now)

    def test_official_retention_and_draft_metadata_not_reset_by_preview(self):
        artifact = self.put(draft=True, draft_reason='Unverified final section')
        result = self.preview_file(artifact.artifact_id)
        self.assertEqual(result.disposition, 'unsent')
        self.assertEqual(result.original_deadline, artifact.retain_until)
        self.assertTrue(result.export['draft'])
        self.assertEqual(result.export['draft_reason'], 'Unverified final section')
        self.assertNotIn('TEST_INPUT', result.export_json)
        expired = self.preview_file(artifact.artifact_id, now=artifact.retain_until)
        self.assertEqual(expired.disposition, 'terminal')

    def test_actual_official_upload_unknown_never_exports_new_file_intent(self):
        artifact = self.put()
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_file_deliveries SET state='upload_unknown',reason_code='test_unknown' WHERE artifact_id=?",
                (artifact.artifact_id,))
        result = self.preview_file(artifact.artifact_id)
        self.assertEqual((result.disposition, result.export), ('unknown', None))


if __name__ == '__main__':
    unittest.main()
