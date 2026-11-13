"""Persistent VM authority, never an impersonated human or a shared-history alias.

The lifecycle caller supplies the *observed* guest boot id and the authenticated
endpoint route. HTTP callers may supply only the opaque token. Before exposing
routes, the lifecycle must also check the actual VM and revoke stale boots.
"""
from dataclasses import dataclass, field
import hashlib
import secrets
import time
from uuid import UUID

from ...background_tools import workspace_owner
from ...platform_types import ActorKey, EndpointKey
from ...sandbox import SANDBOX_ID_PATTERN
from ...sandbox_identity import TOKEN
from .lease import assert_epoch


@dataclass(frozen=True)
class VMSource:
    endpoint: EndpointKey
    sandbox_id: str
    boot_id: str
    token_generation: int
    account_epoch: int
    endpoint_generation: int
    route_epoch: int
    token_hash: str = field(repr=False)

    @property
    def scope_key(self):
        return self.endpoint.scope.key

    @property
    def caller_ref(self):
        return f'vm:{self.sandbox_id}:{self.token_generation}'


@dataclass(frozen=True)
class VMCredential:
    source: VMSource
    token: str = field(repr=False)


def fingerprint(token):
    if type(token) is not str or TOKEN.fullmatch(token) is None:
        raise PermissionError('vm_token_invalid')
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def source(row):
    return VMSource(EndpointKey.parse(row['endpoint_key']),*(row[key] for key in (
        'sandbox_id','boot_id','token_generation','account_epoch','endpoint_generation','route_epoch','token_hash')))


