import unittest

import psycopg

from tests import test_qq_official_outbox_migration as migration
from tests.test_drop_dead_tables_migration import _schema_snapshot


class AttentionReasonMigrationTests(unittest.TestCase):
    def test_existing_observation_stays_null_and_reason_is_bounded_and_immutable(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, '0120_retire_context_feedback')
        with migration.connect(schema) as conn:
            conn.execute('SET session_replication_role=replica')
            conn.execute('''INSERT INTO qq_official_proactive_observations
                (inbox_id,account_key,endpoint_key,actor_key,canonical_message_id,source_native_message_id,
                 source_received_at,deadline_at,worker_owner,worker_generation,account_epoch,endpoint_generation,
                 endpoint_route_epoch,state,created_at)
                VALUES ('TEST_OLD','TEST_ACCOUNT','TEST_EP','TEST_ACTOR',1,'TEST_MESSAGE',1,31,
                        'TEST_WORKER',1,1,1,1,'discarded',1)''')
            conn.execute('SET session_replication_role=origin')
        migration.migrate(schema, '0121_attention_reason')
        with migration.connect(schema) as conn:
            self.assertEqual(conn.execute('SELECT state,decision_reason FROM qq_official_proactive_observations').fetchone(), ('discarded', None))
            for reason in ('TEST_MODIFIED', 'x' * 41):
                with self.assertRaises(psycopg.Error), conn.transaction():
                    conn.execute('UPDATE qq_official_proactive_observations SET decision_reason=%s', (reason,))
            self.assertEqual(conn.execute('SELECT decision_reason FROM qq_official_proactive_observations').fetchone(), (None,))


class AttentionCursorMigrationTests(unittest.TestCase):
    def test_existing_group_starts_idle_and_downgrade_cannot_drop_pending_work(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, '0112_proactive_attention')
        with migration.connect(schema) as conn:
            before = _schema_snapshot(conn, schema)
            conn.execute("INSERT INTO qq_official_accounts(account_key,created_at,updated_at) VALUES ('TEST_ACCOUNT',1,1)")
            conn.execute("""INSERT INTO qq_official_endpoints(endpoint_key,account_key,kind,native_conversation_id,
                canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                VALUES ('TEST_EP','TEST_ACCOUNT','group','TEST_GROUP','TEST_SCOPE','active','present',1,1)""")
        migration.migrate(schema, '0113_group_attention_scheduler')
        with migration.connect(schema) as conn:
            self.assertEqual(conn.execute('SELECT attention_cursor,attention_end,attention_next_at FROM qq_official_endpoints').fetchone(), (0, 0, None))
            conn.execute('UPDATE qq_official_endpoints SET attention_end=100,attention_next_at=200')
        with self.assertRaisesRegex(RuntimeError, 'pending_group_attention_requires_drain'):
            migration.migrate(schema, '0112_proactive_attention', downgrade=True)
        with migration.connect(schema) as conn:
            conn.execute('UPDATE qq_official_endpoints SET attention_cursor=attention_end')
        migration.migrate(schema, '0112_proactive_attention', downgrade=True)
        with migration.connect(schema) as conn:
            self.assertEqual(_schema_snapshot(conn, schema), before)
            self.assertEqual(conn.execute('SELECT endpoint_key FROM qq_official_endpoints').fetchone(), ('TEST_EP',))
