"""Pure progress tests; no PostgreSQL, credentials or transport I/O."""
import asyncio
from contextlib import nullcontext
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.plugins.ai_chat.adapters.qq_official import progress, send_policy
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.outbox_types import OutboxInvariantError, PlanRef, stable_json, digest
from src.plugins.ai_chat.adapters.qq_official.subagent_actions import file_coordinator
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import TEXT_TOOLS, FILE_TOOLS
from src.plugins.ai_chat.ai_tools import SAY_TOOL
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, BackgroundToolSource
from src.plugins.ai_chat.tools.progress import enabled, execute


class Cursor:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.duplicate = False
        self.plan = None
        self.sql = []

    def execute(self, sql, args=()):
        self.sql.append((sql, args))
        if 'idempotency_key' in sql:
            self.result = {'plan_id': 'old'} if self.duplicate else None
        elif 'FROM qq_official_endpoints' in sql:
            self.result = {'endpoint_key': self.endpoint.key}
        else:
            self.result = self.plan
        return self

    def fetchone(self):
        return self.result


class ProgressUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = AccountKey('qq-official', 'TEST_PROGRESS', 'test')
        self.endpoint = EndpointKey(self.account, 'group', 'TEST_GROUP')
        self.actor = ActorKey(self.account, 'group_member', 'TEST_GROUP', 'TEST_ACTOR')
        self.token = SimpleNamespace(account_key=self.account.key, epoch=7)
        self.now = 100
        self.cursor = Cursor(self.endpoint)
        self.outbox = OfficialOutbox(SimpleNamespace(transaction=lambda: nullcontext(self.cursor)),
            admission=lambda *_: True, pepper=b'TEST'*8, clock=lambda: self.now)
        self.context = progress.ProgressContext('task:123:1', self.endpoint, self.actor,
            self.endpoint.scope.key, 600, 'TEST_NATIVE', 123)
        self.current = Mock(side_effect=lambda *_: self.context)
        async def calls(fn, *args):
            return fn(*args)
        self.wake = Mock()
        self.emitter = progress.ProgressEmitter(token=self.token, outbox=self.outbox,
            current=self.current, database_calls=calls, wake=self.wake)
        self.choice = SimpleNamespace(send_mode='passive')

    def record_plan(self, cursor, **kwargs):
        plan = dict(plan_id='TEST_PLAN', purpose=kwargs['purpose'], payload_mode='persistent', part_count=1,
            account_key=self.account.key, endpoint_key=self.endpoint.key, requester_actor_key=self.actor.key,
            source_scope_key=kwargs['source_scope_key'], source_canonical_message_id=123,
            expires_at=kwargs['expires_at'])
        self.assertTrue(kwargs['source_guard'](cursor, token=self.token, plan=plan, now=self.now))
        plan['manifest_json'] = stable_json({'v': 1, 'control': None, 'source_native_message_id': 'TEST_NATIVE'})
        plan['manifest_digest'] = digest(plan['manifest_json'])
        self.cursor.plan = plan
        return PlanRef('TEST_PLAN', (1,), True)

    async def emit(self):
        with patch.object(progress, 'check_send_policy', return_value=self.choice), \
                patch.object(progress, 'enqueue_plan', side_effect=self.record_plan) as enqueue:
            result = await self.emitter.say('PDF 已生成，正在核对页数。')
        return result, enqueue

    async def test_enqueued_is_not_accepted_and_original_deadline_is_bounded(self):
        self.context = replace(self.context, deadline_at=111)
        result, enqueue = await self.emit()
        self.assertEqual(result, {'ok': True, 'status': 'enqueued'})
        self.assertEqual(enqueue.call_args.kwargs['expires_at'], 111)
        self.assertEqual(enqueue.call_args.kwargs['purpose'], 'progress')
        self.wake.assert_called_once_with()

    async def test_current_rechecked_on_candidate_and_send_boundary_and_revocation_denies(self):
        await self.emit()
        row = dict(self.cursor.plan)
        self.assertTrue(self.outbox._authorized(self.cursor, self.token, row, self.endpoint, self.actor, self.now))
        self.current.side_effect = PermissionError('TEST_REVOKED')
        self.assertFalse(self.outbox._authorized(self.cursor, self.token, row, self.endpoint, self.actor, self.now))
        self.current.side_effect = progress.TaskLeaseLost('TEST_CANCELLED_TASK')
        self.assertFalse(self.outbox._authorized(self.cursor, self.token, row, self.endpoint, self.actor, self.now))

    async def test_epoch_and_restart_never_reauthorize_old_plan(self):
        await self.emit()
        row = dict(self.cursor.plan)
        newer = SimpleNamespace(account_key=self.account.key, epoch=8)
        self.assertFalse(self.outbox._authorized(self.cursor, newer, row, self.endpoint, self.actor, 101))
        self.outbox._progress.clear()
        self.assertFalse(self.outbox._authorized(self.cursor, self.token, row, self.endpoint, self.actor, 101))

    async def test_duplicate_after_restart_or_unknown_does_not_enqueue_or_extend_ttl(self):
        self.cursor.duplicate = True
        with patch.object(progress, 'check_send_policy') as policy, patch.object(progress, 'enqueue_plan') as enqueue:
            result = await self.emitter.say('PDF 已生成，正在核对页数。')
        self.assertEqual(result['reason'], 'already_reported')
        policy.assert_not_called()
        enqueue.assert_not_called()
        self.wake.assert_not_called()

    async def test_no_window_no_permission_suppresses_without_plan_or_attempt(self):
        self.choice = send_policy.PolicyWait('waiting_reply_window')
        result, enqueue = await self.emit()
        self.assertEqual(result['status'], 'suppressed')
        enqueue.assert_not_called()
        self.assertFalse(any('INSERT' in sql for sql, _ in self.cursor.sql))

    async def test_task_failure_isolated_but_cancellation_propagates(self):
        self.current.side_effect = RuntimeError('TEST_PRIVATE_DETAIL')
        result = await self.emitter.say('test')
        self.assertEqual(result['reason'], 'progress_unavailable')
        self.assertNotIn('TEST_PRIVATE_DETAIL', json.dumps(result))
        self.current.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.emitter.say('test')

    async def test_tool_enabled_only_for_bound_port_and_returns_queue_state(self):
        source = BackgroundToolSource(self.endpoint, self.actor, 'subagent', 'task:123', 1)
        context = SimpleNamespace(inbound=None, background_source=source, platform=SimpleNamespace(
            progress=self.emitter, progress_enabled=True))
        self.assertTrue(enabled(context))
        with patch.object(self.emitter, 'say', return_value={'ok': True, 'status': 'enqueued'}):
            result = json.loads(await execute(SimpleNamespace(context=context), {'text': 'test'}))
        self.assertEqual(result['status'], 'enqueued')
        context.platform.progress = None
        self.assertFalse(enabled(context))
        self.assertEqual(json.loads(await execute(SimpleNamespace(context=context), {'text': 'test'}))['status'], 'suppressed')

    async def test_real_coordinator_progress_callback_queues_without_finishing_result(self):
        from src.plugins.ai_chat.subagents import SubAgentCoordinator
        with patch.object(progress, 'check_send_policy', return_value=self.choice), \
                patch.object(progress, 'enqueue_plan', side_effect=self.record_plan) as enqueue:
            await SubAgentCoordinator._notify_progress(SimpleNamespace(logger=Mock()),
                self.emitter.report, '文件已生成，开始独立验收。')
        self.assertEqual(enqueue.call_args.kwargs['purpose'], 'progress')
        self.assertNotIn('final', [sql for sql, _ in self.cursor.sql])

    def test_generic_progress_enqueue_and_forged_emission_are_rejected(self):
        with self.assertRaisesRegex(OutboxInvariantError, 'progress_authority_unavailable'):
            self.outbox.enqueue(self.token, purpose='progress', rendered=None, requester=None,
                logical_key='test', source_scope_key='test', expires_at=130)
        with self.assertRaisesRegex(OutboxInvariantError, 'progress_authority_unavailable'):
            self.outbox.enqueue_progress(self.token, emission=object(), rendered=None,
                logical_key='test', cursor=self.cursor, now=100)

    async def test_m5_generic_plan_guard_is_not_used_as_a_progress_authority(self):
        self.outbox.source_guard = Mock(side_effect=AssertionError('final guard is not a progress proof'))
        await self.emit()
        self.assertTrue(self.outbox._authorized(self.cursor, self.token, self.cursor.plan, self.endpoint, self.actor, 101))
        self.outbox.source_guard.assert_not_called()
        self.current.assert_called()

    def test_file_roles_expose_say_as_runtime_tool(self):
        self.assertIn('say', TEXT_TOOLS | FILE_TOOLS)
        coordinator = file_coordinator(Mock(), Mock(), logger=Mock())
        for role in ('coder', 'document'):
            self.assertIn('say', coordinator.registry.worker(role).allowed_tools)
        self.assertEqual(SAY_TOOL['function']['name'], 'say')


class ProgressPolicyTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountKey('qq-official', 'TEST_PROGRESS_POLICY', 'test')
        self.endpoint = EndpointKey(self.account, 'group', 'TEST_GROUP')
        self.row = dict(endpoint_key=self.endpoint.key, account_key=self.account.key,
            generation=1, route_epoch=1, capability_revision=1, binding_state='active', membership_state='present',
            proactive_state='unknown', proactive_probe_attempt_id=None, passive_blocked_until=0, proactive_blocked_until=0)
        self.window = dict(state='active', issued_at=99, endpoint_generation=1, route_epoch=1,
            expires_at=400, next_seq=1, max_replies=5, used_count=0, reserved_count=0,
            window_id='TEST_WINDOW', credential_kind='msg_id', credential_value='TEST_NATIVE')

    def choice(self, window, **updates):
        self.row.update(updates)
        cursor = Mock()
        cursor.execute.return_value.fetchone.side_effect = [window, None] if window is not None and window['used_count'] == 0 else [window]
        with patch.object(send_policy, '_target', return_value=self.row), \
                patch.object(send_policy, '_quota_admission', return_value=SimpleNamespace(period_end_at=None)):
            return send_policy.check_send_policy(cursor, token=Mock(), endpoint=self.endpoint,
                endpoint_row=self.row, source_native_id='TEST_NATIVE', purpose='progress', deadline=130,
                now=100, allow_proactive_probe=True)

    def test_valid_window_allows_progress_and_preserves_four_final_slots(self):
        self.assertEqual(self.choice(self.window).send_mode, 'passive')
        self.window['used_count'] = 1
        self.assertEqual(self.choice(self.window).reason, 'waiting_reply_window')

    def test_only_verified_proactive_allows_no_window_never_unknown_probe(self):
        self.assertEqual(self.choice(None).reason, 'waiting_reply_window')
        self.assertEqual(self.choice(None, proactive_state='denied').reason, 'waiting_reply_window')
        choice = self.choice(None, proactive_state='allowed')
        self.assertEqual(choice.send_mode, 'proactive')
        self.assertFalse(choice.requires_probe)

    def test_durable_prior_progress_blocks_even_a_larger_window(self):
        self.window['max_replies'] = 20
        cursor = Mock()
        cursor.execute.return_value.fetchone.side_effect = [self.window, {'prior': 1}]
        with patch.object(send_policy, '_target', return_value=self.row):
            choice = send_policy.check_send_policy(cursor, token=Mock(), endpoint=self.endpoint,
                endpoint_row=self.row, source_native_id='TEST_NATIVE', purpose='progress',
                deadline=130, now=100)
        self.assertEqual(choice.reason, 'waiting_reply_window')
        self.assertIn("window_charge_state IN ('reserved','spent')", cursor.execute.call_args.args[0])