class VMSessions:
    def __init__(self, transaction, lease, *, private_admission, clock=time.time):
        self.transaction,self.lease,self.private_admission,self.clock = transaction,lease,private_admission,clock

    def _route(self, cursor, endpoint):
        assert_epoch(cursor,self.lease)
        if not isinstance(endpoint,EndpointKey) or endpoint.account.key!=self.lease.account_key:
            raise PermissionError('vm_endpoint_unavailable')
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',
            (endpoint.key,)).fetchone()
        if row is None or row['membership_state']!='present' or row['binding_state']=='revoked':
            raise PermissionError('vm_endpoint_unavailable')
        if endpoint.kind=='group':
            allowed = row['binding_state']=='active'
        else:
            # This checks access to the private endpoint, NOT a caller identity.
            # Programs do not acquire the peer's owner/operations privileges.
            peer = ActorKey(endpoint.account,'c2c_user','',endpoint.native_conversation_id)
            allowed = self.private_admission(cursor,endpoint,peer)
        if not allowed:
            raise PermissionError('vm_endpoint_unavailable')
        return row

    def issue(self, endpoint, sandbox_id, boot_id, *, expected_route):
        """Host lifecycle only, after checking VM ownership and observing its boot.

        Rotating also invalidates every previously authenticated VMSource. The
        caller must write this one returned token through the guest agent; a
        failed write leaves the previous token revoked rather than falling back.
        """
        if (type(sandbox_id) is not str or SANDBOX_ID_PATTERN.fullmatch(sandbox_id) is None
                or type(boot_id) is not str):
            raise ValueError('invalid_vm_boot')
        try:
            if str(UUID(boot_id))!=boot_id:
                raise ValueError()
        except ValueError:
            raise ValueError('invalid_vm_boot') from None
        token,now = 'gvm_'+secrets.token_urlsafe(32),int(self.clock())
        with self.transaction() as cursor:
            route = self._route(cursor,endpoint)
            if (route['generation'],route['route_epoch'])!=expected_route:
                raise PermissionError('vm_route_replaced')
            row = cursor.execute('''INSERT INTO qq_official_vm_sessions
                (endpoint_key,sandbox_id,boot_id,token_hash,token_generation,account_epoch,
                 endpoint_generation,route_epoch,created_at,updated_at)
                VALUES (?,?,?,?,1,?,?,?,?,?) ON CONFLICT(endpoint_key) DO UPDATE SET
                  sandbox_id=excluded.sandbox_id,boot_id=excluded.boot_id,token_hash=excluded.token_hash,
                  token_generation=qq_official_vm_sessions.token_generation+1,
                  account_epoch=excluded.account_epoch,endpoint_generation=excluded.endpoint_generation,
                  route_epoch=excluded.route_epoch,updated_at=excluded.updated_at,revoked_at=NULL RETURNING *''',
                (endpoint.key,sandbox_id,boot_id,fingerprint(token),self.lease.epoch,*expected_route,now,now)).fetchone()
            return VMCredential(source(row),token)

    def resume(self, endpoint, sandbox_id, observed, *, expected_route):
        """Only a lifecycle observation, never data supplied to an HTTP route.

        A host restart may retain a running VM's token after observing the same
        boot and file fingerprint. Route changes and explicit revocation cannot
        be undone this way; the lifecycle must explicitly issue a new token.
        """
        with self.transaction() as cursor:
            route = self._route(cursor,endpoint)
            if (route['generation'],route['route_epoch'])!=expected_route:
                raise PermissionError('vm_route_replaced')
            row = cursor.execute('SELECT * FROM qq_official_vm_sessions WHERE endpoint_key=?',(endpoint.key,)).fetchone()
            if (row is None or row['revoked_at'] is not None or row['sandbox_id']!=sandbox_id
                    or row['boot_id']!=observed['boot_id'] or row['token_hash']!=observed['token_hash']
                    or (row['endpoint_generation'],row['route_epoch'])!=expected_route):
                return None
            updated = cursor.execute('''UPDATE qq_official_vm_sessions SET account_epoch=?,updated_at=?
                WHERE endpoint_key=? RETURNING *''',(self.lease.epoch,int(self.clock()),endpoint.key)).fetchone()
            return source(updated)

    def discard(self, current):
        """A failed install must not revoke a newer successful installation."""
        with self.transaction() as cursor:
            assert_epoch(cursor,self.lease)
            if current.endpoint.account.key!=self.lease.account_key:
                raise PermissionError('vm_endpoint_unavailable')
            cursor.execute('''UPDATE qq_official_vm_sessions SET revoked_at=?,updated_at=?
                WHERE endpoint_key=? AND token_hash=?''',
                (int(self.clock()),int(self.clock()),current.endpoint.key,current.token_hash))

    def _current(self, cursor, row):
        if row is None or row['revoked_at'] is not None or row['account_epoch']!=self.lease.epoch:
            raise PermissionError('vm_token_revoked')
        current = source(row)
        route = self._route(cursor,current.endpoint)
        if (route['generation'],route['route_epoch'])!=(current.endpoint_generation,current.route_epoch):
            raise PermissionError('vm_route_replaced')
        return current

    def authenticate_cursor(self, cursor, token):
        digest = fingerprint(token)
        assert_epoch(cursor,self.lease)
        row = cursor.execute('SELECT * FROM qq_official_vm_sessions WHERE token_hash=?',(digest,)).fetchone()
        return self._current(cursor,row)

    def authenticate(self, token):
        with self.transaction() as cursor:
            return self.authenticate_cursor(cursor,token)

    def check(self, cursor, current):
        """Recheck a host-authenticated source inside each short action transaction."""
        assert_epoch(cursor,self.lease)
        row = cursor.execute('SELECT * FROM qq_official_vm_sessions WHERE endpoint_key=?',
            (current.endpoint.key,)).fetchone()
        if self._current(cursor,row)!=current:
            raise PermissionError('vm_token_revoked')
        return current

    def revoke_vm(self, sandbox_id, *, idle_owner=None):
        """Check active owner jobs / VM commands and revoke in one account transaction."""
        with self.transaction() as cursor:
            assert_epoch(cursor,self.lease)
            if idle_owner is not None:
                agents = cursor.execute("SELECT DISTINCT endpoint_key FROM background_jobs WHERE account_key=? AND state IN ('queued','running')",
                    (self.lease.account_key,)).fetchall()
                if any(workspace_owner(EndpointKey.parse(row['endpoint_key']))==idle_owner for row in agents):
                    return False
            if idle_owner is not None and cursor.execute('''SELECT 1 FROM qq_official_background_commands c
                    JOIN qq_official_endpoints e USING(endpoint_key)
                    WHERE e.account_key=? AND c.sandbox_id=? AND c.finished_at IS NULL LIMIT 1''',
                    (self.lease.account_key,sandbox_id)).fetchone():
                return False
            cursor.execute('''UPDATE qq_official_vm_sessions SET revoked_at=?,updated_at=? WHERE sandbox_id=?
                AND endpoint_key IN (SELECT endpoint_key FROM qq_official_endpoints WHERE account_key=?)''',
                (int(self.clock()),int(self.clock()),sandbox_id,self.lease.account_key))
            return True

    def revoke(self, endpoint):
        with self.transaction() as cursor:
            assert_epoch(cursor,self.lease)
            if endpoint.account.key!=self.lease.account_key:
                raise PermissionError('vm_endpoint_unavailable')
            cursor.execute('''UPDATE qq_official_vm_sessions SET revoked_at=?,updated_at=? WHERE endpoint_key=?''',
                (int(self.clock()),int(self.clock()),endpoint.key))
