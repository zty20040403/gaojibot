"""Real PostgreSQL authority tests; no platform network or production grants.

Run with repository on PYTHONPATH and the existing loopback test database.
The root route/source ports are explicit test doubles: this tests project grant
transactions, not the separate root migration/source assembly.
"""
from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace


from src.bot_storage.database import DatabaseError
from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied, Operation, ProjectAuthority, ProjectRequest
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingContext, BindingReducer, parse_binding_command
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.postgres_fixture import postgres_for_test


class AuthorityFixture(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.now = 1_800_000_000
        self.account = AccountKey('qq-official', 'TEST_APP', 'test')
        self.endpoint = EndpointKey(self.account, 'group', 'TEST_GROUP/a:b')
        self.issuer = ActorKey(self.account, 'group_member', self.endpoint.native_conversation_id, 'TEST_ISSUER')
        self.actor = ActorKey(self.account, 'group_member', self.endpoint.native_conversation_id, 'TEST_MEMBER非数字')
        self.private = EndpointKey(self.account, 'private', 'TEST_OWNER')
        self.owner_actor = ActorKey(self.account, 'c2c_user', '', 'TEST_OWNER')
        self.scope = ConversationScope('onebot-v11', 'group', '101')
        self.owners = OwnerAuthority(clock=lambda: self.now)
        self.token = SimpleNamespace(account_key=self.account.key, epoch=1)
        self.source = object()
        self.source_live = self.route_live = self.fresh = True
        self.recipient_seen = True
        self.ledger = MessageLedger(self.database)
        self.addCleanup(self.ledger.close)
        self.principal = self.ledger.ensure_actor_identity(self.actor, 'Test member')
        self.issuer_principal = self.ledger.ensure_actor_identity(self.issuer, 'Test issuer')
        creator = self.ledger.ensure_principal_identity('onebot-v11', '201', 'Original creator')
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO qq_official_accounts(account_key,fencing_epoch,created_at,updated_at)
                VALUES (?,1,?,?)''', (self.account.key, self.now, self.now))
            for endpoint in (self.endpoint, self.private):
                cursor.execute('''INSERT INTO qq_official_endpoints(endpoint_key,account_key,kind,
                    native_conversation_id,canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                    VALUES (?,?,?,?,?,'unbound','present',?,?)''',
                    (endpoint.key, self.account.key, endpoint.kind, endpoint.native_conversation_id,
                     endpoint.scope.key, self.now, self.now))
            self.owner = self.owners.apply_trusted_config(cursor, self.account, ('TEST_OWNER',), 'TEST_CONFIG')[0].grant
            cursor.execute('''INSERT INTO project_scope_limits(scope_key,kind,quota_bytes,max_projects)
                VALUES (?,'group',1000000,5)''', (self.scope.key,))
            self.project_id = cursor.execute('''INSERT INTO projects(scope_key,creator_principal_id,title,
                backend,quota_bytes,last_active_at,state_changed_at,created_at,updated_at,bot_native_user_id)
                VALUES (?,?,'Test project','vm',100000,?,?,?,?, '301') RETURNING project_id''',
                (self.scope.key, creator, self.now, self.now, self.now, self.now)).fetchone()['project_id']
        reducer = BindingReducer(b'TEST_ONLY_FAKE_PEPPER_12345678901234567890',
                                 epoch_guard=self.guard, authority=self.owners, clock=lambda: self.now)
        with self.tx() as cursor:
            issue = reducer.apply(cursor, context=self.context(self.private, self.owner_actor),
                command=parse_binding_command('/bind create'), lease_token=self.token)
            reducer.mark_issued(cursor, account=self.account, challenge_id=issue.challenge_id,
                                outcome='success', lease_token=self.token)
            candidate = reducer.apply(cursor, context=self.context(self.endpoint, self.issuer),
                command=parse_binding_command(f'/bind submit {issue.challenge_id} {issue.effect.code}'),
                lease_token=self.token)
            confirm = reducer.apply(cursor, context=self.context(self.private, self.owner_actor),
                command=parse_binding_command(f'/bind confirm {candidate.candidate_id} 1 --grant-owner'),
                lease_token=self.token)
            self.assertEqual(confirm.status, 'confirmed')
        self.authority = self.new_authority()
        self.ref = None

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
        row = cursor.execute('SELECT fencing_epoch FROM qq_official_accounts WHERE account_key=? FOR UPDATE',
                             (token.account_key,)).fetchone()
        if row is None or row['fencing_epoch'] != token.epoch:
            raise Denied()

    def context(self, endpoint, actor):
        return BindingContext(endpoint, actor, uuid.uuid4().hex, self.now)

    def route_guard(self, cursor, *, canonical_scope, endpoint, route_epoch, operation):
        return (self.route_live and canonical_scope == self.scope and endpoint == self.endpoint
                and route_epoch == 7 and operation in {'read', 'execute', 'send'})

    def source_guard(self, cursor, *, request, source):
        self.guard(cursor, self.token)
        return self.source_live and source is self.source

    def new_authority(self, **overrides):
        options = dict(source_guard=self.source_guard, route_guard=self.route_guard,
            recipient_evidence=lambda *args, **kwargs: self.recipient_seen,
            fresh_actor_evidence=lambda *args, **kwargs: self.fresh, clock=lambda: self.now)
        return ProjectAuthority(**(options | overrides))

    def request(self, operation=Operation.READ, **changes):
        return ProjectRequest(**(dict(endpoint=self.endpoint, actor=self.actor,
            canonical_scope=self.scope, route_epoch=7, endpoint_route_epoch=2, project_id=self.project_id,
            operation=operation) | changes))

    def issue(self, **changes):
        with self.tx() as cursor:
            self.ref = self.authority.issue_project(cursor, **(dict(
                issuer=self.request(Operation.MANAGE, actor=self.issuer), recipient=self.actor,
                capabilities=frozenset({Operation.READ, Operation.EXECUTE, Operation.DELIVER}),
                grant_id=uuid.uuid4().hex, expires_at=self.now+3600,
                evidence_ref='TEST_AUTHENTICATED_COMMAND', source=self.source) | changes))
        return self.ref

    def require(self, request=None, ref=None, **changes):
        with self.tx() as cursor:
            return self.authority.require_project(cursor, **(dict(request=request or self.request(),
                grant=ref or self.ref, source=self.source) | changes))

    def update(self, sql, values=()):
        with self.tx() as cursor:
            cursor.execute(sql, values)

    @contextmanager
    def sql_rejects(self, expected):
        with self.assertRaises(DatabaseError) as caught:
            yield
        self.assertIsInstance(caught.exception.__cause__, expected)
