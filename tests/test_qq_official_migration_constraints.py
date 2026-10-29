"""Actual Alembic round trips and combined M5 integrity boundaries."""
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
from unittest.mock import patch

from alembic import command
from alembic.config import Config
import psycopg

from src.bot_storage.database import DatabaseError
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from tests.qq_official_outbox_fixture import OutboxFixture


class MigrationConstraintTests(OutboxFixture):
    def migrate(self, direction, revision):
        config = Config()
        config.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'migrations'))
        with patch.dict(os.environ, AI_POSTGRES_DSN=self.database.dsn, AI_POSTGRES_SCHEMA=self.database.schema):
            getattr(command, direction)(config, revision)

    def schema_snapshot(self):
        return (
            [tuple(row) for row in self.query('''SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
                FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=?
                ORDER BY c.relname,k.conname''', (self.database.schema,))],
            [tuple(row) for row in self.query('''SELECT table_name,column_name,is_nullable,column_default,
                data_type,generation_expression FROM information_schema.columns WHERE table_schema=?
                ORDER BY table_name,ordinal_position''', (self.database.schema,))],
        )

    def insert(self, table, values):
        with self.tx() as cursor:
            return cursor.execute('INSERT INTO '+table+' ('+','.join(values)+') VALUES ('+
                ','.join('?' for _ in values)+') RETURNING *', tuple(values.values())).fetchone()

    @contextmanager
    def rejected(self, error):
        with self.assertRaises(DatabaseError) as caught:
            yield
        self.assertIsInstance(caught.exception.__cause__, error)

    def baseline(self, state='ready'):
        account = AccountKey('onebot-v11', '301', 'test')
        return self.insert('migration_runtime_baselines', dict(account_key=account.key,
            runtime_id='a'*32, revision=1, backend_pid=1, advisory_key=1,
            deployment_digest='b'*64, expires_at=self.now+600, ready_at=self.now, state=state))

    def journal(self):
        self.baseline()
        source = EndpointKey(AccountKey('onebot-v11', '301', 'test'), 'group', '700')
        scope = ConversationScope('onebot-v11', 'group', '700', bot_native_user_id='301')
        route_id, migration_id = secrets.token_hex(16), secrets.token_hex(16)
        self.insert('admin_accounts', dict(account_id='TEST_SCHEMA_ADMIN', username='test-schema-admin',
            password_hash='TEST_HASH', role='admin', qq_id='42', enabled=1, version=1, created_at=self.now))
        self.insert('migration_routes', dict(route_id=route_id, source_scope_key=scope.key,
            source_endpoint_key=source.key, target_scope_key=self.endpoint.scope.key,
            target_endpoint_key=self.endpoint.key, source_account_key=source.account.key,
            target_account_key=self.account.key, target_generation=1, target_endpoint_epoch=1,
            epoch=0, phase='planned', active_endpoint_key=source.key,
            baseline_runtime_id='a'*32, baseline_revision=1))
        self.insert('migration_journals', dict(migration_id=migration_id, route_id=route_id,
            preview_digest='c'*64, preview_json='{}', creator_id='TEST_SCHEMA_ADMIN', creator_version=1,
            deadline=self.now+600, created_at=self.now))
        return migration_id, route_id, source, scope

    def item_values(self, migration_id, **changes):
        return dict(item_id=secrets.token_hex(16), migration_id=migration_id, kind='reminder',
            source_ref=secrets.token_hex(8), source_revision=1, classification='terminal',
            export_json='{}', export_digest='d'*64, created_at=self.now) | changes

    def test_empty_upgrade_downgrade_and_second_upgrade_restore_exact_structures(self):
        head = self.schema_snapshot()
        self.migrate('downgrade', '0065_qq_official_files')
        old = self.schema_snapshot()
        self.assertIsNone(self.query("SELECT to_regclass('migration_routes')")[0][0])
        self.migrate('upgrade', 'head')
        self.assertEqual(self.schema_snapshot(), head)
        self.migrate('downgrade', '0065_qq_official_files')
        self.assertEqual(self.schema_snapshot(), old)
        self.migrate('upgrade', 'head')

    def test_legacy_delivery_survives_real_migration_roundtrip(self):
        self.migrate('downgrade', '0065_qq_official_files')
        store = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(store.close)
        scope = ConversationScope('onebot', 'group', '700')
        delivery, _ = store.enqueue(idempotency_key='TEST_OLD_DELIVERY', source_scope_key=scope.key,
            target_scope=scope, body=MessageBody((TextNode(0, 'TEST unchanged content'),)), now=self.now)
        original = dict(self.query('SELECT * FROM deliveries WHERE delivery_id=?', (delivery.delivery_id,))[0])
        self.migrate('upgrade', 'head')
        self.migrate('downgrade', '0065_qq_official_files')
        self.assertEqual(dict(self.query('SELECT * FROM deliveries WHERE delivery_id=?', (delivery.delivery_id,))[0]), original)
        self.migrate('upgrade', 'head')

    def test_downgrade_preserves_even_revoked_baseline_evidence(self):
        record = self.baseline('revoked')
        before = self.schema_snapshot()
        with self.assertRaisesRegex(RuntimeError, 'migration_evidence_requires_export'):
            self.migrate('downgrade', '0065_qq_official_files')
        self.assertEqual(self.schema_snapshot(), before)
        self.assertEqual(dict(self.query('SELECT * FROM migration_runtime_baselines')[0]), dict(record))
        self.assertEqual(self.query('SELECT version_num FROM alembic_version')[0][0], '0066_qq_official_migration')

    def test_partial_mapping_cannot_pass_a_nullable_sql_check(self):
        migration_id, _, _, _ = self.journal()
        complete = dict(mapping_id='e'*32, source_actor_key='TEST_SOURCE_ACTOR',
            target_actor_key=self.actor.key, mapping_revision=1, mapping_expires_at=self.now+100)
        for field in complete:
            with self.subTest(field=field), self.rejected(psycopg.errors.CheckViolation):
                self.insert('migration_items', self.item_values(migration_id, **(complete | {field: None})))

    def test_item_cannot_bind_a_different_items_job(self):
        from src.plugins.ai_chat.storage.jobs import DurableJobStore
        migration_id, _, _, _ = self.journal()
        jobs = DurableJobStore(self.database)
        self.addCleanup(jobs.close)
        job, _ = jobs.enqueue(kind='TEST_SCHEMA_JOB', payload={}, idempotency_key=secrets.token_hex(16), now=self.now)
        first = self.insert('migration_items', self.item_values(migration_id, job_id=job.job_id, expected_job_kind=job.kind))
        with self.rejected(psycopg.errors.UniqueViolation):
            self.insert('migration_items', self.item_values(migration_id, job_id=job.job_id, expected_job_kind=job.kind))
        with self.rejected(psycopg.errors.CheckViolation):
            self.query('UPDATE migration_items SET expected_job_kind=NULL WHERE item_id=?', (first['item_id'],))

    def test_history_grant_must_use_the_exact_original_conversation(self):
        migration_id, route_id, _, scope = self.journal()
        ledger = MessageLedger(self.database)
        self.addCleanup(ledger.close)
        other = ConversationScope('onebot-v11', 'group', '701', bot_native_user_id='301')
        for current in (scope, other):
            ledger.record_message(current, native_message_id='TEST_'+current.native_conversation_id,
                sender_native_user_id='42', sender_display='TEST author',
                body=MessageBody((TextNode(0,'TEST public history'),)), occurred_at=self.now)
        wrong = self.query('SELECT conversation_id FROM conversations WHERE scope_key=?', (other.key,))[0][0]
        values = dict(grant_id=secrets.token_hex(16), migration_id=migration_id, route_id=route_id,
            route_epoch=1, source_scope_key=scope.key, source_conversation_id=wrong,
            target_endpoint_key=self.endpoint.key, target_scope_key=self.endpoint.scope.key,
            target_endpoint_generation=1, target_endpoint_route_epoch=1, source_floor=1,
            target_floor=1, source_cutoff=self.now, manifest_json='{}', manifest_digest='f'*64,
            state='prepared', created_at=self.now)
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.insert('qq_migration_public_history_grants', values)

    def test_persisted_worker_source_requires_a_real_job_binding(self):
        record = self.baseline()
        with self.rejected(psycopg.errors.CheckViolation):
            self.insert('migration_work_sources', dict(source_id=secrets.token_hex(16),
                account_key=record['account_key'], endpoint_key='TEST_ENDPOINT', canonical_scope_key='TEST_SCOPE',
                source_kind='job', source_ref='TEST_JOB', source_revision=1, source_digest='a'*64,
                route_epoch=0, baseline_runtime_id=record['runtime_id'], state='ready',
                created_at=self.now, updated_at=self.now, deadline_at=self.now+100))
