"""Draft official admin identity mode, using the existing local account store.

No approval queue or remote operations here. Console/bootstrap adapters are
trusted host ports; their callback must authenticate provenance under this
transaction and return strict True. A caller-supplied boolean is not evidence.
Passwords are hashed by existing hash_password BEFORE opening the transaction.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field

from .project_authority import Denied, _deny, _positive, _safe_database_errors, _text
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


@dataclass(frozen=True)
class AdminBindingRef:
    binding_id: str = field(repr=False)
    revision: int

    def __post_init__(self):
        if (not isinstance(self.binding_id, str) or re.fullmatch('[0-9a-f]{32}', self.binding_id) is None
                or not _positive(self.revision)):
            raise Denied()


@dataclass(frozen=True)
class AdminSnapshot:
    admin_account_id: str = field(repr=False)
    admin_version: int
    binding: AdminBindingRef
    c2c_endpoint: EndpointKey = field(repr=False)
    c2c_actor: ActorKey = field(repr=False)
    owner_grant_id: str = field(repr=False)
    owner_revision: int

    def __post_init__(self):
        if (not _text(self.admin_account_id, 128) or not _positive(self.admin_version)
                or not isinstance(self.binding, AdminBindingRef)
                or not isinstance(self.c2c_endpoint, EndpointKey) or self.c2c_endpoint.kind != 'private'
                or self.c2c_endpoint.account.platform != 'qq-official'
                or not isinstance(self.c2c_actor, ActorKey) or self.c2c_actor.identity_kind != 'c2c_user'
                or not self.c2c_actor.matches_endpoint(self.c2c_endpoint)
                or not _text(self.owner_grant_id, 64) or not _positive(self.owner_revision)):
            raise Denied()


def two_distinct_official_admins(first: AdminSnapshot, second: AdminSnapshot) -> bool:
    """Identity distinctness only. The real quorum store checks current votes.

    Do not use snapshots as approval evidence. Existing confirmations, code
    generation, task scope, lifetime and revision checks still apply.
    """
    return (isinstance(first, AdminSnapshot) and isinstance(second, AdminSnapshot)
            and first.c2c_endpoint.account == second.c2c_endpoint.account
            and first.admin_account_id != second.admin_account_id
            and first.c2c_actor.key != second.c2c_actor.key)


class AdminBindingStore:
    def __init__(self, *, control_guard=_deny, clock=time.time):
        self.control_guard = control_guard
        self.clock = clock
        self.owners = OwnerAuthority(clock=clock)

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not 0 <= value <= 2**63-1:
            raise Denied()
        return int(value)

    def _lock(self, cursor, endpoint, actor):
        if (not isinstance(endpoint, EndpointKey) or endpoint.account.platform != 'qq-official'
                or not isinstance(actor, ActorKey) or actor.identity_kind == 'bot'
                or not actor.matches_endpoint(endpoint)):
            raise Denied()
        for value, limit in ((endpoint.account.key, 512), (endpoint.key, 1000), (actor.key, 1280),
                             (endpoint.native_conversation_id, 256), (actor.native_user_id, 256)):
            if not _text(value, limit):
                raise Denied()
        if not self.owners.lock_account(cursor, endpoint.account):
            raise Denied()
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE',
                             (endpoint.key, endpoint.account.key)).fetchone()
        if (row is None or row['kind'] != endpoint.kind
                or row['native_conversation_id'] != endpoint.native_conversation_id
                or row['membership_state'] != 'present' or row['binding_state'] == 'revoked'
                or (endpoint.kind == 'group' and row['binding_state'] != 'active')):
            raise Denied()
        # All official approval adapters must use account -> endpoint -> security
        # lock order; never enter legacy SecurityStore.transaction then acquire
        # an official account. Legacy-only calls never acquire official rows.
        cursor.execute('INSERT INTO admin_security_lock VALUES (1,0) ON CONFLICT(lock_id) DO NOTHING')
        cursor.execute('UPDATE admin_security_lock SET revision=revision+1 WHERE lock_id=1')

    def _control(self, cursor, *, endpoint, actor, authorization, bootstrap, operation):
        self._lock(cursor, endpoint, actor)
        if (endpoint.kind != 'private' or actor.identity_kind != 'c2c_user'
                or type(bootstrap) is not bool
                or self.control_guard(cursor, authorization=authorization,
                    endpoint=endpoint, actor=actor, bootstrap=bootstrap, operation=operation) is not True):
            raise Denied()
        if bootstrap and cursor.execute('SELECT account_id FROM admin_accounts LIMIT 1').fetchone() is not None:
            raise Denied()

    def _owner(self, cursor, actor, expected_revision):
        if not _positive(expected_revision):
            raise Denied()
        owner = self.owners.c2c_grant(cursor, actor, lock=True)
        if owner is None or owner.revision != expected_revision:
            raise Denied()
        return owner

    def _insert(self, cursor, *, admin, endpoint, actor, owner):
        if cursor.execute('''SELECT 1 FROM qq_official_admin_bindings
            WHERE account_key=? AND (admin_account_id=? OR c2c_actor_key=?)''',
            (endpoint.account.key, admin['account_id'], actor.key)).fetchone() is not None:
            raise Denied()
        now = self._now()
        ref = AdminBindingRef(secrets.token_hex(16), 1)
        cursor.execute('''INSERT INTO qq_official_admin_bindings(binding_id,admin_account_id,admin_account_version,
            account_key,c2c_endpoint_key,c2c_actor_key,owner_grant_id,owner_revision,revision,state,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,1,'active',?,?)''',
            (ref.binding_id, admin['account_id'], admin['version'], endpoint.account.key,
             endpoint.key, actor.key, owner.grant_id, owner.revision, now, now))
        return AdminSnapshot(admin['account_id'], admin['version'], ref, endpoint, actor, owner.grant_id, owner.revision)

    @_safe_database_errors
    def create_official_admin(self, cursor, *, username, password_hash, endpoint, actor,
                              owner_revision, authorization, bootstrap=False) -> AdminSnapshot:
        # Keep the current minimum Argon2id profile; no expensive work in locks.
        if (not isinstance(username, str) or re.fullmatch('[a-z0-9][a-z0-9_.-]{2,31}', username) is None
                or not isinstance(password_hash, str)
                or re.fullmatch(r'\$argon2id\$v=19\$m=19456,t=2,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}', password_hash) is None):
            raise Denied()
        self._control(cursor, endpoint=endpoint, actor=actor, authorization=authorization,
                      bootstrap=bootstrap, operation='create_official_admin')
        owner = self._owner(cursor, actor, owner_revision)
        if cursor.execute('SELECT 1 FROM admin_accounts WHERE username=?', (username,)).fetchone() is not None:
            raise Denied()
        admin = {'account_id': secrets.token_hex(16), 'version': 1}
        cursor.execute('''INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,
            enabled,version,created_at,identity_mode) VALUES (?,?,?,'admin',NULL,1,1,?,'official_c2c')''',
            (admin['account_id'], username, password_hash, self._now()))
        return self._insert(cursor, admin=admin, endpoint=endpoint, actor=actor, owner=owner)

    @_safe_database_errors
    def bind_existing(self, cursor, *, admin_account_id, admin_version, endpoint, actor,
                      owner_revision, authorization) -> AdminSnapshot:
        if not isinstance(admin_account_id, str) or not _positive(admin_version):
            raise Denied()
        self._control(cursor, endpoint=endpoint, actor=actor, authorization=authorization,
                      bootstrap=False, operation='bind_existing_admin')
        owner = self._owner(cursor, actor, owner_revision)
        admin = cursor.execute('''SELECT * FROM admin_accounts WHERE account_id=? AND version=?
            AND enabled=1 AND role='admin' FOR UPDATE''', (admin_account_id, admin_version)).fetchone()
        if admin is None:
            raise Denied()
        return self._insert(cursor, admin=admin, endpoint=endpoint, actor=actor, owner=owner)

    @_safe_database_errors
    def resolve_admin(self, cursor, *, endpoint, actor) -> AdminSnapshot:
        """Current identity mapping only; caller still needs task/route/approval.

        A current group OwnerGrant link can resolve its explicitly linked admin
        account. It never turns an unrelated owner's native ID into a local QQ.
        """
        self._lock(cursor, endpoint, actor)
        owner = self.owners.owner_for_actor(cursor, endpoint, actor)
        if owner is None:
            raise Denied()
        row = cursor.execute('''SELECT b.*,a.version AS live_admin_version,a.enabled,a.role
            FROM qq_official_admin_bindings b JOIN admin_accounts a ON a.account_id=b.admin_account_id
            WHERE b.account_key=? AND b.owner_grant_id=? AND b.c2c_actor_key=?
            FOR SHARE OF b,a''', (endpoint.account.key, owner.grant_id, owner.c2c_actor_key)).fetchone()
        if (row is None or row['state'] != 'active' or row['owner_revision'] != owner.revision
                or row['role'] != 'admin' or row['enabled'] != 1
                or row['live_admin_version'] != row['admin_account_version']):
            raise Denied()
        try:
            c2c = EndpointKey.parse(row['c2c_endpoint_key'])
            principal = ActorKey.parse(row['c2c_actor_key'])
        except (ValueError, TypeError):
            raise Denied() from None
        if c2c.kind != 'private' or not principal.matches_endpoint(c2c) or c2c.account != endpoint.account:
            raise Denied()
        reachable = cursor.execute('''SELECT 1 FROM qq_official_endpoints
            WHERE endpoint_key=? AND account_key=? AND kind='private'
            AND membership_state='present' AND binding_state<>'revoked' FOR SHARE''',
            (c2c.key, c2c.account.key)).fetchone()
        if reachable is None:
            raise Denied()
        return AdminSnapshot(row['admin_account_id'], row['admin_account_version'],
            AdminBindingRef(row['binding_id'], row['revision']), c2c, principal, owner.grant_id, owner.revision)
