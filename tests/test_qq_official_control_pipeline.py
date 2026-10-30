from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import AsyncMock, patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.delivery import DeliveryStore
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from src.qq_official.testing.fake_platform import FakePlatform

from src.plugins.ai_chat.adapters.qq_official.command_fence import CommandEffectDenied, guarded_services
from src.plugins.ai_chat.adapters.qq_official.control_handler import ControlJobHandler, handoff_control_reply
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from src.plugins.ai_chat.adapters.qq_official.control_receipts import control_job_kind
from tests.test_qq_official_commands import RouterTests, inbound
from tests.test_qq_official_input_normalization import IngressNormalizationTests, official_module


def _router_fixture(cls=RouterTests):
    return cls()


class CommandFenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = _router_fixture()
        await self.fixture.asyncSetUp()
        self.message = inbound()
        self.live = True
        self.held = False
        self.calls = []

        @contextmanager
        def scope(actual):
            self.assertEqual(actual, self.message)
            if not self.live:
                raise CommandEffectDenied('test_fence_lost')
            self.held = True
            try:
                yield 'TEST_LOCKED_CURSOR'
            finally:
                self.held = False
        self.scope = scope
        self.services = guarded_services(self.fixture.services, self.message, effect_scope=scope,
            database_call=lambda f: f(), settings_authority=self.authority)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
        self.fixture.temp.cleanup()

    def authority(self, guard, message, domain, method, args):
        self.assertTrue(self.held)
        self.assertEqual(guard, 'TEST_LOCKED_CURSOR')
        self.calls.append((domain, method))
        return True

    async def test_shared_settings_write_holds_guard_and_uses_original_store(self):
        old = self.fixture.models._save
        def save():
            self.assertTrue(self.held)
            old()
        with patch.object(self.fixture.models, '_save', side_effect=save):
            result = await self.fixture.route('/model test-model', self.message, self.services)
        self.assertEqual(result.result.status, 'ok')
        self.assertIn(('model', 'set'), self.calls)
        self.assertEqual((await self.fixture.route('/effort high', self.message, self.services)).result.status, 'ok')

    async def test_fence_loss_after_begin_stops_settings_and_cancel(self):
        info, worker = await self.fixture.start_task(self.message)
        async def begin(*_):
            self.live = False
        self.fixture.begin.side_effect = begin
        for command in ('/model test-model', '/cancel ' + info.task_id):
            with self.assertRaises(CommandEffectDenied):
                await self.fixture.route(command, self.message, self.services)
        self.assertFalse(worker.done())
        self.assertEqual(self.fixture.models.items(), [])

    async def test_policy_rechecked_under_lock_and_methods_targets_are_exact(self):
        denied = guarded_services(self.fixture.services, self.message, effect_scope=self.scope,
            database_call=lambda f: f(), settings_authority=lambda *args: False)
        with self.assertRaises(CommandEffectDenied):
            await self.fixture.route('/model test-model', self.message, denied)
        with self.assertRaises(CommandEffectDenied):
            await self.services.db_call(self.fixture.models.set, 'OTHER_ACTOR_KEY', 'test-model')
        with self.assertRaises(CommandEffectDenied):
            await self.services.db_call(self.fixture.models.set_group_enabled, 123, True)
        with self.assertRaises(CommandEffectDenied):
            await self.services.db_call(lambda: None)
        self.assertEqual(self.fixture.models.items(), [])


