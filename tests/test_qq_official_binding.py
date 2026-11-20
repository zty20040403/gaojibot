"""Transactional binding and owner isolation against real PostgreSQL."""
from __future__ import annotations
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingContext, BindingEpochLost, BindingReducer, parse_binding_command
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.postgres_fixture import postgres_for_test

class OfficialBindingTests(unittest.TestCase):
    def setUp(self):
        self.database, self.now = postgres_for_test(self), 1_800_000_000
        self.account = AccountKey('qq-official', 'TEST_APP_' + uuid.uuid4().hex[:12], 'test')
        self.private, self.group = EndpointKey(self.account, 'private', 'TEST_OWNER'), EndpointKey(self.account, 'group', 'TEST_GROUP')
        self.owner = ActorKey(self.account, 'c2c_user', '', 'TEST_OWNER')
        self.member = ActorKey(self.account, 'group_member', 'TEST_GROUP', 'TEST_MEMBER')
        self.attacker = ActorKey(self.account, 'group_member', 'TEST_GROUP', 'TEST_ATTACKER')
        self.authority = OwnerAuthority(account=self.account, owner_c2c_ids=('TEST_OWNER',), clock=lambda: self.now)
        self.token = SimpleNamespace(account_key=self.account.key, epoch=1)
        self.reducer = BindingReducer(b'FAKE-PEPPER-LOCAL-TEST-ONLY-123456789', epoch_guard=self.guard,
                                      authority=self.authority, clock=lambda: self.now)
        with self.tx() as cursor:
            cursor.execute('INSERT INTO qq_official_accounts(account_key,fencing_epoch,created_at,updated_at) VALUES (?,1,?,?)',
                           (self.account.key, self.now, self.now))
            self.seed_endpoint(cursor, self.private)
            self.seed_endpoint(cursor, self.group)
    @contextmanager
    def tx(self):
        connection = self.database.store_connection()
        try:
            yield connection.cursor()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    @staticmethod
    def guard(cursor, token):
        row = cursor.execute('SELECT fencing_epoch FROM qq_official_accounts WHERE account_key=? FOR UPDATE', (token.account_key,)).fetchone()
        if row is None or row['fencing_epoch'] != token.epoch:
            raise BindingEpochLost('Lost test lease')
    def seed_endpoint(self, cursor, endpoint):
        cursor.execute('''INSERT INTO qq_official_endpoints
            (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,
             generation,route_epoch,capability_revision,created_at,updated_at) VALUES (?,?,?,?,?,'unbound','present',1,1,1,?,?)''',
            (endpoint.key, endpoint.account.key, endpoint.kind, endpoint.native_conversation_id, endpoint.scope.key, self.now, self.now))
    def context(self, endpoint=None, actor=None):
        return BindingContext(endpoint or self.private, actor or self.owner, uuid.uuid4().hex, self.now)
    def issue(self):
        with self.tx() as cursor: return self.reducer.issue_owner_code(cursor, context=self.context(), lease_token=self.token)
    def consume(self, code, endpoint=None, actor=None, token=None):
        with self.tx() as cursor:
            return self.reducer.consume_owner_code(cursor, context=self.context(endpoint or self.group, actor or self.member),
                                                    code=code, lease_token=token or self.token)
    def configure(self, *ids):
        authority = OwnerAuthority(account=self.account, owner_c2c_ids=ids, clock=lambda: self.now)
        with self.tx() as cursor: result = authority.apply_trusted_config(cursor, self.account, ids, 'TEST_REVISION')
        self.authority = self.reducer.authority = authority
        return result
    def test_code_binds_exact_actor_without_plaintext_or_history_alias(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.binding.secrets.randbelow', return_value=42): issued = self.issue()
        self.assertEqual((issued.effect.code, issued.effect.expires_at), ('000042', self.now + 300))
        self.assertNotIn('000042', repr(issued))
        self.assertEqual(self.consume('000042').status, 'confirmed')
        self.assertEqual(self.consume('000042').status, 'invalid')
        with self.tx() as cursor:
            account = cursor.execute('SELECT * FROM qq_official_accounts WHERE account_key=?', (self.account.key,)).fetchone()
            endpoint = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,)).fetchone()
            link = cursor.execute('SELECT * FROM qq_official_owner_actor_links').fetchone()
            self.assertNotIn('000042', repr((dict(account), dict(link), [dict(row) for row in cursor.execute('SELECT * FROM admin_audit_log').fetchall()])))
            self.assertEqual((endpoint['canonical_scope_key'], link['configured_owner_actor_key'], link['actor_key']),
                             (self.group.scope.key, self.owner.key, self.member.key))
            self.assertIsNone(link['owner_grant_id'])
            self.assertIsNotNone(self.authority.owner_for_actor(cursor, self.group, self.member))
            cursor.execute('UPDATE qq_official_endpoints SET generation=2 WHERE endpoint_key=?', (self.group.key,))
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.member))
        self.assertEqual(self.consume(self.issue().effect.code, actor=self.attacker).status, 'conflict')
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_endpoints SET binding_state='unbound' WHERE endpoint_key=?", (self.group.key,))
        self.assertEqual(self.consume(self.issue().effect.code, actor=self.attacker).status, 'confirmed')
        with self.tx() as cursor:
            self.assertIsNotNone(self.authority.owner_for_actor(cursor, self.group, self.attacker))
        self.configure()
        self.configure('TEST_OWNER')
        with self.tx() as cursor:
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, self.attacker))
    def test_wrong_expired_reconfigured_epoch_and_membership_codes_fail_once(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.binding.secrets.randbelow', side_effect=(1, 2)):
            first, second = self.issue().effect.code, self.issue().effect.code
        self.assertEqual((self.consume(first).status, self.consume(second).status), ('invalid', 'invalid'))
        code = self.issue().effect.code
        self.now += 300
        self.assertEqual(self.consume(code).status, 'invalid')
        self.now -= 300
        code = self.issue().effect.code
        self.configure()
        self.configure('TEST_OWNER')
        self.assertEqual(self.consume(code).status, 'invalid')
        code = self.issue().effect.code
        with self.tx() as cursor: cursor.execute('UPDATE qq_official_accounts SET fencing_epoch=2 WHERE account_key=?', (self.account.key,))
        self.token.epoch = 2
        self.assertEqual(self.consume(code).status, 'invalid')
        code = self.issue().effect.code
        with self.tx() as cursor: cursor.execute("UPDATE qq_official_endpoints SET membership_state='absent' WHERE endpoint_key=?", (self.group.key,))
        self.assertEqual(self.consume(code).status, 'invalid')
    def test_consumption_is_atomic_across_groups_and_accounts(self):
        other = EndpointKey(self.account, 'group', 'TEST_OTHER_GROUP')
        other_actor = ActorKey(self.account, 'group_member', 'TEST_OTHER_GROUP', 'TEST_OTHER_MEMBER')
        with self.tx() as cursor: self.seed_endpoint(cursor, other)
        code = self.issue().effect.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda pair: self.consume(code, *pair).status, ((self.group, self.member), (other, other_actor))))
        self.assertEqual(sorted(outcomes), ['confirmed', 'invalid'])
        foreign = AccountKey('qq-official', self.account.app_id, 'sandbox')
        group = EndpointKey(foreign, 'group', 'TEST_FOREIGN')
        actor = ActorKey(foreign, 'group_member', 'TEST_FOREIGN', 'TEST_MEMBER')
        code = self.issue().effect.code
        with self.tx() as cursor:
            cursor.execute('INSERT INTO qq_official_accounts(account_key,fencing_epoch,created_at,updated_at) VALUES (?,1,?,?)',
                           (foreign.key, self.now, self.now))
            self.seed_endpoint(cursor, group)
        token = SimpleNamespace(account_key=foreign.key, epoch=1)
        self.assertEqual(self.consume(code, group, actor, token).status, 'invalid')
        with self.tx() as cursor:
            self.assertIsNotNone(cursor.execute('SELECT owner_link_code_hmac FROM qq_official_accounts WHERE account_key=?', (self.account.key,)).fetchone()[0])
    def test_current_legacy_link_blocks_rebinding_but_stale_revision_does_not(self):
        grant = self.configure('TEST_OWNER')[0].grant
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_binding_challenges
                (challenge_id,account_key,issuer_actor_key,issuer_endpoint_key,owner_grant_id,owner_revision,code_hmac,expires_at,
                 state,attempts,created_at,updated_at,consumed_at,delivery_epoch) VALUES ('TEST_C',?,?,?,?,?,'',?,'confirmed',0,?,?,?,1)""",
                (self.account.key, self.owner.key, self.private.key, grant.grant_id, 1, self.now + 300, self.now, self.now, self.now))
            cursor.execute("""INSERT INTO qq_official_binding_candidates
                (candidate_id,challenge_id,account_key,endpoint_key,actor_key,endpoint_generation,version,state,expires_at,
                 created_at,updated_at,grant_submitter_owner,consumed_at) VALUES ('TEST_N','TEST_C',?,?,?,?,1,'confirmed',?,?,?,TRUE,?)""",
                (self.account.key, self.group.key, self.member.key, 1, self.now + 300, self.now, self.now, self.now))
            cursor.execute("""INSERT INTO qq_official_owner_actor_links
                (endpoint_key,actor_key,account_key,owner_grant_id,owner_revision,endpoint_generation,revision,state,candidate_id,created_at,updated_at)
                VALUES (?,?,?,?,?,1,1,'active','TEST_N',?,?)""", (self.group.key, self.member.key, self.account.key, grant.grant_id, 1, self.now, self.now))
        self.assertEqual(self.consume(self.issue().effect.code, actor=self.attacker).status, 'conflict')
        with self.tx() as cursor: cursor.execute('UPDATE qq_official_owner_grants SET revision=2 WHERE grant_id=?', (grant.grant_id,))
        self.assertEqual(self.consume(self.issue().effect.code, actor=self.attacker).status, 'confirmed')
    def test_active_group_rejects_extra_link_even_when_matching_link_exists(self):
        self.assertEqual(self.consume(self.issue().effect.code).status, 'confirmed')
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_owner_actor_links
                (endpoint_key,account_key,actor_key,configured_owner_actor_key,endpoint_generation,revision,state,created_at,updated_at)
                VALUES (?,?,?,?,1,1,'active',?,?)""",
                (self.group.key, self.account.key, self.attacker.key, self.owner.key, self.now, self.now))
        self.assertEqual(self.consume(self.issue().effect.code).status, 'conflict')

    def test_entries_are_sensitive_and_only_configured_private_owner_can_issue(self):
        with self.tx() as cursor:
            for text, status in (('/bind create', 'retired'), ('/bind whoami', 'identity'), ('/绑定群', 'owner_code_issued')):
                self.assertEqual(self.reducer.apply(cursor, context=self.context(), command=parse_binding_command(text), lease_token=self.token).status, status)
        self.configure()
        self.assertEqual(self.issue().status, 'denied')
