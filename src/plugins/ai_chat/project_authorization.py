"""Host-issued official project access. No model-supplied permission booleans.

The foreground source uses the actual M4 execution and job fence.
Background recovery must supply a separately issued, exact persisted source;
it cannot fall back to choosing a fresh foreground grant.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
import threading
import time
import uuid
import weakref

from src.plugins.ai_chat.adapters.qq_official import turn_source as execution, identity_store
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseUnavailable
from src.plugins.ai_chat.conversation_scope import ConversationScope

from .adapters.qq_official.project_authority import Denied, Operation, ProjectAuthority, ProjectGrantRef, ProjectRequest

_issued = weakref.WeakValueDictionary()
_lock = threading.RLock()


def _deny(*args, **kwargs):
    return False


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class ProjectProof:
    provider: object
    scope: ConversationScope
    principal_id: int

    def __repr__(self):
        return 'ProjectProof(<protected>)'


def official_project(access):
    """Return only a locally issued proof, denying mixed/forged identities."""
    proof = getattr(access, 'official_proof', None)
    endpoint = getattr(access, 'endpoint', None)
    actor = getattr(access, 'actor', None)
    if proof is None and endpoint is None and actor is None:
        if access.scope.platform == 'qq-official':
            raise Denied()
        return None
    with _lock:
        if (not isinstance(proof, ProjectProof) or _issued.get(id(proof)) is not proof
                or access.scope != proof.scope or access.principal_id != proof.principal_id
                or endpoint != proof.provider.endpoint or actor != proof.provider.actor
                or access.member or access.admin):
            raise Denied()
    return proof


def authorize(cursor, access, *, project_id=None, operation='project.read'):
    proof = official_project(access)
    if proof is None:
        return None
    return proof.provider.require(cursor, proof, project_id=project_id, operation=Operation(operation))


class ForegroundProjectProvider:
    """One actual execution's fixed identity and grant selections.

    Route/source-scope guards are mandatory host ports. The latter validates an
    exact approved migrated project and its original bot snapshot, not merely
    that some project exists in an aliased conversation.
    """
    def __init__(self, *, token, handle, job, admission, route_epoch,
                 route_guard=_deny, project_scope_guard=_deny, clock=time.time, expected_execution_kind="chat",
                 max_grant_ttl_seconds=86400):
        if type(route_epoch) is not int or route_epoch < 0:
            raise Denied()
        if expected_execution_kind not in {"chat", "command"}:
            raise Denied()
        self.expected_execution_kind = expected_execution_kind
        self.token, self.handle, self.job, self.admission = token, handle, job, admission
        self.endpoint, self.actor = handle.endpoint, handle.actor
        self.route_epoch = route_epoch
        self.route_guard, self.project_scope_guard, self.clock = route_guard, project_scope_guard, clock
        self._pins = {}
        self._pins_lock = threading.RLock()
        self.authority = ProjectAuthority(source_guard=self._source_guard,
            route_guard=route_guard, recipient_evidence=self._recipient,
            fresh_actor_evidence=self._fresh, clock=clock, max_grant_ttl_seconds=max_grant_ttl_seconds)

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not 0 <= value <= 2**63-1:
            raise Denied()
        return int(value)

    def _current(self, cursor):
        # Both actual endpoint and approved resource alias acquire the same
        # sorted logical route mutexes before M4 inbox/job locks.
        if self.route_guard(cursor, canonical_scope=self.endpoint.scope, endpoint=self.endpoint,
                            route_epoch=self.route_epoch, operation='read') is not True:
            raise Denied()
        try:
            row = execution.assert_execution_current(cursor, token=self.token, handle=self.handle,
                job=self.job, admission=self.admission, now=self._now())
        except (execution.ExecutionError, LeaseUnavailable):
            raise Denied() from None
        if (row['state'] != 'executing' or row['execution_kind'] != self.expected_execution_kind
                or self._now() >= row['deadline_at']):
            raise Denied()
        return row

    def _source_guard(self, cursor, *, request, source):
        if (source is not self or request.endpoint != self.endpoint or request.actor != self.actor
                or request.route_epoch != self.route_epoch
                or request.endpoint_route_epoch != self.handle.route_epoch):
            return False
        self._current(cursor)
        project = cursor.execute('SELECT * FROM projects WHERE project_id=?',
                                 (request.project_id,)).fetchone()
        if project is None or project['scope_key'] != request.canonical_scope.key:
            return False
        if request.canonical_scope.key == self.endpoint.scope.key:
            author = identity_store.snapshot(cursor, self.endpoint.account)
            return author is not None and project['bot_native_user_id'] == author.bot_actor.native_user_id
        return self.project_scope_guard(cursor, request=request, project=dict(project), source=self) is True

    def _recipient(self, cursor, *, endpoint, actor, evidence_ref, source):
        self._current(cursor)
        return (source is self and endpoint == self.endpoint and actor == self.actor
                and evidence_ref == 'project-create:' + self.handle.execution_id)

    def _fresh(self, cursor, *, request, source, now, max_age_seconds):
        self._current(cursor)
        row = cursor.execute('''SELECT occurred_at FROM messages WHERE canonical_message_id=?
            AND source_endpoint_key=? AND sender_actor_key=? AND direction='inbound' ''',
            (self.handle.canonical_message_id, self.endpoint.key, self.actor.key)).fetchone()
        return source is self and row is not None and 0 <= now-row['occurred_at'] <= max_age_seconds

    def issue(self, cursor):
        from src.plugins.ai_chat.projects import ProjectAccess
        self._current(cursor)
        if self.route_guard(cursor, canonical_scope=self.endpoint.scope, endpoint=self.endpoint,
                            route_epoch=self.route_epoch, operation='read') is not True:
            raise Denied()
        author = identity_store.snapshot(cursor, self.endpoint.account)
        if author is None:
            raise Denied()
        principal = self.authority._principal(cursor, self.actor)
        scope = replace(self.endpoint.scope, actor_native_user_id=self.actor.native_user_id,
                        bot_native_user_id=author.bot_actor.native_user_id)
        proof = ProjectProof(self, scope, principal)
        with _lock:
            _issued[id(proof)] = proof
        return ProjectAccess(scope, principal, endpoint=self.endpoint, actor=self.actor, official_proof=proof)

    def _pin(self, cursor, project_id):
        with self._pins_lock:
            pinned = self._pins.get(project_id)
            if pinned is not None:
                return pinned
            rows = cursor.execute('''SELECT grant_id,revision,project_scope_key FROM qq_official_project_grants
                WHERE project_id=? AND account_key=? AND endpoint_key=? AND actor_key=? AND state='active'
                AND issued_at<=? AND expires_at>? ORDER BY grant_id LIMIT 2''',
                (project_id, self.endpoint.account.key, self.endpoint.key, self.actor.key,
                 self._now(), self._now())).fetchall()
            if len(rows) != 1:
                raise Denied()
            row = rows[0]
            pinned = ProjectGrantRef(row['grant_id'], row['revision']), ConversationScope.from_key(row['project_scope_key'])
            # Pin before checking the operation: a later regrant must not turn
            # this execution's denied write into an allowed write.
            self._pins[project_id] = pinned
            return pinned

    def require(self, cursor, proof, *, project_id=None, operation=Operation.READ):
        with _lock:
            if not isinstance(proof, ProjectProof) or _issued.get(id(proof)) is not proof:
                raise Denied()
        self._current(cursor)
        if (proof.provider is not self or self.authority._principal(cursor, self.actor) != proof.principal_id
                or self.route_guard(cursor, canonical_scope=self.endpoint.scope, endpoint=self.endpoint,
                    route_epoch=self.route_epoch, operation='read') is not True):
            raise Denied()
        if project_id is None:
            return proof.scope
        grant, scope = self._pin(cursor, project_id)
        request = ProjectRequest(self.endpoint, self.actor, scope, self.route_epoch,
                                 self.handle.route_epoch, project_id, operation)
        self.authority.require_project(cursor, request=request, grant=grant, source=self)
        return scope

    def before_create(self, cursor, proof):
        self.require(cursor, proof)
        if (self.route_guard(cursor, canonical_scope=self.endpoint.scope, endpoint=self.endpoint,
                             route_epoch=self.route_epoch, operation='execute') is not True
                or OwnerAuthority(clock=self.clock).owner_for_actor(cursor, self.endpoint, self.actor) is None):
            raise Denied()

    def created(self, cursor, proof, project_id):
        self.before_create(cursor, proof)
        request = ProjectRequest(self.endpoint, self.actor, self.endpoint.scope, self.route_epoch,
                                 self.handle.route_epoch, project_id, Operation.MANAGE)
        grant = self.authority.issue_project(cursor, issuer=request, recipient=self.actor,
            capabilities=frozenset(Operation), grant_id=uuid.uuid4().hex,
            expires_at=self._now()+self.authority.max_grant_ttl_seconds,
            evidence_ref='project-create:' + self.handle.execution_id, source=self)
        # The grant and new project commit together. If the caller rolls back,
        # the pin cannot authorize a missing grant on retry.
        with self._pins_lock:
            self._pins[project_id] = grant, self.endpoint.scope

    def visible(self, cursor, proof, *, include_trash=False):
        self.require(cursor, proof)
        ids = cursor.execute('''SELECT project_id FROM qq_official_project_grants
            WHERE account_key=? AND endpoint_key=? AND actor_key=? AND state='active'
            AND issued_at<=? AND expires_at>? ORDER BY project_id LIMIT 200''',
            (self.endpoint.account.key, self.endpoint.key, self.actor.key, self._now(), self._now())).fetchall()
        rows = []
        for item in ids:
            try:
                self.require(cursor, proof, project_id=item['project_id'])
            except Denied:
                continue
            row = cursor.execute('SELECT * FROM projects WHERE project_id=?', (item['project_id'],)).fetchone()
            if row is not None and row['status'] != 'purged' and (include_trash or row['status'] != 'trash'):
                rows.append(dict(row))
        return sorted(rows, key=lambda row: (row['last_active_at'], row['project_id']), reverse=True)

    @contextmanager
    def guard_scope(self, transaction):
        """Recheck original pins before reusing project text in a model RPC.

        Each check commits/closes its short transaction before the caller can
        perform network I/O. It cannot select a replacement grant after revoke.
        """
        from .agent.control import RouteFence, active_route_fence
        previous=active_route_fence.get()
        def current(cursor):
            proof=official_project(self.issue(cursor))
            with self._pins_lock:
                projects=tuple(self._pins)
            for project_id in projects:
                self.require(cursor,proof,project_id=project_id,operation=Operation.READ)
        def check():
            if previous is not None:previous.assert_current()
            with transaction() as cursor:current(cursor)
            return True
        def guarded(cursor):
            if previous is not None:
                if previous.transaction_guard is not None:previous.transaction_guard(cursor)
                else:previous.assert_current()
            current(cursor)
        token=active_route_fence.set(RouteFence(check,guarded))
        try:yield
        finally:active_route_fence.reset(token)
