"""Enlist directional-history revocation in the existing ledger clear TX."""
from dataclasses import dataclass, field

from src.plugins.ai_chat.conversation_scope import ConversationScope


@dataclass(frozen=True)
class ClearTarget:
    grant_id: str
    route_id: str
    source_scope_key: str = field(repr=False)
    target_scope_key: str = field(repr=False)
    target_endpoint_key: str = field(repr=False)


def clear_public_history(cursor, scope: ConversationScope, *, lock_routes):
    """Trusted existing clear caller only, not a user permission entry point.

    The mandatory host port acquires all account/route locks in their stable
    order before the grant or physical visibility rows. It authenticates any
    configured official clear source. No new history or permission is granted.
    """
    if not isinstance(scope, ConversationScope) or not callable(lock_routes):
        raise PermissionError('history_clear_unavailable')
    rows=cursor.execute('''SELECT grant_id,route_id,source_scope_key,target_scope_key,target_endpoint_key
        FROM qq_migration_public_history_grants WHERE (source_scope_key=? OR target_scope_key=?)
        AND state IN ('prepared','active') ORDER BY route_id,grant_id''',(scope.key,scope.key)).fetchall()
    targets=tuple(ClearTarget(**dict(row)) for row in rows)
    # Lock the supplied scope even when there are no grants: a concurrent new
    # prepare must serialize with this clear, not slip between its scan/update.
    if lock_routes(cursor, scope=scope, targets=targets) is not None:
        raise PermissionError('history_clear_unavailable')
    # Reread after obtaining route locks; another prepare may have committed
    # before those locks were acquired. The same route's scope lock covers it.
    rows=cursor.execute('''SELECT grant_id FROM qq_migration_public_history_grants
        WHERE (source_scope_key=? OR target_scope_key=?) AND state IN ('prepared','active')
        ORDER BY grant_id FOR UPDATE''',(scope.key,scope.key)).fetchall()
    for row in rows:
        cursor.execute("UPDATE qq_migration_public_history_grants SET state='revoked' WHERE grant_id=?",
                       (row['grant_id'],))


def lock_history_revocation(cursor, *, scope, targets):
    """Lock only to revoke. Deliberately no lease, membership or enabled check.

    Ordinary read/activation uses RouteService.lock instead. A concurrently
    introduced route causes rollback/retry rather than acquiring account locks
    after a scope lock. This keeps clear safe even after the bot left a group.
    """
    from src.plugins.ai_chat.platform_types import EndpointKey

    if not isinstance(scope,ConversationScope):
        raise PermissionError('history_clear_unavailable')
    scopes={scope.key}
    for target in targets:
        scopes.update((target.source_scope_key,target.target_scope_key))
    def discover():
        values=tuple(sorted(scopes))
        placeholders=','.join('?' for _ in values)
        return cursor.execute('''SELECT DISTINCT r.* FROM migration_routes r
            WHERE r.source_scope_key IN ('''+placeholders+') OR r.target_scope_key IN ('+placeholders+')',
            (*values,*values)).fetchall()
    rows=discover()
    endpoints={}
    for row in rows:
        scopes.update((row['source_scope_key'],row['target_scope_key']))
        for key in ('source_endpoint_key','target_endpoint_key'):
            endpoint=EndpointKey.parse(row[key]);endpoints[endpoint.key]=endpoint
    if scope.account is not None:
        endpoint=EndpointKey(scope.account,scope.kind,scope.native_conversation_id)
        endpoints[endpoint.key]=endpoint
    accounts={endpoint.account.key for endpoint in endpoints.values() if endpoint.account.platform=='qq-official'}
    for key in sorted(accounts):
        cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(key,)).fetchone()
    for key,endpoint in sorted(endpoints.items()):
        if endpoint.account.platform=='qq-official':
            cursor.execute('SELECT endpoint_key FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',(key,)).fetchone()
    for key in sorted(scopes):
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(?))',('migration.scope:'+key,))
    current=discover()
    if {r['route_id'] for r in current}!={r['route_id'] for r in rows}:
        raise PermissionError('history_clear_route_changed')
    for row in sorted(rows,key=lambda value:value['route_id']):
        cursor.execute('SELECT route_id FROM migration_routes WHERE route_id=? FOR UPDATE',(row['route_id'],)).fetchone()
