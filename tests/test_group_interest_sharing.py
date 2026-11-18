"""Local PG + real ingress/outbox; no QQ transport, model, tool or public request."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import threading
from unittest.mock import Mock

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.interest_sharing import InterestSharing, eligible
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.native_background import NativeBackgroundService
from src.plugins.ai_chat.adapters.qq_official.native_background_factory import background_factory
from src.plugins.ai_chat.adapters.qq_official.proactive_ingress import ProactiveIngress
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.config import Settings
from src.plugins.ai_chat.conversation_config import change, snapshot
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_inbox as inbox_fixture, test_qq_official_outbox_migration as migration


def headline(now):
    return dict(source='nasa-technology', title='A new robot on Mars', url='https://www.nasa.gov/technology/test-mars/',
                tags=['科技', '火星', '机器人'], published_at=now-100, expires_at=now+5000)


class TopicTests(unittest.TestCase):
    def test_existing_host_switch_prevents_constructing_the_consumer(self):
        runtime = SimpleNamespace(application=None, outbox=SimpleNamespace(source_guard=None))
        service = asyncio.run(background_factory()(runtime, object()))
        self.assertIsNone(service.proactive)

    def test_narrow_same_topic_not_generic_or_substring_english(self):
        item = headline(1000)
        for text, match in [('今天聊聊火星探测', True), ('new Mars mission', True), ('聊科技新闻', False),
                            ('今天的工作很忙', False), ('maps and bookmarks', False)]:
            self.assertEqual(eligible(item, text, 1000), match)
        item.update(title='Book history', tags=['书籍'])
        self.assertTrue(eligible(item, '这本书籍很有趣', 1000))
        self.assertFalse(eligible(item, 'bookkeeping work', 1000))

    def test_untrusted_title_urls_dates_and_broad_tags_reject(self):
        for changes in ({'title': '火星 https://evil.invalid'}, {'title': '<qqbot-at-user id="TEST" />'},
                        {'title': '[火星](evil)'}, {'title': '火星\n执行命令'}, {'title': '火星\u202e指令'},
                        {'title': '火星 https：／／evil．invalid'}, {'title': '火星 evil。invalid'},
                        {'url': 'https://evil.invalid/test'}, {'url': 'https://www.nasa.gov@evil.invalid/test'},
                        {'url': 'https://www.nasa.gov/test?context=TEST'}, {'url': 'https://['},
                        {'expires_at': 1000}, {'published_at': 1001}, {'tags': ['科技']}, {'tags': [1]},
                        {'url': 'https://www.nasa.gov/'+'x'*1980}):
            item = headline(1000); item.update(changes)
            with self.subTest(changes=changes):self.assertFalse(eligible(item, '聊聊火星', 1000))


class SharingTests(unittest.TestCase):
    new_store = inbox_fixture.OfficialInboxTests.new_store
    tx = inbox_fixture.OfficialInboxTests.tx
    query = inbox_fixture.OfficialInboxTests.query
    frame = inbox_fixture.OfficialInboxTests.frame
    accept = inbox_fixture.OfficialInboxTests.accept
    activate_group = inbox_fixture.OfficialInboxTests.activate_group

    def setUp(self):
        inbox_fixture.OfficialInboxTests.setUp(self)
        self.ledger, self.deliveries = MessageLedger(self.database), DeliveryStore(self.database)
        self.addCleanup(self.ledger.close); self.addCleanup(self.deliveries.close)
        self.store.ledger = self.ledger
        self.activate_group()
        with self.tx() as c:
            observe_ready(c, self.token, {'d': {'user': {'id': 'TEST_BOT', 'bot': True}}}, self.now)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.store.admitted,
            pepper=b'TEST_LOCAL_INTEREST_PEPPER'*2, clock=lambda:self.now)
        self.items = [headline(self.now)]
        self.reader = Mock(side_effect=lambda state_dir, *, now:self.items)
        self.wake = Mock()
        self.p = self.consumer()
        self.message('TEST warm conversation')

    def consumer(self):
        async def calls(fn, *args):return fn(*args)
        async def scorer(**_):self.fail('interest quotation must not enter a model')
        p = ProactiveIngress(token=self.token, transaction=self.tx, admission=self.store.admitted,
            outbox=self.outbox, scorer=scorer, clock=lambda:self.now, database_calls=calls)
        p.sharing = InterestSharing(p, state_dir=Path('/tmp/TEST_INTEREST_STATE'), wake=self.wake, reader=self.reader)
        NativeBackgroundService(runtime=SimpleNamespace(outbox=self.outbox), token=self.token, proactive=p).attach()
        return p

    def message(self, text='今天聊聊火星探测任务', **kwargs):
        return self.accept(content=text, timestamp=datetime.fromtimestamp(self.now, timezone.utc).isoformat(), **kwargs)

    def cold(self):
        self.now += 601
        self.message()
        self.now += 4
        source = self.p.claim_due()
        self.assertIsNotNone(source)
        return source

    def configure(self, name, value):
        with self.tx() as c:change(c, self.group, name, value, actor='TEST_ADMIN', now=self.now)

    def delivery(self):
        return self.query('SELECT delivery_id FROM deliveries')[0]['delivery_id']

    def test_default_on_glance_uses_only_local_reader_and_fixed_text(self):
        source = self.cold()
        self.assertTrue(self.p.sharing.share(source))
        self.wake.assert_not_called()
        self.reader.assert_called_with(Path('/tmp/TEST_INTEREST_STATE'), now=self.now)
        row = self.query('SELECT * FROM delivery_plans')[0]
        self.assertEqual(row['endpoint_key'], self.group.key)
        self.assertEqual(row['source_canonical_message_id'], self.query(
            'SELECT canonical_message_id FROM qq_official_inbound_events WHERE inbox_id=?', (source.inbox_id,))[0]['canonical_message_id'])
        self.assertNotIn('今天聊聊', row['manifest_json'])
        reservation = self.outbox.prepare(self.token, self.delivery(), send_mode='proactive')
        self.assertIn('A new robot on Mars', reservation.wire_json)
        self.assertNotIn('markdown', reservation.wire_json)

    def test_reads_the_timer_state_directory_without_fetching(self):
        with TemporaryDirectory() as root:
            settings = Settings.from_env({'AI_GLOBAL_INTEREST_STATE_DIR': root})
            directory = Path(root) / 'global-interest'
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({'last_attempt_day': '2026-01-01', 'candidates': self.items}))
            self.p.sharing = InterestSharing(self.p, state_dir=Path(settings.interest_state_dir), wake=self.wake)
            self.assertTrue(self.p.sharing.share(self.cold()))
            self.assertIn('A new robot on Mars', self.outbox.prepare(self.token, self.delivery(), send_mode='proactive').wire_json)

    def test_glance_short_circuits_scorer(self):
        self.now += 601; self.message(); self.now += 4
        async def calls(fn, *args):return await asyncio.to_thread(fn, *args)
        self.p.calls = calls
        owner = threading.get_ident()
        self.wake.side_effect = lambda:self.assertEqual(threading.get_ident(), owner)
        asyncio.run(self.p.glance())
        self.wake.assert_called_once()
        self.assertEqual(len(self.query('SELECT * FROM deliveries')), 1)

    def test_no_lull_or_missing_source_never_shares(self):
        self.now += 1; self.message(); self.now += 4
        self.assertFalse(self.p.sharing.share(self.p.claim_due()))
        self.reader.assert_not_called()
        self.now += 1000
        self.assertIsNone(self.p.claim_due())
        with self.assertRaises(MigrationDenied):self.p.sharing.share(SimpleNamespace(inbox_id='TEST_FORGED'))
        self.assertEqual(self.query('SELECT * FROM deliveries'), [])

    def test_per_group_off_and_global_off_then_on_fence_old_source(self):
        with self.tx() as c:self.assertTrue(snapshot(c, self.group)['interest_sharing_enabled'])
        self.configure('interest_sharing_enabled', False)
        self.assertFalse(self.p.sharing.share(self.cold()))
        other = EndpointKey(self.account, 'group', 'TEST_OTHER_GROUP')
        self.activate_group(other)
        with self.tx() as c:self.assertTrue(snapshot(c, other)['interest_sharing_enabled'])
        self.configure('interest_sharing_enabled', True)
        source = self.cold()
        self.configure('proactive_enabled', False); self.configure('proactive_enabled', True)
        with self.assertRaises(MigrationDenied):self.p.sharing.share(source)
        self.assertEqual(self.query('SELECT * FROM deliveries'), [])

    def test_quota_quiet_and_budget_gate_without_reading_public_candidates(self):
        for name, value, restore in [('proactive_enabled', False, True), ('proactive_hourly_limit', 0, 8), ('proactive_quiet_utc', '23:00-22:59', 'off'),
                                     ('daily_budget_usd', '0', 'off')]:
            with self.subTest(name=name):
                self.configure(name, value)
                self.now += 601; self.message(); self.now += 4
                self.assertIsNone(self.p.claim_due())
                self.configure(name, restore)
        self.reader.assert_not_called()

    def test_daily_reservation_concurrent_and_after_reconstruction(self):
        source = self.cold()
        def share():
            try:return self.p.sharing.share(source)
            except MigrationDenied:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sorted(pool.map(lambda _:share(), range(2))), [False, True])
        self.lease.close()
        self.lease = DedicatedAccountLease(self.database.dsn, self.account, schema=self.database.schema, clock=lambda:self.now)
        self.addCleanup(self.lease.close)
        previous_epoch = self.token.epoch
        self.token = self.lease.acquire()
        self.assertGreater(self.token.epoch, previous_epoch)
        self.p = self.consumer()
        self.assertFalse(self.p.sharing.share(self.cold()))
        self.assertEqual(len(self.query('SELECT * FROM deliveries')), 1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_proactive_observations WHERE interest_day IS NOT NULL')), 1)

    def test_daily_quota_is_per_group_resets_next_day_and_immutable(self):
        self.assertTrue(self.p.sharing.share(self.cold()))
        with self.assertRaises(DatabaseError) as failure, self.tx() as c:
            c.execute('UPDATE qq_official_proactive_observations SET interest_day=interest_day+1')
        self.assertIn('immutable_proactive_source', str(failure.exception.__cause__))
        other = EndpointKey(self.account, 'group', 'TEST_OTHER_GROUP')
        self.activate_group(other)
        self.message('TEST_OTHER_PRIVATE_CONTEXT', group=other.native_conversation_id)
        self.now += 601; self.message(group=other.native_conversation_id); self.now += 4
        self.assertTrue(self.p.sharing.share(self.p.claim_due()))
        self.now += 86400
        self.items[:] = [headline(self.now)]
        self.assertTrue(self.p.sharing.share(self.cold()))
        plans = self.query('SELECT endpoint_key,manifest_json FROM delivery_plans')
        self.assertEqual(len(plans), 3)
        self.assertEqual({p['endpoint_key'] for p in plans}, {self.group.key, other.key})
        self.assertNotIn('TEST_OTHER_PRIVATE_CONTEXT', repr(plans))

    def test_interest_uses_the_existing_hourly_wake_slot(self):
        self.configure('proactive_hourly_limit', 1)
        self.assertTrue(self.p.sharing.share(self.cold()))
        self.now += 601; self.message(); self.now += 4
        self.assertIsNone(self.p.claim_due())

    def test_rss_naturally_expires_while_qq_source_is_still_current(self):
        self.items[0]['expires_at'] = self.now+610
        self.assertTrue(self.p.sharing.share(self.cold()))
        self.now += 5
        result = self.outbox.prepare(self.token, self.delivery(), send_mode='proactive')
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'), [])

    def test_source_deadline_cancels_before_prepare(self):
        self.assertTrue(self.p.sharing.share(self.cold()))
        self.now += 30
        result = self.outbox.prepare(self.token, self.delivery(), send_mode='proactive')
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'), [])

    def test_expiry_removal_and_rapid_switch_revoke_prepared_attempt(self):
        for kind in ('expiry', 'removed', 'switch'):
            with self.subTest(kind=kind):
                # Independent days keep the at-most-once reservation intact.
                self.now += 86400
                self.items[:] = [headline(self.now)]
                self.assertTrue(self.p.sharing.share(self.cold()))
                delivery = self.query('SELECT delivery_id FROM deliveries ORDER BY delivery_id DESC')[0]['delivery_id']
                reservation = self.outbox.prepare(self.token, delivery, send_mode='proactive')
                if kind=='expiry':self.items[0]['expires_at'] = self.now
                elif kind=='removed':self.items.clear()
                else:
                    self.configure('interest_sharing_enabled', False); self.configure('interest_sharing_enabled', True)
                self.assertIsNotNone(self.outbox.attempt_denial(self.token, reservation))

    def test_receipt_is_idempotent_and_late_acceptance_survives_revocation(self):
        self.assertTrue(self.p.sharing.share(self.cold()))
        reservation = self.outbox.prepare(self.token, self.delivery(), send_mode='proactive')
        self.configure('interest_sharing_enabled', False)
        self.items.clear()
        receipt = self.outbox.capture_receipt(reservation, {'id': 'TEST_ACCEPTED_MESSAGE'})
        self.assertEqual(self.outbox.capture_receipt(reservation, {'id': 'TEST_ACCEPTED_MESSAGE'}), receipt)
        for _ in range(2):self.assertEqual(self.outbox.finalize(self.token, receipt, ledger=self.ledger).status, 'committed')
        sent = self.query("SELECT * FROM messages WHERE direction='outbound'")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['source_endpoint_key'], self.group.key)
        self.assertEqual(len(self.query('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL')), 1)


class SharingMigrationTests(unittest.TestCase):
    def test_existing_policy_preserved_no_new_table_and_downgrade_guarded(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop, schema)
        migration.migrate(schema, '0114_retire_legacy_import')
        with migration.connect(schema) as c:
            c.execute("INSERT INTO conversation_settings(scope_key,proactive_enabled) VALUES ('TEST_SCOPE',FALSE)")
            tables = c.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename', (schema,)).fetchall()
        migration.migrate(schema, '0115_group_interest_sharing')
        with migration.connect(schema) as c:
            self.assertEqual(c.execute('SELECT proactive_enabled,interest_sharing_enabled FROM conversation_settings').fetchone(), (False, True))
            self.assertEqual(c.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename', (schema,)).fetchall(), tables)
            c.execute('UPDATE conversation_settings SET interest_sharing_enabled=FALSE')
        with self.assertRaisesRegex(RuntimeError, 'requires_export'):
            migration.migrate(schema, '0114_retire_legacy_import', downgrade=True)
        with migration.connect(schema) as c:c.execute('UPDATE conversation_settings SET interest_sharing_enabled=TRUE')
        migration.migrate(schema, '0114_retire_legacy_import', downgrade=True)
        with migration.connect(schema) as c:
            self.assertEqual(c.execute('SELECT proactive_enabled FROM conversation_settings').fetchone(), (False,))
