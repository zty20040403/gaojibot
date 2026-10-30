"""Transactional binding and owner isolation against real isolated PostgreSQL."""
from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import (
    BindingContext, BindingEpochLost, BindingReducer, parse_binding_command,
)
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.postgres_fixture import postgres_for_test


class OfficialBindingTests(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.now = 1_800_000_000
        self.account = AccountKey('qq-official', 'TEST_APP_' + uuid.uuid4().hex[:12], 'test')
        self.private = EndpointKey(self.account, 'private', 'owner-c2c-sensitive')
        self.owner = ActorKey(self.account, 'c2c_user', '', self.private.native_conversation_id)
        self.group = EndpointKey(self.account, 'group', 'group-sensitive')
        self.member = ActorKey(self.account, 'group_member', self.group.native_conversation_id, 'member-sensitive')
        self.attacker = ActorKey(self.account, 'group_member', self.group.native_conversation_id, 'attacker-sensitive')
        self.authority = OwnerAuthority(clock=lambda: self.now)
        self.token = SimpleNamespace(account_key=self.account.key, epoch=1)
        self.reducer = BindingReducer(b'FAKE-PEPPER-LOCAL-TEST-ONLY-123456789', epoch_guard=self.guard,
                                      authority=self.authority, clock=lambda: self.now)
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO qq_official_accounts(account_key,fencing_epoch,created_at,updated_at)
                VALUES (?,1,?,?)''', (self.account.key, self.now, self.now))
            self.seed_endpoint(cursor, self.private)
            self.seed_endpoint(cursor, self.group)

    @contextmanager
    def tx(self):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def guard(cursor, token):
        row = cursor.execute('SELECT fencing_epoch FROM qq_official_accounts WHERE account_key=? FOR UPDATE',
                             (token.account_key,)).fetchone()
        if row is None or row['fencing_epoch'] != token.epoch:
            raise BindingEpochLost('Lost test lease')

    def seed_endpoint(self, cursor, endpoint):
        cursor.execute('''INSERT INTO qq_official_endpoints
            (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,
             membership_state,generation,route_epoch,capability_revision,created_at,updated_at)
            VALUES (?,?,?,?,?,'unbound','present',1,1,1,?,?)''',
            (endpoint.key, endpoint.account.key, endpoint.kind, endpoint.native_conversation_id,
             endpoint.scope.key, self.now, self.now))

    def context(self, endpoint=None, actor=None):
        return BindingContext(endpoint or self.private, actor or self.owner, uuid.uuid4().hex, self.now)

    def apply(self, text, *, endpoint=None, actor=None):
        with self.tx() as cursor:
            return self.reducer.apply(cursor, context=self.context(endpoint, actor),
                                      command=parse_binding_command(text), lease_token=self.token)

    def bootstrap(self):
        with self.tx() as cursor:
            return self.authority.apply_trusted_config(cursor, self.account,
                                                       (self.owner.native_user_id,), 'test-config-revision')

    def issue(self):
        self.bootstrap()
        decision = self.apply('/bind create')
        self.assertEqual(decision.status, 'sending')
        with self.tx() as cursor:
            marked = self.reducer.mark_issued(cursor, account=self.account, challenge_id=decision.challenge_id,
                                              outcome='success', lease_token=self.token)
        self.assertEqual(marked.status, 'issued')
        return decision

    def candidate(self, *, actor=None):
        issued = self.issue()
        result = self.apply(f'/bind submit {issued.challenge_id} {issued.effect.code}',
                            endpoint=self.group, actor=actor or self.member)
        self.assertEqual(result.status, 'pending')
        return issued, result

    def row(self, table, field, value):
        self.assertIn(table, {'qq_official_binding_challenges', 'qq_official_binding_candidates',
                             'qq_official_endpoints', 'qq_official_owner_grants'})
        self.assertIn(field, {'challenge_id', 'candidate_id', 'endpoint_key', 'grant_id'})
        with self.tx() as cursor:
            row = cursor.execute(f'SELECT * FROM {table} WHERE {field}=?', (value,)).fetchone()
            return dict(row) if row else None

    def test_first_contact_does_not_enroll_owner_and_config_is_idempotent(self):
        self.assertEqual(self.apply('/bind create').status, 'denied')
        self.assertEqual(self.apply('/bind whoami').status, 'identity')
        self.assertEqual(self.apply('/bind create').status, 'denied')
        self.assertEqual(self.bootstrap()[0].status, 'created')
        self.assertEqual(self.bootstrap()[0].status, 'already_active')




    def test_wrong_codes_and_unknown_challenges_persist_attempt_limits(self):
        issued = self.issue()
        wrong = '000000' if issued.effect.code != '000000' else '000001'
        for _ in range(5):
            self.assertEqual(self.apply(f'/bind submit {issued.challenge_id} {wrong}', endpoint=self.group,
                                        actor=self.member).status, 'invalid')
        row = self.row('qq_official_binding_challenges', 'challenge_id', issued.challenge_id)
        self.assertEqual((row['state'], row['attempts'], row['code_hmac']), ('locked', 5, ''))
        missing = 'BC-' + 'f' * 32
        for _ in range(5):
            self.assertEqual(self.apply(f'/bind submit {missing} 123456', endpoint=self.group, actor=self.attacker).status, 'invalid')
        self.assertEqual(self.apply(f'/bind submit {missing} 123456', endpoint=self.group, actor=self.attacker).status, 'limited')


    def test_send_failure_unknown_expiry_and_restart_cancel_without_reemit(self):
        self.bootstrap()
        for outcome in ('failed', 'unknown'):
            decision = self.apply('/bind create')
            with self.tx() as cursor:
                self.assertEqual(self.reducer.mark_issued(cursor, account=self.account, challenge_id=decision.challenge_id,
                    outcome=outcome, lease_token=self.token).status, 'cancelled')
        decision = self.apply('/bind create')
        with self.tx() as cursor:
            self.assertEqual(self.reducer.recover_sending(cursor, account=self.account, lease_token=self.token), 1)
        self.assertEqual(self.row('qq_official_binding_challenges', 'challenge_id', decision.challenge_id)['code_hmac'], '')
        self.now += 601
        decision = self.apply('/bind create')
        self.now += 601
        with self.tx() as cursor:
            self.assertEqual(self.reducer.mark_issued(cursor, account=self.account, challenge_id=decision.challenge_id,
                outcome='success', lease_token=self.token).status, 'cancelled')








    def test_trusted_config_rejects_control_format_surrogates_and_oversized_ids(self):
        for value in ('bad\x00id', 'bad\nidentity', 'bad\u200bidentity', 'bad\ud800identity', '😀' * 65):
            with self.subTest(value=ascii(value)), self.tx() as cursor:
                result = self.authority.apply_trusted_config(cursor, self.account, (value,), 'revision')
                self.assertEqual(result[0].status, 'unavailable')
        self.assertEqual(self.apply('/bind create').status, 'denied')