class ControlPipelineTests(IngressNormalizationTests, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.deliveries = DeliveryStore(self.database)
        self.addCleanup(self.deliveries.close)
        module = official_module('send_store')
        self.outbox = module.OfficialSendStore(self.deliveries, admission=self.store.admitted,
            pepper=b'FAKE_CONTROL_TEST_PEPPER' * 3, clock=lambda: self.now,
            allow_newer_windows=True, allow_proactive_probe=True)
        with self.tx() as cursor:
            observe_ready(cursor, self.token, {'op': 0, 't': 'READY', 'd': {'session_id': self.session,
                'user': {'id': 'TEST_VERIFIED_BOT', 'bot': True}}}, self.now)
        self.fake = FakePlatform(clock=lambda: self.now)
        self.fake.app_id = self.account.app_id
        await self.fake.start()
        self.tokens = TokenManager(Credentials(self.account.app_id, self.fake.app_secret), self.fake.base_url, allow_test_server=True)
        self.rest = RestClient(self.tokens, allow_test_server=True)
        self.sender = OfficialSender(self.account, self.token, self.outbox, self.rest, self.ledger,
            database_calls=self.calls, ensure_current=self.ensure_current, wall_clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.sender.stop()
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()
        await self.calls.close()

    async def ensure_current(self):
        def check():
            with self.tx() as cursor:
                assert_epoch(cursor, self.token)
        await self.calls(check)

    def control_plan(self, *, private=True):
        result = self.accept(private=private, actor='TEST_UNPRIVILEGED', content='/whoami')
        row = self.query('SELECT * FROM qq_official_control_decisions WHERE inbox_id=?', (result.inbox_id,))[0]
        job = self.jobs.claim_due('TEST_CONTROL_WORKER', kinds=(control_job_kind(self.account.key),), now=self.now)[0]
        with self.tx() as cursor:
            plan = handoff_control_reply(cursor, token=self.token, job=job, outbox=self.outbox, now=self.now)
            again = handoff_control_reply(cursor, token=self.token, job=job, outbox=self.outbox, now=self.now)
        self.assertEqual(plan.delivery_ids, again.delivery_ids)
        self.fake.add_window('user' if private else 'group', 'TEST_UNPRIVILEGED' if private else 'TEST_GROUP',
            row['source_native_message_id'])
        return plan.delivery_ids[0], row, job

    async def test_unprivileged_whoami_single_real_loopback_commit_no_owner_or_plain_admission(self):
        delivery, row, _ = self.control_plan()
        with patch.object(self.rest, 'send_message', wraps=self.rest.send_message) as send:
            result = await self.sender.send_delivery(delivery)
            self.assertEqual(result.status, 'committed')
            self.assertEqual((await self.sender.send_delivery(delivery)).status, 'committed')
            self.assertEqual(send.await_count, 2)
        self.assertEqual(len(self.fake.received), 1)
        self.assertEqual(self.fake.received[0]['payload']['msg_id'], row['source_native_message_id'])
        self.assertEqual(self.count('qq_official_owner_grants'), 1)
        self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')), 2)
        self.assertEqual(self.query('SELECT state FROM qq_official_control_decisions WHERE control_id=?',
            (row['control_id'],))[0]['state'], 'terminal')

    async def test_unbound_group_ack_is_exact_passive_only(self):
        delivery, row, _ = self.control_plan(private=False)
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'committed')
        attempt = self.query('SELECT * FROM delivery_attempts ORDER BY attempt_id')[-1]
        self.assertEqual((attempt['send_mode'], attempt['window_id']), ('passive', None))
        self.assertEqual(self.query('SELECT binding_state FROM qq_official_endpoints WHERE endpoint_key=?',
            (row['endpoint_key'],))[0]['binding_state'], 'unbound')


    async def test_expired_source_never_uses_new_window_or_active_permission(self):
        delivery, row, _ = self.control_plan()
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_reply_windows SET state='expired' WHERE window_id=?", (row['reply_window_id'],))
            cursor.execute("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?", (row['endpoint_key'],))
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'failed')
        self.assertEqual(len(self.fake.received), 0)
        self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')), 0)

    async def test_handler_sanitizes_database_exception_before_generic_worker_logging(self):
        _, _, job = self.control_plan()
        handler = ControlJobHandler(token=self.token, outbox=self.outbox, sender=self.sender,
            database_calls=AsyncMock(side_effect=RuntimeError('TEST_SECRET_OTP_AND_ACTOR')),
            ensure_current=self.ensure_current, clock=lambda: self.now)
        with self.assertRaises(JobDeferred) as failure:
            await handler(job)
        self.assertEqual(str(failure.exception), 'official_control_handoff_unavailable')
        self.assertNotIn('TEST_SECRET', str(failure.exception))

    async def test_body_mutation_and_job_lease_loss_do_not_handoff(self):
        delivery, row, job = self.control_plan()
        with self.tx() as cursor:
            cursor.execute("UPDATE deliveries SET body_json=? WHERE delivery_id=?", ('{"nodes":[]}', delivery))
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'failed')
        with self.tx() as cursor:
            cursor.execute("UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?", (job.job_id,))
            with self.assertRaisesRegex(ValueError, 'control_job_lease_lost'):
                handoff_control_reply(cursor, token=self.token, job=job, outbox=self.outbox, now=self.now)
        self.assertFalse(self.fake.received)


for _name in tuple(dir(IngressNormalizationTests)):
    if _name.startswith('test_') and _name not in ControlPipelineTests.__dict__:
        setattr(ControlPipelineTests, _name, None)
del IngressNormalizationTests, RouterTests
