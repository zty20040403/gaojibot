from datetime import datetime
import logging
from types import SimpleNamespace
import unittest

from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.send_types import SendReservation
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.schedules import Schedules, next_time, ZONE
from tests.qq_official_turn_fixture import ExecutionFixture


class ScheduleTests(unittest.TestCase):
    def test_shanghai_time_and_hourly_floor(self):
        now = int(datetime(2026, 1, 1, 8, tzinfo=ZONE).timestamp())
        expected = int(datetime(2026, 1, 1, 9, tzinfo=ZONE).timestamp())
        self.assertEqual(next_time(at='2026-01-01T09:00:00', now=now), expected)
        self.assertEqual(next_time(cron='0 9 * * *', now=now), expected)
        with self.assertRaises(ValueError):
            next_time(cron='*/30 * * * *', now=now)

    def test_due_reminder_is_atomic_and_routine_creates_one_background_job(self):
        f = ExecutionFixture('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        handle = f.claim().handle
        deliveries, usage = DeliveryStore(f.database), UsageStore(f.database)
        self.addCleanup(deliveries.close)
        self.addCleanup(usage.close)
        runtime = SimpleNamespace(jobs=f.jobs, _calls=None,
            store=SimpleNamespace(transaction=f.tx, admitted=f.admission, clock=lambda: f.now),
            outbox=OfficialSendStore(deliveries, admission=f.admission, pepper=b'TEST_SCHEDULE_PEPPER' * 3, clock=lambda: f.now))
        app = SimpleNamespace(logger=logging.getLogger(__name__), usage_store=usage, model_catalog=None,
            model_preferences=SimpleNamespace(get_explicit=lambda _: None),
            settings=SimpleNamespace(background_model='TEST_MODEL', background_effort='high',
                                     background_upstream_profile='', background_gateway_port=0))
        service = OfficialBackgroundAgents(runtime, f.token, app)
        store = Schedules(f.tx, clock=lambda: f.now)
        source = dict(account_key=f.account.key, endpoint_key=f.endpoint.key, actor_key=f.actor.key,
            scope_key=f.endpoint.scope.key, source_execution_id=handle.execution_id, profile='TEST_MODEL',
            canonical_message_id=f.message.canonical_message_id, native_message_id='TEST_INPUT', endpoint_generation=1, route_epoch=1)
        with f.tx() as cursor:
            observe_ready(cursor, f.token, {'d': {'user': {'id': 'TEST_BOT'}}}, f.now)
            one = store.create(cursor, source, text='TEST meeting', at=datetime.fromtimestamp(f.now+60, ZONE).isoformat())
            two = store.create(cursor, source, kind='routine', text='TEST digest', cron='0 * * * *')
            self.assertFalse(store.cancel(cursor, f.endpoint.key, 'TEST_OTHER_ACTOR', one['id']))
        for row in (one, two):
            f.now = row['next_at']
            service.schedules.fire(row['id'], row['next_at'])
            service.schedules.fire(row['id'], row['next_at'])
            if row['kind'] == 'reminder':
                delivery = f.query('SELECT delivery_id FROM deliveries WHERE account_key=?', (f.account.key,))[0]
                prepared = runtime.outbox.prepare(f.token, delivery['delivery_id'], send_mode='proactive')
                self.assertIsInstance(prepared, SendReservation)
                self.assertIn('<qqbot-at-user id="TEST_MEMBER" />', prepared.payload()['markdown']['content'])
        self.assertEqual(len(f.query('SELECT * FROM delivery_plans WHERE account_key=?', (f.account.key,))), 1)
        self.assertEqual(len(service.store.list(f.endpoint.key, f.actor.key)), 1)
        with f.tx() as cursor:
            self.assertEqual([row['id'] for row in store.list(cursor, f.endpoint.key, f.actor.key)], [two['id']])
