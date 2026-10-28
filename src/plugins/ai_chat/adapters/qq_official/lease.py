"""Dedicated PostgreSQL session lock plus transactional account fence.

No runtime registration or credential discovery. These synchronous
methods are short blocking database operations; async callers use an executor.
Lease tokens are internal fencing objects, never human authorization grants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import secrets
import threading
import time
from typing import Callable
import weakref

import psycopg
from psycopg import sql

from src.bot_storage.database import StoreCursor
from src.plugins.ai_chat.platform_types import AccountKey


class LeaseUnavailable(RuntimeError):
    """Fixed diagnostic; never includes DSN, account, session, or DB exception."""


class LeaseLost(LeaseUnavailable):
    """The caller must stop gateway intake/dispatch; this token cannot recover."""


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class LeaseToken:
    account_key: str = field(repr=False)
    epoch: int
    holder_id: str = field(repr=False)
    backend_pid: int = field(repr=False)
    advisory_key: int = field(repr=False)


_issued: weakref.WeakValueDictionary[str, LeaseToken] = weakref.WeakValueDictionary()
_issued_lock = threading.RLock()


def advisory_key_for(account: AccountKey) -> int:
    if (not isinstance(account, AccountKey) or account.platform != "qq-official"
            or not 1 <= len(account.key.encode("utf-8")) <= 512):
        raise LeaseUnavailable("Invalid official lease account")
    # The full canonical key contributes; truncating a hash is not truncating an
    # identity. A 64-bit lock collision only denies concurrency, never aliases DB
    # rows. The schema is intentionally omitted: lock scope is the database.
    digest = hashlib.sha256(b"gaoji:qq-official:account-lease:v1\0" + account.key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _lock_parts(key: int) -> tuple[int, int]:
    unsigned = key & ((1 << 64) - 1)
    return unsigned >> 32, unsigned & ((1 << 32) - 1)


def _locally_current(token: LeaseToken) -> bool:
    if not isinstance(token, LeaseToken):
        return False
    with _issued_lock:
        # Identity, not dataclass equality: JSON reconstruction, replace(), or a
        # copied token does not become a locally issued live fencing object.
        return _issued.get(token.holder_id) is token


def assert_epoch(cursor: StoreCursor, token: LeaseToken) -> None:
    """Fence a caller-owned short transaction; success keeps its account lock.

    Caller provides a StoreCursor in the configured schema. No commit or new
    transaction is performed here. A live session lock is checked as well as the
    row epoch, so a killed backend is rejected before another holder takes over.
    The lock check and epoch are not a user grant or a network delivery guarantee.
    """
    if not _locally_current(token):
        raise LeaseLost("Official account lease is no longer valid")
    high, low = _lock_parts(token.advisory_key)
    try:
        row = cursor.execute("""SELECT a.fencing_epoch FROM qq_official_accounts a
            WHERE a.account_key=? AND a.fencing_epoch=? AND a.holder_id=?
              AND a.lock_backend_pid=? AND a.enabled=TRUE AND a.gateway_state<>'paused'
              AND EXISTS (SELECT 1 FROM pg_catalog.pg_locks l
                  WHERE l.locktype='advisory' AND l.granted AND l.pid=a.lock_backend_pid
                    AND l.classid::bigint=? AND l.objid::bigint=? AND l.objsubid=1
                    AND l.database=(SELECT d.oid FROM pg_catalog.pg_database d
                        WHERE d.datname=pg_catalog.current_database()))
            FOR UPDATE OF a""",
            (token.account_key, token.epoch, token.holder_id, token.backend_pid, high, low)).fetchone()
    except Exception:
        # The transaction owner rolls back; do not expose SQL parameters or DSN.
        raise LeaseLost("Official account fence could not be checked") from None
    if row is None or not _locally_current(token):
        raise LeaseLost("Official account lease is no longer valid")


class DedicatedAccountLease:
    """One raw connection for one account session lock; never a pool checkout.

    Construct only after default-off configuration admission. The account row is
    provisioned by trusted local control-plane code, not by this transport lease.
    A busy acquire may be retried; connection loss permanently retires an instance.
    """

    def __init__(
        self,
        dsn: str,
        account: AccountKey,
        *,
        schema: str = "qq_bot",
        clock: Callable[[], float] = time.time,
        connect: Callable[..., psycopg.Connection] = psycopg.connect,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise LeaseUnavailable("Official lease database is not configured")
        if not isinstance(schema, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise LeaseUnavailable("Invalid official lease schema")
        self._key = advisory_key_for(account)
        self._dsn = dsn
        self._account = account
        self._schema = schema
        self._clock = clock
        self._connect = connect
        self._connection: psycopg.Connection | None = None
        self._token: LeaseToken | None = None
        self._closed = False
        self._lost = False
        self._mutex = threading.RLock()

    @property
    def token(self) -> LeaseToken | None:
        with self._mutex:
            return self._token

    @property
    def lost(self) -> bool:
        with self._mutex:
            return self._lost

    def _table(self) -> sql.Composed:
        return sql.SQL("{}.qq_official_accounts").format(sql.Identifier(self._schema))

    def acquire(self) -> LeaseToken | None:
        """Return None when another session holds the account lock; do not wait."""
        with self._mutex:
            if self._closed or self._lost:
                raise LeaseLost("Official account lease instance has ended")
            if self._token is not None:
                if self.probe():
                    return self._token
                raise LeaseLost("Official account lease instance has ended")
            connection = None
            try:
                connection = self._connect(
                    self._dsn, autocommit=True, application_name="gaoji-qq-official-lease",
                    connect_timeout=3, keepalives=1, keepalives_idle=10,
                    keepalives_interval=5, keepalives_count=3, tcp_user_timeout=15000,
                )
                if not isinstance(connection, psycopg.Connection) or not connection.autocommit:
                    raise LeaseUnavailable("Official lease requires a dedicated PostgreSQL connection")
                connection.execute("SET statement_timeout TO '3s'")
                connection.execute("SET lock_timeout TO '1s'")
                locked = connection.execute("SELECT pg_catalog.pg_try_advisory_lock(%s)", (self._key,)).fetchone()
                if locked is None or not locked[0]:
                    connection.close()
                    return None
                holder_id = secrets.token_hex(16)
                backend_pid = int(connection.info.backend_pid)
                with connection.transaction():
                    row = connection.execute(
                        sql.SQL("SELECT enabled,gateway_state FROM {} WHERE account_key=%s FOR UPDATE").format(self._table()),
                        (self._account.key,),
                    ).fetchone()
                    if row is None or not row[0] or row[1] == "paused":
                        raise LeaseUnavailable("Official account is not enabled for gateway acquisition")
                    changed = connection.execute(sql.SQL("""UPDATE {} SET fencing_epoch=fencing_epoch+1,
                        holder_id=%s,lock_backend_pid=%s,gateway_state='connecting',updated_at=%s
                        WHERE account_key=%s RETURNING fencing_epoch""").format(self._table()),
                        (holder_id, backend_pid, int(self._clock()), self._account.key)).fetchone()
                    if changed is None:
                        raise LeaseUnavailable("Official account fence could not be acquired")
                token = LeaseToken(self._account.key, int(changed[0]), holder_id, backend_pid, self._key)
                self._connection = connection
                self._token = token
                with _issued_lock:
                    _issued[holder_id] = token
                return token
            except BaseException as exc:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(exc, LeaseUnavailable):
                    raise
                raise LeaseUnavailable("Official account lease acquisition failed") from None

    def probe(self) -> bool:
        """Verify the original session and fence; do not reconnect or retake lock."""
        with self._mutex:
            token = self._token
            connection = self._connection
            if self._closed or self._lost or token is None or connection is None:
                return False
            high, low = _lock_parts(token.advisory_key)
            try:
                if connection.closed:
                    self._invalidate()
                    return False
                row = connection.execute(sql.SQL("""SELECT a.fencing_epoch,a.holder_id,a.lock_backend_pid,
                    pg_catalog.pg_backend_pid(),a.enabled,a.gateway_state,
                    EXISTS (SELECT 1 FROM pg_catalog.pg_locks l
                        WHERE l.locktype='advisory' AND l.granted AND l.pid=pg_catalog.pg_backend_pid()
                          AND l.classid::bigint=%s AND l.objid::bigint=%s AND l.objsubid=1
                          AND l.database=(SELECT d.oid FROM pg_catalog.pg_database d
                              WHERE d.datname=pg_catalog.current_database()))
                    FROM {} a WHERE a.account_key=%s""").format(self._table()),
                    (high, low, token.account_key)).fetchone()
                valid = (row is not None and row[0] == token.epoch and row[1] == token.holder_id
                         and row[2] == token.backend_pid and row[3] == token.backend_pid
                         and row[4] and row[5] != "paused" and row[6]
                         and _locally_current(token))
            except Exception:
                valid = False
            if not valid:
                self._invalidate()
            return bool(valid)

    def _invalidate(self) -> None:
        self._lost = True
        token, connection = self._token, self._connection
        self._token = None
        self._connection = None
        if token is not None:
            with _issued_lock:
                _issued.pop(token.holder_id, None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def close(self) -> None:
        """Invalidate this token, clear only its own holder, close raw connection."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            token, connection = self._token, self._connection
            self._token = None
            self._connection = None
            if token is not None:
                with _issued_lock:
                    _issued.pop(token.holder_id, None)
            if connection is None:
                return
            try:
                if token is not None and not connection.closed:
                    with connection.transaction():
                        connection.execute(sql.SQL("""UPDATE {} SET holder_id=NULL,lock_backend_pid=NULL,
                            gateway_state=CASE WHEN gateway_state='paused' THEN 'paused'
                                WHEN enabled THEN 'idle' ELSE 'disabled' END,updated_at=%s
                            WHERE account_key=%s AND fencing_epoch=%s AND holder_id=%s
                              AND lock_backend_pid=%s""").format(self._table()),
                            (int(self._clock()), token.account_key, token.epoch, token.holder_id, token.backend_pid))
            except Exception:
                # Closing the session releases locks even if clearing its stale
                # diagnostic row fails. Future acquisition advances the epoch.
                self._lost = True
            finally:
                try:
                    connection.close()
                except Exception:
                    self._lost = True
