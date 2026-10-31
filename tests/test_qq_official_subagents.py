from types import SimpleNamespace
"""Actual Coordinator + real disposable PG + M3 handoff, no external calls."""
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, Mock, patch
import json

from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from tests import qq_official_turn_fixture as fixtures

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence
from src.plugins.ai_chat.agent.task_identity import object_owner, owner_fields
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from src.qq_official.testing.fake_platform import FakePlatform
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import OfficialSubAgentDispatcher, assert_current, job_kind, submit


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixtures.ExecutionFixture('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.handle = self.f.claim().handle
        self.f.begin(self.handle)
        self.store = SubAgentStore(self.f.database)
        self.addCleanup(self.store.close)
        self.deliveries = DeliveryStore(self.f.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.f.admission,
            pepper=b'TEST_SUBAGENT_PEPPER' * 3, clock=lambda: self.f.now)
        self.profile = ModelProfile(name='test', provider='test', protocol='openai-chat',
            model='test', base_url='http://127.0.0.1', api_key_required=False)
        self.logger = Mock()
        self.coordinator = SubAgentCoordinator(self.store,
            ModelCatalog({'test': self.profile}, default_profile='test'), logger=self.logger,
            max_parallelism=1)
        self.packet = ContextPacket(self.f.endpoint.scope.key, 'TEST_CONVERSATION', None,
            self.f.message.canonical_message_id, '分析文字', endpoint=self.f.endpoint,
            requester_actor=self.f.actor, supporting_context='TEST frozen context')
        self.f.jobs.lease_seconds = 3600
        self.submit_args = dict(token=self.f.token, source_api=execution, source_handle=self.handle,
            source_job=self.f.job, tool_call_id='TEST_CALL', tool_name='run_subagents', packet=self.packet,
            decision=None, profile='test', now=self.f.now, execution_deadline=self.f.now+1200,
            result_wait_seconds=600, admission=self.f.admission, coordinator=self.coordinator, jobs=self.f.jobs)

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()

    async def asyncTearDown(self):
        await self.calls.close()

    def submission(self, **kwargs):
        with self.f.tx() as cursor:
            return submit(cursor, **{**self.submit_args, **kwargs})

    def dispatcher(self):
        return OfficialSubAgentDispatcher(self.f.token, self.coordinator, self.f.jobs,
            self.outbox, self.f.tx, self.calls, self.f.admission, self.profile,
            clock=lambda: self.f.now)

    async def compute_301_with_real_worker(self):
        task = self.submission()
        job = self.f.jobs.claim_due('TEST_SUBAGENT_WORKER', kinds=(job_kind(self.f.account),), now=self.f.now)[0]
        worker = DurableJobWorker(self.f.jobs, logger=self.logger, database_calls=self.calls,
                                  worker_id=job.lease_owner)
        worker.register(job_kind(self.f.account), self.dispatcher().execute)
        async def model(*args, **kwargs):
            self.f.now += 301
            return '{"status":"success","summary":"TEST analysis","artifacts":[]}'
        plan = {'steps':[{'id':'analysis','agent':'analyst','objective':'分析文字','depends_on':[]}]}
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_json', new=AsyncMock(return_value=plan)), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek', new=AsyncMock(return_value='TEST final')):
            await worker._execute(job)  # Real claimed job, heartbeat and settlement path.
        self.assertEqual(self.f.jobs.get(job.job_id).status, 'succeeded')
        self.assertEqual(self.store.get(task.task_id).status, 'completed')
        return self.f.query('SELECT delivery_id FROM deliveries')[0]['delivery_id']

    async def wire_case(self, *, proactive):
        fake = FakePlatform(clock=lambda: self.f.now)
        fake.app_id = self.f.account.app_id
        await fake.start()
        tokens = TokenManager(Credentials(self.f.account.app_id, fake.app_secret),
            fake.base_url, allow_test_server=True)
        rest = RestClient(tokens, allow_test_server=True)
        authority = OwnerAuthority(clock=lambda: self.f.now)
        binding = BindingReducer(b'TEST_SUBAGENT_BINDING_PEPPER_1234567890',
            epoch_guard=assert_epoch, authority=authority, clock=lambda: self.f.now)
        inbox = OfficialInboxStore(self.f.database, jobs=self.f.jobs, authority=authority,
            binding=binding, clock=lambda: self.f.now)
        self.addCleanup(inbox.close)
        session = 'TEST_SUBAGENT_SESSION'
        inbox.accept({'op':0,'s':0,'t':'READY','d':{'session_id':session}},
            token=self.f.token, session_id=session)
        permission = await fake.emit('GROUP_MSG_RECEIVE' if proactive else 'GROUP_MSG_REJECT',
                                    {'group_openid':self.f.endpoint.native_conversation_id})
        inbox.accept(permission, token=self.f.token, session_id=session)
        with self.f.tx() as cursor:
            observe_ready(cursor, self.f.token, {'op':0,'t':'READY','d':{
                'session_id':session,'user':{'id':'TEST_BOT','bot':True}}}, self.f.now)
        # Explicit trusted test policy: lifecycle RECEIVE is still unknown in
        # production and only this controlled fixture authorizes its one probe.
        # Newer windows are explicitly enabled for this fake-contract test.
        self.outbox = OfficialSendStore(self.deliveries, admission=self.f.admission,
            pepper=b'TEST_SUBAGENT_PEPPER' * 3, clock=lambda: self.f.now,
            allow_proactive_probe=proactive, allow_newer_windows=True)
        self.f.window(source='TEST_INPUT', expires=self.f.now+295)
        fake.add_window('group', self.f.endpoint.native_conversation_id, 'TEST_INPUT')
        async def ensure_current():
            def check():
                with self.f.tx() as cursor:
                    assert_epoch(cursor, self.f.token)
            await self.calls(check)
        sender = OfficialSender(self.f.account, self.f.token, self.outbox, rest, self.f.ledger,
            database_calls=self.calls, ensure_current=ensure_current, wall_clock=lambda:self.f.now)
        try:
            delivery_id = await self.compute_301_with_real_worker()
            first = await sender.send_delivery(delivery_id)
            if proactive:
                self.assertEqual(first.status, 'committed')
                self.assertNotIn('msg_id', fake.received[0]['payload'])
                self.assertEqual(self.f.query('SELECT send_mode FROM delivery_attempts')[0]['send_mode'], 'proactive')
            else:
                self.assertEqual(first.status, 'pending')
                self.assertEqual(first.reason, 'waiting_reply_window')
                self.assertEqual(fake.received, [])
                # A newly observed, admitted AT event produces a real new window.
                frame = await fake.emit('GROUP_AT_MESSAGE_CREATE', {
                    'id':'TEST_NEW_WINDOW', 'content':'TEST follow up',
                    'group_openid':self.f.endpoint.native_conversation_id,
                    'author':{'member_openid':self.f.actor.native_user_id},
                    'timestamp':datetime.fromtimestamp(self.f.now, timezone.utc).isoformat()})
                accepted = inbox.accept(frame, token=self.f.token, session_id=session)
                with self.f.tx() as cursor:
                    result = process_inbox(cursor, inbox_id=accepted.inbox_id, lease_token=self.f.token,
                        ledger=self.f.ledger, jobs=self.f.jobs, epoch_guard=assert_epoch,
                        admission=inbox.admitted, now=self.f.now)
                self.assertEqual(result.status, 'processed')
                self.f.now = max(self.f.now, self.deliveries.get(delivery_id).next_attempt_at)
                self.assertEqual((await sender.send_delivery(delivery_id)).status, 'committed')
                self.assertEqual(fake.received[0]['payload']['msg_id'], 'TEST_NEW_WINDOW')
                self.assertEqual(fake.received[0]['payload']['msg_seq'], 1)
            self.assertEqual(len(fake.received), 1)
            self.assertEqual(self.f.query('SELECT COUNT(*) AS n FROM delivery_attempts')[0]['n'], 1)
            self.assertEqual(self.f.query('SELECT COUNT(*) AS n FROM delivery_receipts WHERE applied_at IS NOT NULL')[0]['n'], 1)
            self.assertEqual(self.f.query("SELECT COUNT(*) AS n FROM messages WHERE direction='outbound'")[0]['n'], 1)
            await sender.send_delivery(delivery_id)
            self.assertEqual(len(fake.received), 1)
        finally:
            await sender.stop()
            await rest.aclose()
            await tokens.aclose()
            await fake.close()

    async def test_301_second_real_subagent_and_worker_send_proactive_through_m3(self):
        await self.wire_case(proactive=True)


    async def test_actual_coordinator_runs_after_old_window_and_hands_background_result_to_m3(self):
        task = self.submission()
        self.f.window(source='TEST_INPUT', expires=self.f.now+295)
        job = self.f.jobs.claim_due('TEST_SUBAGENT_WORKER', kinds=(job_kind(self.f.account),), now=self.f.now)[0]
        calls = []

        async def model(prompt, messages, tools, execute_tool, **kwargs):
            calls.append(prompt)
            self.f.now += 301
            kwargs['transcript_sink']([{'role':'user', 'content':'TEST frozen context'},
                {'role':'assistant', 'content':'TEST analysis'}])
            self.assertEqual(json.loads(await execute_tool('host_inspect', {}))['error'], 'tool_unavailable')
            return '{"status":"success","summary":"TEST analysis","artifacts":[]}'

        plan = {'steps':[{'id':'analysis','agent':'analyst','objective':'分析文字','depends_on':[]}]}
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_json', new=AsyncMock(return_value=plan)), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek', new=AsyncMock(return_value='TEST final')):
            outcome = await self.dispatcher().execute(job)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get(task.task_id).status, 'completed')
        self.assertIsNone(self.store.get(task.task_id).requester_user_id)
        runs = self.store.runs(task.task_id)
        self.assertEqual([(r.role,r.status) for r in runs], [('analyst','succeeded')])
        session = self.store.agent_session(task.task_id, runs[0].run_id,
            scope_key=task.scope_key, **owner_fields(object_owner(task)))
        self.assertEqual(len(session['messages']), 2)
        row = self.f.query('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?', (task.task_id,))[0]
        self.assertEqual(row['plan_id'], outcome['plan_id'])
        self.assertEqual(row['source_native_message_id'], 'TEST_INPUT')
        self.assertGreaterEqual(row['result_at']-row['created_at'], 301)
        self.assertEqual(self.f.query('SELECT purpose FROM delivery_plans')[0]['purpose'], 'background_result')
        self.assertEqual(self.f.query('SELECT COUNT(*) AS n FROM delivery_attempts')[0]['n'], 0)
        # Revisit terminal task: no second model, task or plan.
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=AsyncMock(side_effect=AssertionError('replayed'))):
            repeated = await self.dispatcher().execute(job)
        self.assertEqual(repeated['plan_id'], outcome['plan_id'])
        self.assertEqual(self.f.query('SELECT COUNT(*) AS n FROM delivery_plans')[0]['n'], 1)

    async def test_submission_is_atomic_and_strictly_idempotent(self):
        task = self.submission()
        self.assertEqual(self.submission().task_id, task.task_id)
        self.assertIsNone(task.requester_user_id)
        self.assertEqual(task.endpoint, self.f.endpoint)
        with self.assertRaisesRegex(ValueError, 'subagent_submission_conflict'):
            self.submission(profile='changed')
        self.assertEqual(len(self.f.query('SELECT task_id FROM subagent_tasks')), 1)
        self.assertEqual(self.store.dispatchable_tasks(), [])
        self.assertEqual(self.store.finalizable_tasks(), [])

    async def test_real_m2_unbound_private_owner_can_run_structured_subagent(self):
        authority = OwnerAuthority(clock=lambda: self.f.now)
        binding = BindingReducer(b'TEST_PRIVATE_SUBAGENT_PEPPER_1234567890',
            epoch_guard=assert_epoch, authority=authority, clock=lambda:self.f.now)
        inbox = OfficialInboxStore(self.f.database, jobs=self.f.jobs, authority=authority,
            binding=binding, clock=lambda:self.f.now)
        self.addCleanup(inbox.close)
        inbox.apply_configuration(self.f.token, ('TEST_PRIVATE_OWNER',))
        session='TEST_PRIVATE_SESSION'
        inbox.accept({'op':0,'s':0,'t':'READY','d':{'session_id':session}},
            token=self.f.token, session_id=session)
        accepted = inbox.accept({'op':0,'s':1,'t':'C2C_MESSAGE_CREATE','id':'TEST_PRIVATE_EVENT',
            'd':{'id':'TEST_PRIVATE_MESSAGE','content':'TEST task',
                 'author':{'user_openid':'TEST_PRIVATE_OWNER'},
                 'timestamp':datetime.fromtimestamp(self.f.now,timezone.utc).isoformat()}},
            token=self.f.token, session_id=session)
        with self.f.tx() as cursor:
            ingested = process_inbox(cursor, inbox_id=accepted.inbox_id, lease_token=self.f.token,
                ledger=self.f.ledger, jobs=self.f.jobs, epoch_guard=assert_epoch,
                admission=inbox.admitted, now=self.f.now)
            cursor.execute('UPDATE qq_official_inbound_events SET turn_text=?,turn_text_version=1 WHERE inbox_id=?',
                           ('TEST task', accepted.inbox_id))
        self.assertEqual(ingested.status, 'processed')
        source_job = SimpleNamespace(job_id=None,payload={'inbox_id':accepted.inbox_id})
        with self.f.tx() as cursor:
            handle = execution.claim_execution(cursor, token=self.f.token, job=source_job,
                now=self.f.now, deadline_at=self.f.now+1200, admission=inbox.admitted).handle
            turn = self.f.journal.start_turn(handle.endpoint.scope,
                trigger_canonical_message_id=handle.canonical_message_id, objective='TEST task',
                provider='test', model='test', profile='test', started_at=self.f.now,
                transaction_cursor=cursor)
            execution.begin_execution(cursor, token=self.f.token, handle=handle, job=source_job,
                now=self.f.now, admission=inbox.admitted, journal_turn_id=turn.turn_id)
        endpoint = self.f.query('SELECT binding_state,membership_state FROM qq_official_endpoints WHERE endpoint_key=?',
                                (handle.endpoint.key,))[0]
        self.assertEqual((endpoint['binding_state'],endpoint['membership_state']), ('unbound','present'))
        self.packet = ContextPacket(handle.scope_key, 'TEST_PRIVATE_CONVERSATION', None,
            handle.canonical_message_id, '分析文字', endpoint=handle.endpoint, requester_actor=handle.actor)
        self.submit_args.update(source_handle=handle, source_job=source_job,
            packet=self.packet, admission=inbox.admitted)
        delivery = await self.compute_301_with_real_worker()
        self.assertEqual(self.deliveries.get(delivery).status, 'pending')
        row = self.f.query('SELECT endpoint_key,requester_actor_key FROM qq_official_subagent_dispatches')[0]
        self.assertEqual((row['endpoint_key'],row['requester_actor_key']), (handle.endpoint.key,handle.actor.key))

    async def test_owner_commands_do_not_use_canonical_history_or_same_literal_openid(self):
        from src.plugins.ai_chat.adapters.qq_official.subagent_commands import list_for_endpoint, cancel_for_endpoint
        from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
        task = self.submission()
        other_endpoint = EndpointKey(self.f.account, 'group', 'TEST_OTHER_GROUP')
        self.f.provision(other_endpoint)
        other_actor = ActorKey(self.f.account, 'group_member', other_endpoint.native_conversation_id,
                               self.f.actor.native_user_id)
        with self.f.tx() as cursor:
            self.assertEqual(list_for_endpoint(cursor, token=self.f.token, endpoint=other_endpoint,
                actor=other_actor, now=self.f.now, admission=self.f.admission), ())
            self.assertIsNone(cancel_for_endpoint(cursor, token=self.f.token, endpoint=other_endpoint,
                actor=other_actor, task_id=task.handle, now=self.f.now, admission=self.f.admission))
            visible = list_for_endpoint(cursor, token=self.f.token, endpoint=self.f.endpoint,
                actor=self.f.actor, now=self.f.now, admission=self.f.admission)
            self.assertEqual(visible[0].task_id, task.handle)
            stopped = cancel_for_endpoint(cursor, token=self.f.token, endpoint=self.f.endpoint,
                actor=self.f.actor, task_id=task.handle, now=self.f.now, admission=self.f.admission)
            self.assertEqual(stopped.task_id, task.handle)
        self.assertEqual(self.store.get(task.task_id).status, 'cancelled')
        self.assertEqual(self.f.query('SELECT status FROM durable_jobs WHERE kind=?', (job_kind(self.f.account),))[0]['status'], 'cancelled')
        self.assertEqual(self.f.query('SELECT * FROM deliveries'), [])

    async def test_actual_admin_detail_never_grants_official_numeric_revision(self):
        import httpx
        from fastapi import FastAPI
        from src.plugins.ai_chat.admin import AdminServices
        from tests.admin_session_fixture import ApprovedClient, register_admin
        task = self.submission()
        app = FastAPI()
        register_admin(app, AdminServices(version='test', started_at=1,
            subagent_store=self.store, subagent_coordinator=self.coordinator,
            model_catalog=self.coordinator.model_catalog), test_case=self)
        async with ApprovedClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(f'/bot-admin/api/subagents/{task.task_id}',
                headers={'X-Test-Login':'admin','If-Match':'"0"'})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload['revision_allowed'])
        self.assertFalse(payload['collaboration']['supported'])
        self.assertIsNone(payload['collaboration']['creator_user_id'])
        self.assertIsNone(payload['final_delivery'])

    async def test_cancel_active_real_worker_blocks_result_handoff(self):
        import asyncio
        from src.plugins.ai_chat.adapters.qq_official.subagent_commands import cancel_for_endpoint
        task = self.submission()
        job = self.f.jobs.claim_due('TEST_CANCEL_WORKER', kinds=(job_kind(self.f.account),), now=self.f.now)[0]
        worker = DurableJobWorker(self.f.jobs, logger=self.logger, database_calls=self.calls,
                                  worker_id=job.lease_owner)
        worker.register(job_kind(self.f.account), self.dispatcher().execute)
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(*args, **kwargs):
            entered.set()
            await release.wait()
            return '{"status":"success","summary":"TEST late result"}'
        plan={'steps':[{'id':'analysis','agent':'analyst','objective':'分析文字','depends_on':[]}]}
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_json', new=AsyncMock(return_value=plan)), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=model):
            work = asyncio.create_task(worker._execute(job))
            await asyncio.wait_for(entered.wait(), 5)
            with self.f.tx() as cursor:
                cancel_for_endpoint(cursor, token=self.f.token, endpoint=self.f.endpoint,
                    actor=self.f.actor, task_id=task.handle, now=self.f.now, admission=self.f.admission)
            release.set()
            await asyncio.wait_for(work, 5)
        self.assertEqual(self.f.jobs.get(job.job_id).status, 'cancelled')
        self.assertEqual(self.store.get(task.task_id).status, 'cancelled')
        self.assertEqual(self.f.query('SELECT * FROM deliveries'), [])

    async def test_terminal_result_recovers_handoff_without_reentering_model(self):
        from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
        task = self.submission()
        job = self.f.jobs.claim_due('TEST_RESULT_WORKER', kinds=(job_kind(self.f.account),), now=self.f.now)[0]
        plan={'steps':[{'id':'analysis','agent':'analyst','objective':'分析文字','depends_on':[]}]}
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_json', new=AsyncMock(return_value=plan)), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=AsyncMock(return_value='{"status":"success","summary":"TEST result"}')) as model, \
             patch('src.plugins.ai_chat.subagents.ask_deepseek', new=AsyncMock(return_value='TEST durable final')), \
             patch.object(self.outbox, 'enqueue', side_effect=RuntimeError('TEST sensitive storage detail')):
            with self.assertRaisesRegex(JobDeferred, '^official_subagent_unavailable$'):
                await self.dispatcher().execute(job)
        self.assertEqual(model.await_count, 1)
        self.assertEqual(self.store.get(task.task_id).status, 'completed')
        self.assertEqual(self.f.query('SELECT * FROM deliveries'), [])
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=AsyncMock(side_effect=AssertionError('model replay'))):
            result = await self.dispatcher().reconcile_results()
            repeated = await self.dispatcher().reconcile_results()
        self.assertEqual(result[0][:2], (task.task_id, 'handed_off'))
        self.assertEqual(repeated, ())
        self.assertEqual(self.f.query('SELECT COUNT(*) AS n FROM delivery_plans')[0]['n'], 1)


    async def test_account_loss_fences_store_mutations_even_while_job_lease_is_valid(self):
        task = self.submission()
        job = self.f.jobs.claim_due('TEST_SUBAGENT_WORKER', kinds=(job_kind(self.f.account),), now=self.f.now)[0]
        def guard(cursor):
            return assert_current(cursor, token=self.f.token, task_id=task.task_id,
                now=self.f.now, admission=self.f.admission, job=job)
        mark = active_job_fence.set(JobFence(job.job_id, job.lease_owner, job.attempts,
            lambda: True, job.lease_generation, guard))
        try:
            self.f.account_lease.close()
            with self.assertRaises(LeaseLost):
                self.store.set_task_state(task.task_id, 'failed')
        finally:
            active_job_fence.reset(mark)
        self.assertEqual(self.store.get(task.task_id).status, 'queued')


if __name__ == '__main__':
    unittest.main()
