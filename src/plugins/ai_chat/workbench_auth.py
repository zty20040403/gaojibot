"""One-use member login credentials; deliberately independent of admin auth.

The HTTP boundary must check Origin/CSRF and live membership on every request.
Resolving a session only establishes identity, never administrator permission.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets
import threading
import time

from src.bot_storage import DatabaseSource, open_store_connection
from .projects import ProjectAccess, validate_project_access


def _hash(kind, token):
    if not isinstance(token, str) or not re.fullmatch("[A-Za-z0-9_-]{43}", token):
        raise PermissionError("Invalid workbench credential")
    return hashlib.sha256(f"gaoji-workbench:{kind}:{token}".encode()).hexdigest()


@dataclass(frozen=True)
class WorkbenchLogin:
    token: str = field(repr=False)
    expires_at: int


@dataclass(frozen=True)
class WorkbenchSession:
    principal_id: int
    scope_key: str
    expires_at: int
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


class WorkbenchAuthStore:
    def __init__(self, source: DatabaseSource):
        _, self._connection = open_store_connection(source)
        self._lock = threading.RLock()

    def close(self):
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def issue(self, access: ProjectAccess, *, now=None):
        now = int(time.time()) if now is None else now
        token = secrets.token_urlsafe(32)
        with self._transaction() as cursor:
            validate_project_access(cursor, access)
            self._principal_lock(cursor, access.principal_id)
            cursor.execute(
                """UPDATE workbench_login_codes SET revoked_at=? WHERE scope_key=? AND principal_id=?
                AND used_at IS NULL AND revoked_at IS NULL""",
                (now, access.scope.key, access.principal_id),
            )
            cursor.execute(
                """INSERT INTO workbench_login_codes(code_hash,principal_id,scope_key,created_at,expires_at)
                VALUES (?,?,?,?,?)""",
                (
                    _hash("login", token),
                    access.principal_id,
                    access.scope.key,
                    now,
                    now + 600,
                ),
            )
        return WorkbenchLogin(token, now + 600)

    @staticmethod
    def _principal_lock(cursor, principal_id):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?,0))",
            (f"workbench-principal:{principal_id}",),
        )

    def consume(self, token, *, now=None):
        now = int(time.time()) if now is None else now
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._transaction() as cursor:
            code_hash = _hash("login", token)
            identity = cursor.execute(
                "SELECT principal_id FROM workbench_login_codes WHERE code_hash=?",
                (code_hash,),
            ).fetchone()
            if identity is None:
                raise PermissionError("Login link expired, revoked or already used")
            self._principal_lock(cursor, identity["principal_id"])
            row = cursor.execute(
                """UPDATE workbench_login_codes SET used_at=? WHERE code_hash=?
                AND used_at IS NULL AND revoked_at IS NULL AND expires_at>? RETURNING principal_id,scope_key""",
                (now, code_hash, now),
            ).fetchone()
            if row is None:
                raise PermissionError("Login link expired, revoked or already used")
            cursor.execute(
                """INSERT INTO workbench_sessions(session_hash,principal_id,scope_key,csrf_hash,created_at,expires_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    _hash("session", session),
                    row["principal_id"],
                    row["scope_key"],
                    _hash("csrf", csrf),
                    now,
                    now + 7 * 86400,
                ),
            )
            return WorkbenchSession(
                row["principal_id"], row["scope_key"], now + 7 * 86400, session, csrf
            )

    def resolve(self, token, *, csrf_token=None, now=None):
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            row = cursor.execute(
                """SELECT principal_id,scope_key,expires_at,csrf_hash FROM workbench_sessions
                WHERE session_hash=? AND revoked_at IS NULL AND expires_at>?""",
                (_hash("session", token), now),
            ).fetchone()
            if row is None:
                raise PermissionError("Workbench session expired or revoked")
            if csrf_token is not None and not hmac.compare_digest(
                row["csrf_hash"], _hash("csrf", csrf_token)
            ):
                raise PermissionError("Invalid workbench CSRF token")
            return {
                key: row[key] for key in ("principal_id", "scope_key", "expires_at")
            }

    def revoke(self, token, *, now=None):
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE workbench_sessions SET revoked_at=? WHERE session_hash=? AND revoked_at IS NULL",
                (now, _hash("session", token)),
            )

    def revoke_principal(self, principal_id, *, now=None):
        """Trusted membership/account management boundary, never a public login API."""
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            self._principal_lock(cursor, principal_id)
            cursor.execute(
                "UPDATE workbench_sessions SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL",
                (now, principal_id),
            )
            cursor.execute(
                "UPDATE workbench_login_codes SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL",
                (now, principal_id),
            )
