from src.bot_storage import StoreCursor


class ScopeLeaseBusy(RuntimeError):
    pass


def lock_scope(cursor: StoreCursor, scope: str) -> None:
    cursor.execute('INSERT INTO scope_leases(scope_key) VALUES (?) ON CONFLICT DO NOTHING', (scope,))
    cursor.execute('SELECT scope_key FROM scope_leases WHERE scope_key=? FOR UPDATE', (scope,))


def claim_scope(cursor: StoreCursor, scope: str, owner: str, *, now: int, until: int) -> int:
    lock_scope(cursor, scope)
    row = cursor.execute('''UPDATE scope_leases SET lease_owner=?, expires_at=?, lease_generation=lease_generation+1
        WHERE scope_key=? AND expires_at<=? RETURNING lease_generation''', (owner, until, scope, now)).fetchone()
    if row is None:
        raise ScopeLeaseBusy('scope_lease_busy')
    return row['lease_generation']


def renew_scope(cursor: StoreCursor, scope: str, owner: str, generation: int, *, now: int, until: int) -> None:
    result = cursor.execute('''UPDATE scope_leases SET expires_at=?
        WHERE scope_key=? AND lease_owner=? AND lease_generation=? AND expires_at>?''',
        (until, scope, owner, generation, now))
    if not result.rowcount:
        raise ScopeLeaseBusy('scope_lease_lost')
