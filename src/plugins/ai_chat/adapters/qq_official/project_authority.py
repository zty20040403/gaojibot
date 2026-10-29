"""Offline M5 project grant adapter. No commit, network, or implicit authority.

Host-only callers supply real source handles; the default source/route/evidence
callbacks deny. Constructing the dataclasses cannot establish authentication.
Every effect calls require_project in its OWN transaction. A returned snapshot
must not be cached as a capability or used to authorize a later side effect.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps

from psycopg import Error as DatabaseError

from src.bot_storage.database import DatabaseError as StoreDatabaseError
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


class Denied(PermissionError):
    def __init__(self):
        super().__init__('project_authority_denied')


class RecipientEvidenceUnavailable(Denied):
    """Current authority passed; only recent recipient presence is missing.

    This is not permission or a renewable deadline. Physical send boundaries
    may wait within the original grant and artifact lifetime, then revalidate
    everything again. Unknown transport outcomes must never become retryable.
    """
    def __init__(self):
        PermissionError.__init__(self, 'recipient_evidence_unavailable')


class Unavailable(RuntimeError):
    def __init__(self):
        super().__init__('project_authority_unavailable')


def _safe_database_errors(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (DatabaseError, StoreDatabaseError):
            # SQL integrity/availability details can contain full actor keys.
            # The host may retry a transaction but must never treat this as allow.
            raise Unavailable() from None
    return call


class Operation(str, Enum):
    READ = 'project.read'
    WRITE = 'project.write'
    EXECUTE = 'project.execute'
    MANAGE = 'project.manage'
    DELIVER = 'project.deliver'

    @property
    def route_operation(self) -> str:
        if self is Operation.READ:
            return 'read'
        return 'send' if self is Operation.DELIVER else 'execute'


def _positive(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2**63-1


def _text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return (len(value.encode('utf-8')) <= limit and not any(
            unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in value))
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True)
class ProjectGrantRef:
    grant_id: str = field(repr=False)
    revision: int

    def __post_init__(self):
        if (not isinstance(self.grant_id, str)
                or re.fullmatch('[0-9a-f]{32}', self.grant_id) is None
                or not _positive(self.revision)):
            raise Denied()


@dataclass(frozen=True)
class ProjectRequest:
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    # Resource scope, possibly an approved old route alias. Not a send target.
    canonical_scope: ConversationScope = field(repr=False)
    route_epoch: int
    endpoint_route_epoch: int
    project_id: int = field(repr=False)
    operation: Operation

    def __post_init__(self):
        if (not isinstance(self.endpoint, EndpointKey)
                or self.endpoint.account.platform != 'qq-official'
                or not isinstance(self.actor, ActorKey)
                or self.actor.identity_kind == 'bot'
                or not self.actor.matches_endpoint(self.endpoint)
                or not isinstance(self.canonical_scope, ConversationScope)
                or not isinstance(self.operation, Operation)
                or type(self.route_epoch) is not int or not 0 <= self.route_epoch <= 2**63-1
                or not _positive(self.endpoint_route_epoch) or not _positive(self.project_id)):
            raise Denied()
        for value, limit in (
            (self.endpoint.account.key, 512), (self.endpoint.key, 1000),
            (self.actor.key, 1280), (self.canonical_scope.key, 1000),
            (self.actor.native_user_id, 256), (self.endpoint.native_conversation_id, 256),
        ):
            if not _text(value, limit):
                raise Denied()


@dataclass(frozen=True)
class ProjectSnapshot:
    grant: ProjectGrantRef
    principal_id: int = field(repr=False)
    project_id: int = field(repr=False)
    operation: Operation
    expires_at: int


def _deny(*args, **kwargs):
    return False


class ProjectAuthority:
    def __init__(self, *, source_guard=_deny, route_guard=_deny,
                 recipient_evidence=_deny, fresh_actor_evidence=_deny,
                 clock=time.time, max_grant_ttl_seconds=86400):
        if type(max_grant_ttl_seconds) is not int or not 60 <= max_grant_ttl_seconds <= 86400:
            raise Denied()
        self.max_grant_ttl_seconds = max_grant_ttl_seconds
        self.source_guard = source_guard
        self.route_guard = route_guard
        self.recipient_evidence = recipient_evidence
        self.fresh_actor_evidence = fresh_actor_evidence
        self.clock = clock
        self.owners = OwnerAuthority(clock=clock)

    def _now(self) -> int:
        value = self.clock()
        # Reject NaN, bool, overflow and invalid clocks before SQL or mutation.
        if type(value) not in (int, float) or not 0 <= value <= 2**63-1:
            raise Denied()
        return int(value)

    def _prepare(self, cursor, request: ProjectRequest, source):
        if not isinstance(request, ProjectRequest):
            raise Denied()
        account = request.endpoint.account
        if cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',
                          (account.key,)).fetchone() is None:
            raise Denied()
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE',
                             (request.endpoint.key, account.key)).fetchone()
        if (row is None or row['kind'] != request.endpoint.kind
                or row['native_conversation_id'] != request.endpoint.native_conversation_id
                or row['membership_state'] != 'present' or row['binding_state'] == 'revoked'
                or (request.endpoint.kind == 'group' and row['binding_state'] != 'active')
                or row['route_epoch'] != request.endpoint_route_epoch):
            raise Denied()
        # Root owns aliases and migration states. A trusted wrapper calls its
        # assert_active_route and returns strict True only after success.
        if self.route_guard(cursor, canonical_scope=request.canonical_scope,
                            endpoint=request.endpoint, route_epoch=request.route_epoch,
                            operation=request.operation.route_operation) is not True:
            raise Denied()
        cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',
                       ('project-scope:'+request.canonical_scope.key,))
        if self.source_guard(cursor, request=request, source=source) is not True:
            raise Denied()
        return row

    @staticmethod
    def _principal(cursor, actor):
        row = cursor.execute('''SELECT principal_id FROM principal_identities
            WHERE platform=? AND account_key=? AND identity_kind=?
            AND identity_namespace=? AND native_user_id=? FOR SHARE''',
            (actor.account.platform, actor.account.key, actor.identity_kind,
             actor.identity_namespace, actor.native_user_id)).fetchone()
        if row is None:
            raise Denied()
        return row['principal_id']

    @staticmethod
    def _project(cursor, request):
        row = cursor.execute('SELECT scope_key,status,bot_native_user_id FROM projects WHERE project_id=? FOR UPDATE',
                             (request.project_id,)).fetchone()
        if (row is None or row['scope_key'] != request.canonical_scope.key
                or row['status'] == 'purged'
                or (row['status'] == 'trash' and request.operation is not Operation.MANAGE)):
            raise Denied()
        # source_guard MUST use identity_store.snapshot's verified actual bot
        # native ID for a new official resource. An AppID is not that identity.
        # For an old resource-scope alias, source_guard MUST resolve the exact
        # approved project migration item and compare this project's original
        # bot_native_user_id to its frozen reviewed origin. A scope-level route
        # alias alone cannot authorize other bots' projects in the same group.

    def _owner(self, cursor, request):
        grant = self.owners.owner_for_actor(cursor, request.endpoint, request.actor)
        if grant is None:
            raise Denied()
        cursor.execute('SELECT grant_id FROM qq_official_owner_grants WHERE grant_id=? FOR SHARE',
                       (grant.grant_id,)).fetchone()
        link_revision = None
        if request.endpoint.kind == 'group':
            link = cursor.execute('''SELECT revision FROM qq_official_owner_actor_links
                WHERE endpoint_key=? AND actor_key=? AND state='active' FOR SHARE''',
                (request.endpoint.key, request.actor.key)).fetchone()
            if link is None:
                raise Denied()
            link_revision = link['revision']
        return grant, link_revision

    @_safe_database_errors
    def issue_project(self, cursor, *, issuer: ProjectRequest, recipient: ActorKey,
                      capabilities: frozenset[Operation], grant_id: str,
                      expires_at: int, evidence_ref: str, source,
                      expected_revision: int = 0) -> ProjectGrantRef:
        """Trusted command/console adapter only. Caller commits its own decision.

        Recipient evidence identifies the actor; an old message alone is never
        permission. Local owner + current route/source + this explicit grant are
        required. Reissue cannot change the immutable namespace or project.
        """
        now = self._now()
        if (not isinstance(issuer, ProjectRequest) or issuer.operation is not Operation.MANAGE
                or not isinstance(recipient, ActorKey) or recipient.identity_kind == 'bot'
                or not recipient.matches_endpoint(issuer.endpoint)
                or not _text(recipient.key, 1280) or not _text(recipient.native_user_id, 256)
                or type(capabilities) is not frozenset or not capabilities
                or any(not isinstance(cap, Operation) for cap in capabilities)
                or type(expected_revision) is not int or not 0 <= expected_revision < 2**63-1
                or not _positive(expires_at) or not now < expires_at <= now+self.max_grant_ttl_seconds
                or not isinstance(evidence_ref, str)
                or re.fullmatch('[A-Za-z0-9_.:-]{1,128}', evidence_ref) is None):
            raise Denied()
        ProjectGrantRef(grant_id, expected_revision+1)
        endpoint = self._prepare(cursor, issuer, source)
        owner, link_revision = self._owner(cursor, issuer)
        if self.recipient_evidence(cursor, endpoint=issuer.endpoint, actor=recipient,
                                   evidence_ref=evidence_ref, source=source) is not True:
            raise Denied()
        principal = self._principal(cursor, recipient)
        prior = cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=? FOR UPDATE',
                               (grant_id,)).fetchone()
        immutable = (issuer.project_id, issuer.endpoint.account.key, issuer.endpoint.key,
                     recipient.key, principal, issuer.canonical_scope.key, owner.grant_id)
        if prior is None:
            if expected_revision:
                raise Denied()
        elif (prior['revision'] != expected_revision or tuple(prior[key] for key in (
                'project_id', 'account_key', 'endpoint_key', 'actor_key', 'principal_id',
                'project_scope_key', 'owner_grant_id')) != immutable):
            raise Denied()
        self._project(cursor, issuer)
        now = self._now()
        if (not now < expires_at <= now+self.max_grant_ttl_seconds
                or self.source_guard(cursor, request=issuer, source=source) is not True):
            raise Denied()
        caps = sorted(cap.value for cap in capabilities)
        if prior is None:
            cursor.execute('''INSERT INTO qq_official_project_grants
                (grant_id,project_id,account_key,endpoint_key,actor_key,principal_id,
                 project_scope_key,endpoint_generation,route_epoch,endpoint_route_epoch,issuer_actor_key,
                 owner_grant_id,owner_revision,issuer_link_revision,capabilities,
                 evidence_kind,evidence_ref,revision,state,issued_at,expires_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'owner_command',?,1,'active',?,?,?)''',
                (grant_id, *immutable[:6], endpoint['generation'], issuer.route_epoch,
                 issuer.endpoint_route_epoch, issuer.actor.key, owner.grant_id, owner.revision, link_revision, caps,
                 evidence_ref, now, expires_at, now))
        else:
            cursor.execute('''UPDATE qq_official_project_grants SET endpoint_generation=?,route_epoch=?,endpoint_route_epoch=?,
                issuer_actor_key=?,owner_revision=?,issuer_link_revision=?,capabilities=?,
                evidence_kind='owner_command',evidence_ref=?,revision=revision+1,state='active',issued_at=?,expires_at=?,
                updated_at=?,revoked_at=NULL WHERE grant_id=?''',
                (endpoint['generation'], issuer.route_epoch, issuer.endpoint_route_epoch, issuer.actor.key, owner.revision,
                 link_revision, caps, evidence_ref, now, expires_at, now, grant_id))
        return ProjectGrantRef(grant_id, expected_revision+1)

    @_safe_database_errors
    def revoke_project(self, cursor, *, issuer: ProjectRequest, grant: ProjectGrantRef, source):
        if (not isinstance(issuer, ProjectRequest) or issuer.operation is not Operation.MANAGE
                or not isinstance(grant, ProjectGrantRef)):
            raise Denied()
        self._prepare(cursor, issuer, source)
        owner, _ = self._owner(cursor, issuer)
        row = cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=? FOR UPDATE',
                             (grant.grant_id,)).fetchone()
        if (row is None or row['revision'] != grant.revision
                or row['project_id'] != issuer.project_id
                or row['endpoint_key'] != issuer.endpoint.key
                or row['owner_grant_id'] != owner.grant_id):
            raise Denied()
        self._project(cursor, issuer)
        if self.source_guard(cursor, request=issuer, source=source) is not True:
            raise Denied()
        if row['state'] != 'revoked':
            now = self._now()
            cursor.execute('''UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,
                revoked_at=?,updated_at=? WHERE grant_id=?''', (now, now, grant.grant_id))
        return ProjectGrantRef(grant.grant_id, row['revision'] + (row['state'] != 'revoked'))

    @_safe_database_errors
    def require_project(self, cursor, *, request: ProjectRequest, grant: ProjectGrantRef, source) -> ProjectSnapshot:
        if not isinstance(grant, ProjectGrantRef):
            raise Denied()
        now = self._now()
        endpoint = self._prepare(cursor, request, source)
        preview = cursor.execute('SELECT owner_grant_id FROM qq_official_project_grants WHERE grant_id=?',
                                 (grant.grant_id,)).fetchone()
        if preview is None:
            raise Denied()
        owner = cursor.execute('SELECT * FROM qq_official_owner_grants WHERE grant_id=? FOR SHARE',
                               (preview['owner_grant_id'],)).fetchone()
        row = cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=? FOR UPDATE',
                             (grant.grant_id,)).fetchone()
        if (row is None or owner is None or owner['state'] != 'active'
                or owner['account_key'] != request.endpoint.account.key
                or owner['revision'] != row['owner_revision']
                or row['state'] != 'active' or row['revision'] != grant.revision
                or not row['issued_at'] <= now < row['expires_at']
                or row['account_key'] != request.endpoint.account.key
                or row['endpoint_key'] != request.endpoint.key or row['actor_key'] != request.actor.key
                or row['project_id'] != request.project_id
                or row['project_scope_key'] != request.canonical_scope.key
                or row['endpoint_generation'] != endpoint['generation']
                or row['route_epoch'] != request.route_epoch
                or row['endpoint_route_epoch'] != request.endpoint_route_epoch
                or request.operation.value not in row['capabilities']):
            raise Denied()
        try:
            issuer = ActorKey.parse(row['issuer_actor_key'])
        except (ValueError, TypeError):
            raise Denied() from None
        if not issuer.matches_endpoint(request.endpoint) or issuer.identity_kind == 'bot':
            raise Denied()
        current_owner = self.owners.owner_for_actor(cursor, request.endpoint, issuer)
        if current_owner is None or current_owner.grant_id != owner['grant_id']:
            raise Denied()
        if request.endpoint.kind == 'group':
            link = cursor.execute('''SELECT revision FROM qq_official_owner_actor_links
                WHERE endpoint_key=? AND actor_key=? AND state='active' FOR SHARE''',
                (request.endpoint.key, issuer.key)).fetchone()
            if link is None or link['revision'] != row['issuer_link_revision']:
                raise Denied()
        elif row['issuer_link_revision'] is not None:
            raise Denied()
        principal = self._principal(cursor, request.actor)
        if principal != row['principal_id']:
            raise Denied()
        self._project(cursor, request)
        fresh = (request.operation is not Operation.DELIVER or self.fresh_actor_evidence(
            cursor, request=request, source=source, now=self._now(), max_age_seconds=300) is True)
        # Account/project locks may have waited past the original expiry.
        if (not row['issued_at'] <= self._now() < row['expires_at']
                or self.source_guard(cursor, request=request, source=source) is not True):
            raise Denied()
        if not fresh:
            raise RecipientEvidenceUnavailable()
        return ProjectSnapshot(grant, principal, request.project_id, request.operation, row['expires_at'])

    @_safe_database_errors
    def freeze_task_grant(self, cursor, *, request: ProjectRequest, grant: ProjectGrantRef,
                          task_id: int, source) -> ProjectGrantRef:
        """Enlist with existing ProjectTaskStore admission; no queue or commit.

        An existing task cannot silently switch to a refreshed grant. Recovery
        reads this exact reference, then calls require_project under its actual
        task/job source guard before every operation. This is a reference, not
        permission or a replacement for the normal task/baseline/job checks.
        """
        if (not isinstance(request, ProjectRequest) or request.operation is not Operation.EXECUTE
                or not _positive(task_id)):
            raise Denied()
        self.require_project(cursor, request=request, grant=grant, source=source)
        task = cursor.execute('''SELECT t.account_key,t.endpoint_key,t.requester_actor_key,b.project_id,
            b.actor_principal_id FROM subagent_tasks t JOIN project_task_bindings b ON b.task_id=t.task_id
            WHERE t.task_id=? FOR UPDATE OF t,b''', (task_id,)).fetchone()
        if (task is None or task['account_key'] != request.endpoint.account.key
                or task['endpoint_key'] != request.endpoint.key or task['requester_actor_key'] != request.actor.key
                or task['project_id'] != request.project_id
                or task['actor_principal_id'] != self._principal(cursor, request.actor)):
            raise Denied()
        # Waiting for an existing task row may outlast grant/source leases.
        self.require_project(cursor, request=request, grant=grant, source=source)
        prior = cursor.execute('SELECT * FROM qq_official_project_task_authority WHERE task_id=? FOR UPDATE',
                               (task_id,)).fetchone()
        expected = (request.project_id, grant.grant_id, grant.revision, request.endpoint.account.key,
                    request.endpoint.key, request.actor.key, request.route_epoch, request.endpoint_route_epoch)
        if prior is not None:
            if tuple(prior[key] for key in ('project_id','grant_id','grant_revision','account_key',
                    'endpoint_key','actor_key','route_epoch','endpoint_route_epoch')) != expected:
                raise Denied()
            return grant
        cursor.execute('''INSERT INTO qq_official_project_task_authority(task_id,project_id,grant_id,grant_revision,
            account_key,endpoint_key,actor_key,route_epoch,endpoint_route_epoch,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', (task_id, *expected, self._now()))
        return grant
