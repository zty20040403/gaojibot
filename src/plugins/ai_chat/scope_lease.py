"""Single-row per-scope PostgreSQL lease replacing scattered advisory locks."""

from __future__ import annotations

import time


def _has_scope_leases_table(cursor) -> bool:
    row = cursor.execute("SELECT to_regclass('scope_leases') AS rel").fetchone()
    if row is None:
        return False
    val = row[0] if isinstance(row, tuple) else row["rel"]
    return val is not None


def serialize_scope_writer(
    cursor,
    lease_key: str,
    *,
    owner: str = "host",
    ttl_seconds: int = 300,
    now: int | None = None,
) -> int:
    """Acquire an exclusive transactional row lock on one scope_leases row."""
    if not _has_scope_leases_table(cursor):
        # Historical pre-0111 schema during Alembic migration tests.
        return 1
    current = int(time.time()) if now is None else int(now)
    ph = "%s" if type(cursor).__module__.startswith("psycopg") else "?"
    row = cursor.execute(
        f"""
        INSERT INTO scope_leases (scope_key, lease_owner, lease_generation, expires_at, updated_at)
        VALUES ({ph}, {ph}, 1, {ph}, {ph})
        ON CONFLICT (scope_key) DO UPDATE SET
            lease_owner = EXCLUDED.lease_owner,
            lease_generation = scope_leases.lease_generation + 1,
            expires_at = EXCLUDED.expires_at,
            updated_at = EXCLUDED.updated_at
        RETURNING lease_generation
        """,
        (str(lease_key), str(owner)[:128], current + max(1, int(ttl_seconds)), current),
    ).fetchone()
    if row is None:
        return 1
    return int(row[0] if isinstance(row, tuple) else row["lease_generation"])
