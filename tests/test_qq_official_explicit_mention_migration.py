import unittest

from tests import test_qq_official_outbox_migration as migration


class ExplicitMentionMigrationTests(unittest.TestCase):
    def test_backfill_requires_mention_evidence_not_just_dispatch(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, '0135_retire_grant_anchors')
        rows = migration.OfficialOutboxMigrationTests()
        with migration.connect(schema) as rows.connection:
            db = rows.connection
            group = rows.endpoint(kind='group', native='TEST_GROUP')
            for inbox_id, event_type, payload in (
                ('TEST_AT_EVENT', 'GROUP_AT_MESSAGE_CREATE', None),
                ('TEST_LATE_AT', 'GROUP_MESSAGE_CREATE', None),
                ('TEST_AT_PAYLOAD', 'GROUP_MESSAGE_CREATE', '{"d":{"mentions":[{"is_you":true}]}}'),
                ('TEST_MEMBER_QUOTE', 'GROUP_MESSAGE_CREATE', '{"d":{"content":"TEST quote"}}'),
                ('TEST_INVALID_JSONB', 'GROUP_MESSAGE_CREATE', r'{"d":{"content":"\u0000"}}'),
            ):
                migration.insert(db, 'qq_official_inbound_events', dict(
                    inbox_id=inbox_id, account_key=group.account.key, endpoint_key=group.key,
                    endpoint_generation=1, route_epoch=1, event_type=event_type, status='processed',
                    trigger_requested=True, payload_json=payload, payload_expires_at=200,
                    received_at=100, created_at=100, updated_at=100,
                ))
            migration.insert(db, 'qq_official_gateway_receipts', dict(
                account_key=group.account.key, session_id='TEST_SESSION', seq=1,
                event_type='GROUP_AT_MESSAGE_CREATE', disposition='inbox', inbox_id='TEST_LATE_AT', received_at=101,
            ))
        migration.migrate(schema, '0136_explicit_mention')
        with migration.connect(schema) as db:
            self.assertEqual(dict(db.execute('SELECT inbox_id,explicit_mention FROM qq_official_inbound_events')), {
                'TEST_AT_EVENT': True, 'TEST_LATE_AT': True, 'TEST_AT_PAYLOAD': True,
                'TEST_MEMBER_QUOTE': False, 'TEST_INVALID_JSONB': False,
            })
            self.assertEqual(db.execute('SELECT count(*) FROM qq_official_inbound_events WHERE trigger_requested').fetchone(), (5,))
