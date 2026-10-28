"""Actual 0063 upgrades in owned PostgreSQL schemas preserve legacy deliveries.

Uses TEST_POSTGRES_DSN with the shared loopback test-database validator; no fixed
schema is upgraded/reset and no production configuration is imported.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import unittest
import uuid

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
from psycopg import sql

from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.postgres_fixture import validate_test_database

ROOT = Path(__file__).resolve().parents[1]

BASE = '0062_qq_official_inbound'
HEAD = '0063_qq_official_outbox'


def checked_test_dsn():
    dsn = os.environ.get('TEST_POSTGRES_DSN', '').strip()
    validate_test_database(dsn)
    return dsn


def validate_owned_schema(schema):
    if not re.fullmatch(r'qq_outbox_test_\d+_[0-9a-f]{12}', schema):
        raise RuntimeError('Refusing to modify a schema not owned by this migration test')


def unique(prefix='TEST_'):
    return prefix + uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def migration_environment(schema, dsn):
    validate_owned_schema(schema)
    keys = {'AI_POSTGRES_DSN': dsn, 'AI_POSTGRES_SCHEMA': schema, 'PYTHON_DOTENV_DISABLED': '1'}
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def config():
    value = Config()
    value.set_main_option('script_location', str(ROOT / 'migrations'))
    return value


def migrate(schema, revision, *, downgrade=False):
    if any(os.environ.get(name) for name in ('PGHOSTADDR', 'PGSERVICE', 'PGSERVICEFILE')):
        raise RuntimeError('Migration tests reject inherited PostgreSQL routing overrides')
    with migration_environment(schema, checked_test_dsn()):
        (command.downgrade if downgrade else command.upgrade)(config(), revision)


def owned_schema():
    return f'qq_outbox_test_{os.getpid()}_{uuid.uuid4().hex[:12]}'


def connect(schema):
    connection = psycopg.connect(checked_test_dsn())
    connection.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(schema)))
    connection.commit()
    return connection


def drop(schema):
    validate_owned_schema(schema)
    with psycopg.connect(checked_test_dsn()) as connection:
        connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def insert(connection, table, values, returning=None):
    query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, values)),
        sql.SQL(',').join(sql.Placeholder() for _ in values),
    )
    if returning:
        query += sql.SQL(' RETURNING {}').format(sql.Identifier(returning))
    cursor = connection.execute(query, tuple(values.values()))
    return cursor.fetchone()[0] if returning else None


def legacy_delivery(connection, **updates):
    values = dict(idempotency_key=unique(), source_scope_key='onebot-v11:group:TEST_LEGACY',
                  target_platform='onebot-v11', target_kind='group', target_native_conversation_id='123',
                  body_json='{"v":1,"nodes":[]}', content_fingerprint='legacy', status='pending',
                  next_attempt_at=100, created_at=100, updated_at=100)
    values.update(updates)
    return insert(connection, 'deliveries', values, 'delivery_id')


class OfficialOutboxMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = owned_schema()
        cls.addClassCleanup(drop, cls.schema)
        migrate(cls.schema, BASE)
        with connect(cls.schema) as connection:
            cls.old_id = legacy_delivery(connection, idempotency_key='TEST_PREEXISTING_LEGACY')
            cls.old_attempt = insert(connection, 'delivery_attempts', dict(
                delivery_id=cls.old_id, attempt=1, state='sending', detail='legacy evidence', started_at=100,
            ), 'attempt_id')
        migrate(cls.schema, HEAD)

    def setUp(self):
        self.connection = connect(self.schema)
        self.addCleanup(self.connection.close)
        self.addCleanup(self.connection.rollback)

    def test_repository_has_one_head_matching_runtime_schema(self):
        scripts = ScriptDirectory.from_config(config())
        self.assertEqual(scripts.get_heads(), [HEAD_REVISION])
        self.assertEqual(scripts.get_revision(HEAD).down_revision, BASE)

    def rejects(self, callback):
        with self.assertRaises(psycopg.IntegrityError):
            with self.connection.transaction():
                callback()

    def endpoint(self, *, app='TEST_M3_APP', environment='test', native='TEST_SAME_OPENID', kind='private'):
        account = AccountKey('qq-official', app, environment)
        self.connection.execute('''INSERT INTO qq_official_accounts
            (account_key,enabled,gateway_state,created_at,updated_at) VALUES (%s,TRUE,'idle',100,100)
            ON CONFLICT(account_key) DO NOTHING''', (account.key,))
        endpoint = EndpointKey(account, kind, native)
        self.connection.execute('''INSERT INTO qq_official_endpoints
            (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,'active','present',100,100) ON CONFLICT(endpoint_key) DO NOTHING''',
            (endpoint.key, account.key, kind, native, endpoint.scope.key))
        return endpoint

    def actor(self, endpoint):
        if endpoint.kind == 'private':
            return ActorKey(endpoint.account, 'c2c_user', '', endpoint.native_conversation_id)
        return ActorKey(endpoint.account, 'group_member', endpoint.native_conversation_id, 'TEST_SAME_OPENID')

    def challenge(self, endpoint):
        actor = self.actor(endpoint)
        grant_id = unique('TEST_GRANT_')
        insert(self.connection, 'qq_official_owner_grants', dict(
            grant_id=grant_id, account_key=endpoint.account.key, c2c_actor_key=actor.key,
            source='config', source_ref='TEST_CONFIG', state='active', created_at=100, updated_at=100,
        ))
        challenge_id = unique('TEST_CHALLENGE_')
        insert(self.connection, 'qq_official_binding_challenges', dict(
            challenge_id=challenge_id, account_key=endpoint.account.key, issuer_actor_key=actor.key,
            issuer_endpoint_key=endpoint.key, owner_grant_id=grant_id, owner_revision=1,
            code_hmac='', expires_at=300, state='sending', created_at=100, updated_at=100, delivery_epoch=1,
        ))
        return challenge_id

    def plan(self, endpoint, *, ephemeral=False, **updates):
        plan_id = unique('TEST_PLAN_')
        challenge_id = updates.pop('challenge_id', None)
        if ephemeral and challenge_id is None:
            challenge_id = self.challenge(endpoint)
        values = dict(plan_id=plan_id, idempotency_key='qqp1:' + digest(plan_id),
                      account_key=endpoint.account.key, endpoint_key=endpoint.key,
                      requester_actor_key=self.actor(endpoint).key, source_scope_key=endpoint.scope.key,
                      endpoint_generation=1, route_epoch=1, capability_revision=1,
                      purpose='binding_otp' if ephemeral else 'final',
                      payload_mode='ephemeral' if ephemeral else 'persistent',
                      manifest_json='{"v":1,"ephemeral":true}' if ephemeral else '{"v":1,"parts":[]}',
                      manifest_digest='a' * 64, digest_scheme='hmac_sha256_v1' if ephemeral else 'sha256',
                      digest_key_id='TEST_KEY_V1' if ephemeral else None, part_count=1,
                      expires_at=200, priority=0, challenge_id=challenge_id,
                      challenge_expires_at=300 if ephemeral else None,
                      created_at=100)
        values.update(updates)
        insert(self.connection, 'delivery_plans', values)
        return values

    def delivery(self, endpoint, *, plan=None, **updates):
        plan = plan or self.plan(endpoint)
        values = dict(idempotency_key='qqd1:' + digest(unique()), source_scope_key=endpoint.scope.key,
                      target_platform='qq-official', target_kind=endpoint.kind,
                      target_native_conversation_id=endpoint.native_conversation_id,
                      body_json='{"v":1,"nodes":[]}', content_fingerprint='' if plan['payload_mode']=='ephemeral' else 'normal',
                      status='pending', next_attempt_at=100, created_at=100, updated_at=100,
                      plan_id=plan['plan_id'], part_index=0, account_key=endpoint.account.key, endpoint_key=endpoint.key,
                      requester_actor_key=self.actor(endpoint).key, endpoint_generation=1, route_epoch=1,
                      capability_revision=1, purpose=plan['purpose'], payload_mode=plan['payload_mode'], variant='primary',
                      payload_digest='a' * 64, digest_scheme=plan['digest_scheme'], digest_key_id=plan['digest_key_id'],
                      priority=0, expires_at=200)
        values.update(updates)
        return insert(self.connection, 'deliveries', values, 'delivery_id')

    def window(self, endpoint):
        value = unique('TEST_WINDOW_')
        insert(self.connection, 'qq_official_reply_windows', dict(
            window_id=value, account_key=endpoint.account.key, endpoint_key=endpoint.key,
            endpoint_generation=1, route_epoch=1, credential_kind='msg_id', credential_value=unique(),
            issued_at=100, received_at=100, expires_at=400, max_replies=5, state='active', created_at=100, updated_at=100,
        ))
        return value

    def attempt(self, endpoint, delivery_id, *, window=None, seq=None, ephemeral=False, **updates):
        values = dict(delivery_id=delivery_id, attempt=1, state='reserved', started_at=110,
                      account_key=endpoint.account.key, endpoint_key=endpoint.key, fencing_epoch=1,
                      endpoint_generation=1, route_epoch=1, capability_revision=1,
                      send_mode='passive' if window else 'proactive', window_id=window, msg_seq=seq,
                      window_charge_state='reserved' if window else 'none',
                      payload_mode='ephemeral' if ephemeral else 'persistent', payload_digest='b' * 64,
                      digest_scheme='hmac_sha256_v1' if ephemeral else 'sha256',
                      digest_key_id='TEST_KEY_V1' if ephemeral else None,
                      wire_payload_json=None if ephemeral else '{"msg_type":0,"content":"synthetic"}',
                      send_by_at=190, acceptance='unknown')
        values.update(updates)
        return insert(self.connection, 'delivery_attempts', values, 'attempt_id')

    def quota(self, endpoint, *, global_scope=False, **updates):
        values = dict(quota_key=digest(unique()), account_key=endpoint.account.key,
                      endpoint_key=None if global_scope else endpoint.key, scene='c2c', send_mode='proactive',
                      metric='endpoint_daily', period_start_at=100, period_end_at=None, reset_verified=False,
                      limit_count=1000, policy_revision=1, evidence_code='test_fixed_contract', created_at=100, updated_at=100)
        values.update(updates)
        insert(self.connection, 'qq_official_quota_periods', values)
        return values

    def test_existing_legacy_rows_and_new_legacy_inserts_remain_valid(self):
        row = self.connection.execute('SELECT status,account_key,official_owned,auth_retries_used FROM deliveries WHERE delivery_id=%s', (self.old_id,)).fetchone()
        self.assertEqual(row, ('pending', None, False, 0))
        self.assertEqual(self.connection.execute('SELECT detail,official_owned FROM delivery_attempts WHERE attempt_id=%s', (self.old_attempt,)).fetchone(), ('legacy evidence', False))
        for state in ('pending', 'sending', 'ambiguous', 'committed', 'failed', 'cancelled'):
            self.assertIsInstance(legacy_delivery(self.connection, status=state), int)
        self.rejects(lambda: legacy_delivery(self.connection, status='accepted'))

    def test_namespace_foreign_keys_cover_same_openid_accounts_environments_and_kinds(self):
        first = self.endpoint()
        for endpoint in (self.endpoint(app='TEST_OTHER_APP'), self.endpoint(environment='sandbox'), self.endpoint(kind='group')):
            plan = self.plan(endpoint)
            self.rejects(lambda: self.delivery(first, plan=plan))
        left, right = self.delivery(first), self.delivery(self.endpoint(native='TEST_OTHER_USER'))
        self.rejects(lambda: self.attempt(first, right))
        self.rejects(lambda: insert(self.connection, 'delivery_attempts', dict(delivery_id=left, attempt=1, state='sending', started_at=100)))
        legacy = legacy_delivery(self.connection)
        self.rejects(lambda: self.attempt(first, legacy))

    def test_current_attempt_cas_reference_cannot_point_to_another_part(self):
        endpoint = self.endpoint()
        left, right = self.delivery(endpoint), self.delivery(endpoint)
        attempt = self.attempt(endpoint, left)
        self.rejects(lambda: self.connection.execute('UPDATE deliveries SET current_attempt_id=%s,claim_epoch=1,status=\'sending\' WHERE delivery_id=%s', (attempt, right)))
        self.connection.execute('UPDATE deliveries SET current_attempt_id=%s,claim_epoch=1,status=\'sending\' WHERE delivery_id=%s', (attempt, left))
        self.connection.execute('UPDATE delivery_attempts SET state=\'accepted\',acceptance=\'confirmed_yes\' WHERE attempt_id=%s', (attempt,))
        self.connection.execute('UPDATE deliveries SET status=\'accepted\' WHERE delivery_id=%s', (left,))
        self.assertEqual(self.connection.execute('SELECT status FROM deliveries WHERE delivery_id=%s', (left,)).fetchone(), ('accepted',))

    def test_one_unresolved_attempt_per_part_including_ambiguous(self):
        endpoint = self.endpoint()
        part = self.delivery(endpoint)
        attempt = self.attempt(endpoint, part)
        self.rejects(lambda: self.attempt(endpoint, part, attempt=2))
        self.connection.execute('UPDATE delivery_attempts SET state=\'ambiguous\' WHERE attempt_id=%s', (attempt,))
        self.rejects(lambda: self.attempt(endpoint, part, attempt=2))
        self.connection.execute('UPDATE delivery_attempts SET state=\'confirmed_unaccepted\',acceptance=\'confirmed_no\' WHERE attempt_id=%s', (attempt,))
        self.assertIsInstance(self.attempt(endpoint, part, attempt=2), int)

    def test_sequence_uniqueness_and_exhausted_sentinel(self):
        endpoint = self.endpoint()
        window = self.window(endpoint)
        first, second = self.delivery(endpoint), self.delivery(endpoint)
        self.attempt(endpoint, first, window=window, seq=65535)
        self.rejects(lambda: self.attempt(endpoint, second, window=window, seq=65535))
        for seq in (0, -1, 65536):
            self.rejects(lambda: self.attempt(endpoint, second, window=window, seq=seq))
        self.connection.execute('UPDATE qq_official_reply_windows SET next_seq=65536 WHERE window_id=%s', (window,))
        self.rejects(lambda: self.connection.execute('UPDATE qq_official_reply_windows SET next_seq=65537 WHERE window_id=%s', (window,)))
        other = self.endpoint(native='TEST_OTHER_USER')
        self.rejects(lambda: self.attempt(endpoint, second, window=self.window(other), seq=1))

    def test_ephemeral_payload_cannot_be_persisted_or_publicly_hashed(self):
        endpoint = self.endpoint()
        plan = self.plan(endpoint, ephemeral=True)
        for update in ({'body_json': '{"code":"123456"}'}, {'content_fingerprint': digest('123456')},
                       {'digest_scheme': 'sha256', 'digest_key_id': None}, {'body_archive_sha256': digest('123456')},
                       {'last_error': 'code 123456'}, {'auth_retries_used': 1}):
            self.rejects(lambda: self.delivery(endpoint, plan=plan, **update))
        part = self.delivery(endpoint, plan=plan)
        self.rejects(lambda: self.attempt(endpoint, part))
        self.rejects(lambda: self.attempt(endpoint, part, ephemeral=True, wire_payload_json='{"content":"123456"}'))
        self.rejects(lambda: self.attempt(endpoint, part, ephemeral=True, detail='123456'))
        self.assertIsInstance(self.attempt(endpoint, part, ephemeral=True), int)
        self.rejects(lambda: self.connection.execute('UPDATE delivery_plans SET manifest_json=\'{"code":"123456"}\' WHERE plan_id=%s', (plan['plan_id'],)))
        self.rejects(lambda: self.connection.execute('UPDATE delivery_plans SET manifest_digest=%s,digest_scheme=\'sha256\',digest_key_id=NULL WHERE plan_id=%s', (digest('123456'), plan['plan_id'])))

    def test_receipts_are_exact_attempt_evidence_and_conflicts_are_retained(self):
        endpoint = self.endpoint()
        attempt = self.attempt(endpoint, self.delivery(endpoint))
        values = dict(receipt_id=unique(), attempt_id=attempt, account_key=endpoint.account.key, endpoint_key=endpoint.key,
                      kind='response', native_message_id='TEST_ACCEPTED_ID', receipt_digest='c' * 64, observed_at=120)
        insert(self.connection, 'delivery_receipts', values)
        self.rejects(lambda: insert(self.connection, 'delivery_receipts', {**values, 'receipt_id': unique()}))
        self.rejects(lambda: insert(self.connection, 'delivery_receipts', {**values, 'receipt_id': unique(), 'endpoint_key': self.endpoint(native='TEST_OTHER_USER').key}))
        insert(self.connection, 'delivery_receipts', {**values, 'receipt_id': unique(), 'native_message_id': 'TEST_CONTRADICTORY_ID'})
        self.assertEqual(self.connection.execute('SELECT count(*) FROM delivery_receipts WHERE attempt_id=%s', (attempt,)).fetchone(), (2,))

    def test_otp_is_bound_to_issuer_endpoint_actor_deadline_and_one_attempt(self):
        endpoint = self.endpoint()
        other = self.endpoint(native='TEST_OTHER_USER')
        group = self.endpoint(kind='group')
        challenge = self.challenge(endpoint)
        self.rejects(lambda: self.plan(other, ephemeral=True, challenge_id=challenge))
        self.rejects(lambda: self.plan(group, ephemeral=True, challenge_id=challenge))
        self.rejects(lambda: self.plan(endpoint, ephemeral=True, challenge_id=challenge,
                                      requester_actor_key=self.actor(other).key))
        self.rejects(lambda: self.plan(endpoint, ephemeral=True, challenge_id=challenge, expires_at=301))
        self.rejects(lambda: self.plan(endpoint, ephemeral=True, challenge_id=challenge,
                                      expires_at=301, challenge_expires_at=302))
        plan = self.plan(endpoint, ephemeral=True, challenge_id=challenge)
        self.rejects(lambda: self.plan(endpoint, ephemeral=True, challenge_id=challenge))
        self.rejects(lambda: self.delivery(endpoint, plan=plan, requester_actor_key=self.actor(other).key))
        part = self.delivery(endpoint, plan=plan)
        attempt = self.attempt(endpoint, part, ephemeral=True)
        self.connection.execute("UPDATE delivery_attempts SET state='confirmed_unaccepted',acceptance='confirmed_no' WHERE attempt_id=%s", (attempt,))
        self.rejects(lambda: self.attempt(endpoint, part, ephemeral=True, attempt=2))

    def test_quota_association_enforces_account_and_endpoint_even_for_null_global_scope(self):
        endpoint = self.endpoint()
        attempt = self.attempt(endpoint, self.delivery(endpoint))
        own, other = self.quota(endpoint), self.quota(self.endpoint(native='TEST_OTHER_USER'))
        values = dict(attempt_id=attempt, quota_key=own['quota_key'], account_key=endpoint.account.key,
                      endpoint_key=endpoint.key, quota_endpoint_key=endpoint.key,
                      units=1, charge_state='reserved', created_at=110, updated_at=110)
        insert(self.connection, 'delivery_attempt_quotas', values)
        self.rejects(lambda: insert(self.connection, 'delivery_attempt_quotas', {**values, 'quota_key': other['quota_key']}))
        self.rejects(lambda: insert(self.connection, 'delivery_attempt_quotas', {**values, 'quota_key': other['quota_key'], 'quota_endpoint_key': None}))
        cross = self.quota(self.endpoint(app='TEST_OTHER_APP'))
        self.rejects(lambda: insert(self.connection, 'delivery_attempt_quotas', {**values, 'quota_key': cross['quota_key']}))
        account = self.quota(endpoint, global_scope=True)
        insert(self.connection, 'delivery_attempt_quotas', {**values, 'quota_key': account['quota_key'], 'quota_endpoint_key': None})
        self.rejects(lambda: self.quota(endpoint, period_end_at=200, reset_verified=False))
        self.rejects(lambda: self.quota(endpoint, period_end_at=None, reset_verified=True))

    def test_probe_marker_is_target_bound_and_not_partial(self):
        endpoint = self.endpoint()
        attempt = self.attempt(endpoint, self.delivery(endpoint))
        other = self.endpoint(native='TEST_OTHER_USER')
        self.rejects(lambda: self.connection.execute('UPDATE qq_official_endpoints SET proactive_probe_attempt_id=%s,proactive_probe_at=110 WHERE endpoint_key=%s', (attempt, other.key)))
        self.rejects(lambda: self.connection.execute('UPDATE qq_official_endpoints SET proactive_probe_attempt_id=%s WHERE endpoint_key=%s', (attempt, endpoint.key)))
        self.connection.execute('UPDATE qq_official_endpoints SET proactive_probe_attempt_id=%s,proactive_probe_at=110 WHERE endpoint_key=%s', (attempt, endpoint.key))

    def test_mode_cooldowns_default_to_zero_and_reject_negative_values(self):
        endpoint = self.endpoint()
        self.assertEqual(self.connection.execute('SELECT passive_blocked_until,proactive_blocked_until FROM qq_official_endpoints WHERE endpoint_key=%s', (endpoint.key,)).fetchone(), (0, 0))
        for column in ('passive_blocked_until', 'proactive_blocked_until'):
            self.rejects(lambda: self.connection.execute(f'UPDATE qq_official_endpoints SET {column}=-1 WHERE endpoint_key=%s', (endpoint.key,)))

    def test_retry_budgets_and_fallback_variant_are_durable_constraints(self):
        endpoint = self.endpoint()
        part = self.delivery(endpoint)
        for clause in ('auth_retries_used=2', 'transient_retries_used=-1', "variant='plain_fallback'", 'format_fallback_used=TRUE'):
            self.rejects(lambda: self.connection.execute(f'UPDATE deliveries SET {clause} WHERE delivery_id=%s', (part,)))
        self.connection.execute("UPDATE deliveries SET variant='plain_fallback',format_fallback_used=TRUE,auth_retries_used=1 WHERE delivery_id=%s", (part,))

    def test_safe_downgrade_rejects_evidence_and_preserves_legacy_on_empty_roundtrip(self):
        schema = owned_schema()
        self.addCleanup(drop, schema)
        migrate(schema, HEAD)
        with connect(schema) as connection:
            old = legacy_delivery(connection, idempotency_key='TEST_ROUNDTRIP_LEGACY')
            account = AccountKey('qq-official', 'TEST_DOWNGRADE_APP', 'test')
            connection.execute('INSERT INTO qq_official_accounts(account_key,created_at,updated_at) VALUES (%s,100,100)', (account.key,))
            insert(connection, 'qq_official_quota_periods', dict(
                quota_key='d' * 64, account_key=account.key, scene='group', send_mode='proactive', metric='test_daily',
                period_start_at=100, limit_count=1000, policy_revision=1, evidence_code='test_fixed_contract', created_at=100, updated_at=100,
            ))
        with self.assertRaisesRegex(RuntimeError, 'Export official delivery evidence'):
            migrate(schema, BASE, downgrade=True)
        with connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(), (HEAD,))
            connection.execute('DELETE FROM qq_official_quota_periods')
            endpoint = EndpointKey(account, 'private', 'TEST_COOLDOWN_USER')
            insert(connection, 'qq_official_endpoints', dict(
                endpoint_key=endpoint.key, account_key=account.key, kind='private',
                native_conversation_id=endpoint.native_conversation_id, canonical_scope_key=endpoint.scope.key,
                passive_blocked_until=200, created_at=100, updated_at=100,
            ))
        with self.assertRaisesRegex(RuntimeError, 'Export official delivery evidence'):
            migrate(schema, BASE, downgrade=True)
        with connect(schema) as connection:
            connection.execute('UPDATE qq_official_endpoints SET passive_blocked_until=0')
        migrate(schema, BASE, downgrade=True)
        with connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT status FROM deliveries WHERE delivery_id=%s', (old,)).fetchone(), ('pending',))
            self.assertEqual(connection.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s AND table_name='deliveries' AND column_name='account_key'", (schema,)).fetchone(), (0,))
        migrate(schema, HEAD)

    def test_invalid_schema_is_rejected_before_ddl(self):
        spec = importlib.util.spec_from_file_location('qq_official_outbox_migration', ROOT / 'migrations/versions/0063_qq_official_outbox.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        previous = os.environ.get('AI_POSTGRES_SCHEMA')
        try:
            os.environ['AI_POSTGRES_SCHEMA'] = 'bad; DROP SCHEMA public'
            with self.assertRaisesRegex(RuntimeError, 'Invalid PostgreSQL schema'):
                module._schema()
        finally:
            if previous is None:
                os.environ.pop('AI_POSTGRES_SCHEMA', None)
            else:
                os.environ['AI_POSTGRES_SCHEMA'] = previous


if __name__ == '__main__':
    unittest.main(verbosity=2)
