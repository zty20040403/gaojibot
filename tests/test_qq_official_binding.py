"""Transactional binding and owner isolation against real isolated PostgreSQL."""
from __future__ import annotations

import hashlib
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import (
    BindingCommand, BindingContext, BindingEpochLost, BindingReducer, parse_binding_command,
)
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease, LeaseLost, assert_epoch
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

    def test_raced_code_freezes_actor_reject_reissue_cannot_grant_attacker(self):
        issued, candidate = self.candidate(actor=self.attacker)
        raced = self.apply(f'/bind submit {issued.challenge_id} {issued.effect.code}', endpoint=self.group, actor=self.member)
        self.assertEqual(raced.status, 'invalid')
        preview = self.apply(f'/bind status {issued.challenge_id}')
        self.assertEqual(preview.effect.actual_submitter, self.attacker)
        self.assertEqual(preview.effect.actual_group, self.group)
        with self.tx() as cursor:
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.attacker))
        rejected = self.apply(f'/bind reject {candidate.candidate_id} 1')
        self.assertEqual(rejected.status, 'rejected')
        newer = self.apply('/bind create')
        self.assertNotEqual(newer.challenge_id, issued.challenge_id)
        self.assertEqual(self.apply(f'/bind submit {issued.challenge_id} {issued.effect.code}', endpoint=self.group,
                                    actor=self.attacker).status, 'invalid')
        self.assertEqual(self.row('qq_official_endpoints', 'endpoint_key', self.group.key)['binding_state'], 'unbound')

    def test_default_confirm_admits_only_and_false_to_true_replay_is_conflict(self):
        _, candidate = self.candidate()
        decision = self.apply(f'/bind confirm {candidate.candidate_id} 1')
        self.assertEqual(decision.status, 'confirmed')
        endpoint = self.row('qq_official_endpoints', 'endpoint_key', self.group.key)
        self.assertEqual((endpoint['binding_state'], endpoint['canonical_scope_key'], endpoint['route_epoch']),
                         ('active', self.group.scope.key, 2))
        with self.tx() as cursor:
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.member))
        self.assertEqual(self.apply(f'/bind confirm {candidate.candidate_id} 1').status, 'already_confirmed')
        self.assertEqual(self.apply(f'/bind confirm {candidate.candidate_id} 1 --grant-owner').status, 'conflict')

    def test_explicit_grant_is_scoped_and_source_revocation_survives_config_reload(self):
        _, candidate = self.candidate()
        self.assertEqual(self.apply(f'/bind confirm {candidate.candidate_id} 1 --grant-owner').status, 'confirmed')
        with self.tx() as cursor:
            grant = self.authority.owner_for_actor(cursor, self.group, self.member)
            self.assertIsNotNone(grant)
            self.authority.apply_trusted_config(cursor, self.account, (), 'removed-config')
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.member))
        self.assertEqual(self.bootstrap()[0].status, 'revoked')
        self.assertEqual(self.apply('/bind create').status, 'denied')

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

    def test_restart_keeps_candidate_and_does_not_extend_expiry(self):
        issued, candidate = self.candidate()
        self.reducer = BindingReducer(b'FAKE-PEPPER-LOCAL-TEST-ONLY-123456789', epoch_guard=self.guard,
                                      authority=self.authority, clock=lambda: self.now)
        preview = self.apply(f'/bind status {issued.challenge_id}')
        self.assertEqual((preview.effect.candidate_id, preview.effect.actual_submitter), (candidate.candidate_id, self.member))
        self.now += 601
        self.assertEqual(self.apply(f'/bind confirm {candidate.candidate_id} 1').status, 'expired')
        self.assertEqual(self.row('qq_official_endpoints', 'endpoint_key', self.group.key)['binding_state'], 'unbound')

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

    def test_leave_rejoin_generation_invalidates_candidate(self):
        _, candidate = self.candidate()
        with self.tx() as cursor:
            cursor.execute('UPDATE qq_official_endpoints SET generation=generation+1 WHERE endpoint_key=?', (self.group.key,))
        self.assertEqual(self.apply(f'/bind confirm {candidate.candidate_id} 1 --grant-owner').status, 'stale')
        with self.tx() as cursor:
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.member))

    def test_parallel_confirmations_apply_once(self):
        _, candidate = self.candidate()
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda _: self.apply(f'/bind confirm {candidate.candidate_id} 1').status, range(6)))
        self.assertEqual(statuses.count('confirmed'), 1)
        self.assertEqual(statuses.count('already_confirmed'), 5)
        self.assertEqual(self.row('qq_official_endpoints', 'endpoint_key', self.group.key)['route_epoch'], 2)

    def test_transaction_rollback_preserves_candidate_and_epoch_loss_does_not_commit(self):
        _, candidate = self.candidate()
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.tx() as cursor:
                result = self.reducer.apply(cursor, context=self.context(),
                    command=parse_binding_command(f'/bind confirm {candidate.candidate_id} 1'), lease_token=self.token)
                self.assertEqual(result.status, 'confirmed')
                raise RuntimeError('rollback')
        self.assertEqual(self.row('qq_official_binding_candidates', 'candidate_id', candidate.candidate_id)['state'], 'pending')
        with self.tx() as cursor:
            cursor.execute('UPDATE qq_official_accounts SET fencing_epoch=2 WHERE account_key=?', (self.account.key,))
        with self.assertRaises(BindingEpochLost):
            self.apply(f'/bind confirm {candidate.candidate_id} 1')
        self.assertEqual(self.row('qq_official_endpoints', 'endpoint_key', self.group.key)['binding_state'], 'unbound')

    def test_audit_and_repr_never_contain_code_or_native_ids(self):
        issued, candidate = self.candidate()
        preview = self.apply(f'/bind status {issued.challenge_id}')
        self.apply(f'/bind confirm {candidate.candidate_id} 1 --grant-owner')
        with self.tx() as cursor:
            rows = cursor.execute("SELECT * FROM admin_security_audit WHERE action LIKE ?", ("qq.binding.%",)).fetchall()
        output = repr([dict(row) for row in rows]) + repr(issued) + repr(issued.effect) + repr(preview.effect) + repr(self.reducer)
        for secret in (issued.effect.code, self.owner.native_user_id, self.member.native_user_id, self.group.native_conversation_id):
            self.assertNotIn(secret, output)

    def test_other_account_environment_and_private_actor_cannot_confirm(self):
        issued, candidate = self.candidate()
        other_private = EndpointKey(self.account, 'private', self.member.native_user_id)
        other_actor = ActorKey(self.account, 'c2c_user', '', self.member.native_user_id)
        with self.tx() as cursor:
            self.seed_endpoint(cursor, other_private)
        self.assertEqual(self.apply(f'/bind confirm {candidate.candidate_id} 1 --grant-owner',
                                   endpoint=other_private, actor=other_actor).status, 'invalid')
        for account in (AccountKey('qq-official', self.account.app_id, 'sandbox'),
                        AccountKey('qq-official', self.account.app_id + '_different', 'test')):
            endpoint = EndpointKey(account, 'group', self.group.native_conversation_id)
            actor = ActorKey(account, 'group_member', endpoint.native_conversation_id, self.member.native_user_id)
            with self.tx() as cursor:
                cursor.execute('''INSERT INTO qq_official_accounts(account_key,fencing_epoch,created_at,updated_at)
                    VALUES (?,1,?,?)''', (account.key, self.now, self.now))
                self.seed_endpoint(cursor, endpoint)
                result = self.reducer.apply(cursor, context=self.context(endpoint, actor),
                    command=parse_binding_command(f'/bind submit {issued.challenge_id} {issued.effect.code}'),
                    lease_token=SimpleNamespace(account_key=account.key, epoch=1))
                self.assertEqual(result.status, 'invalid')

    def test_console_checks_real_session_version_and_explicit_bool(self):
        _, candidate = self.candidate()
        token, csrf = uuid.uuid4().hex, uuid.uuid4().hex
        admin_id = uuid.uuid4().hex
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,enabled,version,created_at)
                VALUES (?,?,?,'admin',?,1,1,?)''', (admin_id, admin_id, 'not-a-real-password', str(uuid.uuid4().int)[:12], self.now))
            cursor.execute('INSERT INTO admin_sessions VALUES (?,?,1,?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(), admin_id, hashlib.sha256(csrf.encode()).hexdigest(), self.now + 600, self.now))
            self.assertIsNone(self.authority.authenticate_console(cursor, session_token=token, csrf_token='wrong'))
            auth = self.authority.authenticate_console(cursor, session_token=token, csrf_token=csrf)
            self.assertIsNotNone(auth)
            self.assertEqual(self.reducer.confirm_console(cursor, account=self.account, authorization=auth,
                candidate_id=candidate.candidate_id, candidate_version=1, grant_submitter_owner='true').status, 'syntax')
        with self.tx() as cursor:
            cursor.execute('UPDATE admin_accounts SET version=2 WHERE account_id=?', (admin_id,))
            self.assertEqual(self.reducer.confirm_console(cursor, account=self.account, authorization=auth,
                candidate_id=candidate.candidate_id, candidate_version=1, grant_submitter_owner=True).status, 'denied')

    def test_parser_is_bounded_and_requires_explicit_candidate(self):
        self.assertIsNone(parse_binding_command('ordinary chat'))
        command = parse_binding_command('/bind submit ' + 'SECRET' * 1000)
        self.assertFalse(command.valid)
        self.assertNotIn('SECRET', repr(command))
        self.assertFalse(parse_binding_command('/bind confirm').valid)
        self.assertFalse(parse_binding_command('/bind confirm BN-' + '0' * 32 + ' 1 true').valid)
        self.assertIsInstance(parse_binding_command('/bind create'), BindingCommand)
        padded = parse_binding_command(' ' * 2048 + '/bind submit BC-' + '0' * 32 + ' 483729')
        self.assertIsNotNone(padded)
        self.assertFalse(padded.valid)
        self.assertNotIn('483729', repr(padded))

    def test_trusted_config_rejects_control_format_surrogates_and_oversized_ids(self):
        for value in ('bad\x00id', 'bad\nidentity', 'bad\u200bidentity', 'bad\ud800identity', '😀' * 65):
            with self.subTest(value=ascii(value)), self.tx() as cursor:
                result = self.authority.apply_trusted_config(cursor, self.account, (value,), 'revision')
                self.assertEqual(result[0].status, 'unavailable')
        self.assertEqual(self.apply('/bind create').status, 'denied')

    def test_console_valid_confirmation_and_versioned_owner_regrant(self):
        _, candidate = self.candidate()
        token, csrf, admin_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,enabled,version,created_at)
                VALUES (?,?,?,'admin',?,1,1,?)''', (admin_id, admin_id, 'not-a-real-password', str(uuid.uuid4().int)[:12], self.now))
            cursor.execute('INSERT INTO admin_sessions VALUES (?,?,1,?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(), admin_id, hashlib.sha256(csrf.encode()).hexdigest(), self.now + 600, self.now))
            auth = self.authority.authenticate_console(cursor, session_token=token, csrf_token=csrf)
            result = self.reducer.confirm_console(cursor, account=self.account, authorization=auth,
                candidate_id=candidate.candidate_id, candidate_version=1, grant_submitter_owner=True)
            self.assertEqual(result.status, 'confirmed')
            self.assertIsNotNone(self.authority.owner_for_actor(cursor, self.group, self.member))
            revoked = self.authority.set_console_owner(cursor, authorization=auth, actor=self.owner,
                                                       active=False, expected_revision=1)
            self.assertEqual((revoked.status, revoked.grant.revision), ('revoked', 2))
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.member))
            self.assertEqual(self.authority.set_console_owner(cursor, authorization=auth, actor=self.owner,
                active=True, expected_revision=1).status, 'conflict')
            regranted = self.authority.set_console_owner(cursor, authorization=auth, actor=self.owner,
                                                        active=True, expected_revision=2)
            self.assertEqual((regranted.status, regranted.grant.revision), ('regranted', 3))
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.member))

    def test_code_not_persisted_and_new_reducer_can_validate_same_hmac(self):
        issued = self.issue()
        row = self.row('qq_official_binding_challenges', 'challenge_id', issued.challenge_id)
        self.assertNotEqual(row['code_hmac'], issued.effect.code)
        self.assertNotIn(issued.effect.code, repr(row))
        self.reducer = BindingReducer(b'FAKE-PEPPER-LOCAL-TEST-ONLY-123456789', epoch_guard=self.guard,
                                      authority=self.authority, clock=lambda: self.now)
        result = self.apply(f'/bind submit {issued.challenge_id} {issued.effect.code}', endpoint=self.group, actor=self.member)
        self.assertEqual(result.status, 'pending')
        self.assertEqual(self.row('qq_official_binding_challenges', 'challenge_id', issued.challenge_id)['code_hmac'], '')

    def test_real_lease_fences_binding_and_new_holder_confirms_persisted_candidate(self):
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_accounts SET enabled=TRUE,gateway_state='idle' WHERE account_key=?",
                           (self.account.key,))
        first = DedicatedAccountLease(self.database.dsn, self.account, schema=self.database.schema,
                                      clock=lambda: self.now)
        self.addCleanup(first.close)
        self.token = first.acquire()
        self.assertIsNotNone(self.token)
        self.reducer = BindingReducer(b'FAKE-PEPPER-LOCAL-TEST-ONLY-123456789', epoch_guard=assert_epoch,
                                      authority=self.authority, clock=lambda: self.now)
        _, candidate = self.candidate()
        old_epoch = self.token.epoch
        first.close()
        with self.assertRaises(LeaseLost):
            self.apply(f'/bind confirm {candidate.candidate_id} 1')
        self.assertEqual(self.row('qq_official_binding_candidates', 'candidate_id', candidate.candidate_id)['state'], 'pending')
        second = DedicatedAccountLease(self.database.dsn, self.account, schema=self.database.schema,
                                       clock=lambda: self.now)
        self.addCleanup(second.close)
        self.token = second.acquire()
        self.assertGreater(self.token.epoch, old_epoch)
        self.assertEqual(self.apply(f'/bind confirm {candidate.candidate_id} 1').status, 'confirmed')
