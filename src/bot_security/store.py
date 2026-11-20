from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from src.bot_storage.database import DatabaseError, DatabaseSource, PostgresDatabase, StoreCursor
from .passwords import DUMMY_HASH, hash_password, verify_password


class SecurityError(ValueError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {key: account[key] for key in ("account_id", "username", "role", "qq_id", "enabled", "version")}


def lock_admin_security(cursor: StoreCursor) -> None:
    # Advisory locks are database-wide; keep separate schemas independent.
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('gaoji-admin-security:' || current_schema(), 0))")


def write_security_audit(cursor, account, action, target, detail, now):
    cursor.execute("""INSERT INTO admin_audit_log(resource_key,security_audit_id,actor,action,target,security_detail,created_at)
        VALUES ('security',?,?,?,?,?,?)""",
        (secrets.token_hex(16), account, action, target, detail, now))


class SecurityStore:
    """Keep network and password hashing outside short, serialized security transactions."""

    def __init__(self, source: DatabaseSource, secret: bytes, *, clock: Callable[[], float] = time.time):
        if len(secret) < 32:
            raise ValueError("授权密钥至少需要 32 字节")
        self.secret = secret
        self.clock = clock
        self._lock = threading.RLock()
        if not isinstance(source, PostgresDatabase):
            raise DatabaseError(
                "Security storage requires PostgreSQL and Alembic migrations"
            )
        self._database = source
        self._closed = False
        self._caller_cursor = ContextVar('security_store_cursor', default=None)

    @contextmanager
    def enlist(self, cursor):
        """Trusted host only: join its authenticated source/control transaction."""
        token = self._caller_cursor.set(cursor)
        try:
            yield self
        finally:
            self._caller_cursor.reset(token)

    def _security_lock(self, cursor):
        lock_admin_security(cursor)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @contextmanager
    def transaction(self):
        enlisted = self._caller_cursor.get()
        if enlisted is not None:
            savepoint = 'approval_' + secrets.token_hex(8)
            enlisted.execute('SAVEPOINT ' + savepoint)
            try:
                self._security_lock(enlisted)
                yield enlisted
            except BaseException:
                enlisted.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                enlisted.execute('RELEASE SAVEPOINT ' + savepoint)
                raise
            else:
                enlisted.execute('RELEASE SAVEPOINT ' + savepoint)
            return
        with self._lock:
            if self._closed:
                raise DatabaseError("Security store is closed")
            connection = self._database.store_connection()
            cursor = connection.cursor()
            try:
                self._security_lock(cursor)
                yield cursor
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _audit(self, db, account: str, action: str, target: str = "", detail: str = "") -> None:
        write_security_audit(db, account, action, target, detail[:2000], int(self.clock()))

    def _limit(self, db, bucket: str, maximum: int, seconds: int) -> None:
        now = int(self.clock())
        row = db.execute("SELECT * FROM admin_rate_limits WHERE bucket=?", (bucket,)).fetchone()
        if row is None or int(row["window_start"]) + seconds <= now:
            db.execute("INSERT INTO admin_rate_limits VALUES (?, ?, 1) ON CONFLICT (bucket) DO UPDATE SET window_start=excluded.window_start, hits=1", (bucket, now))
        elif int(row["hits"]) >= maximum:
            raise SecurityError("请求过于频繁，请稍后重试", 429)
        else:
            db.execute("UPDATE admin_rate_limits SET hits=hits+1 WHERE bucket=?", (bucket,))

    @staticmethod
    def validate_account(username: str, role: str, qq_id: str | None) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}", username):
            raise SecurityError("账户名需要 3～32 位字母、数字、点、下划线或短横线", 400)
        if role not in {"admin", "member"}:
            raise SecurityError("无效账户角色", 400)
        if (role == "admin" and not qq_id) or (qq_id is not None and not re.fullmatch(r"[1-9][0-9]{4,14}", qq_id)):
            raise SecurityError("管理员必须绑定有效 QQ 号", 400)

    def bootstrap(self, username: str, password: str, qq_id: str) -> dict[str, Any]:
        username = username.strip().lower()
        self.validate_account(username, "admin", qq_id)
        encoded = hash_password(password)
        with self.transaction() as db:
            if db.execute("SELECT account_id FROM admin_accounts LIMIT 1").fetchone():
                raise SecurityError("已有账户，初始化命令不能覆盖现有管理员")
            return self._insert_account(db, username, encoded, "admin", qq_id, "bootstrap")

    def bootstrap_local_admin(self, username: str, password: str) -> dict[str, Any]:
        """Trusted local bootstrap only; creates no transport/owner binding."""
        return self._create_local_admin(username, password, bootstrap=True)

    def add_local_admin(self, username: str, password: str) -> dict[str, Any]:
        """Trusted host CLI only; never exposed as an authenticated web mutation.

        Access to the host database/protected secret is the operator authority.
        This creates no remote owner/binding and cannot confirm any approval.
        """
        return self._create_local_admin(username, password, bootstrap=False)

    def _create_local_admin(self, username: str, password: str, *, bootstrap: bool) -> dict[str, Any]:
        username = username.strip().lower()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}", username):
            raise SecurityError("账户名需要 3～32 位字母、数字、点、下划线或短横线", 400)
        encoded = hash_password(password)
        with self.transaction() as db:
            if bootstrap:
                if db.execute("SELECT account_id FROM admin_accounts LIMIT 1").fetchone():
                    raise SecurityError("已有账户，初始化命令不能覆盖现有管理员")
            elif not db.execute("SELECT 1 FROM admin_accounts WHERE enabled=1 AND role='admin' LIMIT 1").fetchone():
                raise SecurityError("请先初始化管理员账户")
            if db.execute("SELECT 1 FROM admin_accounts WHERE username=?", (username,)).fetchone():
                raise SecurityError("账户名已存在", 409)
            account_id = secrets.token_hex(16)
            db.execute("""INSERT INTO admin_accounts
                (account_id,username,password_hash,role,qq_id,enabled,version,created_at,identity_mode)
                VALUES (?, ?, ?, 'admin', NULL, 1, 1, ?, 'local_admin')""",
                (account_id, username, encoded, int(self.clock())))
            self._audit(db, "bootstrap" if bootstrap else "local-host", "account.created", account_id)
            return public_account(dict(db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (account_id,)).fetchone()))

    def _insert_account(self, db, username: str, encoded: str, role: str, qq_id: str | None, actor: str) -> dict[str, Any]:
        self.validate_account(username, role, qq_id)
        if db.execute("SELECT account_id FROM admin_accounts WHERE username=? OR (qq_id IS NOT NULL AND qq_id=?)", (username, qq_id)).fetchone():
            raise SecurityError("账户名或 QQ 已被绑定", 409)
        account_id = secrets.token_hex(16)
        db.execute("INSERT INTO admin_accounts (account_id,username,password_hash,role,qq_id,enabled,version,created_at) VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
                   (account_id, username, encoded, role, qq_id, int(self.clock())))
        self._audit(db, actor, "account.created", account_id)
        return public_account(dict(db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (account_id,)).fetchone()))

    def accounts(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [public_account(dict(row)) for row in db.execute("SELECT * FROM admin_accounts ORDER BY username").fetchall()]

    def account_for_qq(self, qq_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM admin_accounts WHERE qq_id=? AND enabled=1 AND role='admin'", (qq_id,)).fetchone()
            return public_account(dict(row)) if row else None

    def login(self, username: str, password: str, peer: str, *, lifetime: int = 28800) -> dict[str, Any]:
        username = username.strip().lower()
        with self.transaction() as db:
            self._limit(db, "login-ip:" + digest(peer), 30, 600)
            self._limit(db, "login-user:" + digest(username), 10, 600)
            row = db.execute("SELECT * FROM admin_accounts WHERE username=?", (username,)).fetchone()
            snapshot = dict(row) if row else None
        valid = verify_password(snapshot["password_hash"] if snapshot else DUMMY_HASH, password)
        with self.transaction() as db:
            current = db.execute("SELECT * FROM admin_accounts WHERE username=?", (username,)).fetchone()
            if not valid or not current or not current["enabled"] or not snapshot or current["version"] != snapshot["version"]:
                self._audit(db, "", "login.failed", digest(username))
                result = None
            else:
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                db.execute("INSERT INTO admin_sessions VALUES (?, ?, ?, ?, ?, ?)",
                           (digest(token), current["account_id"], current["version"], digest(csrf), int(self.clock()) + lifetime, int(self.clock())))
                self._audit(db, current["account_id"], "login.succeeded")
                result = {"account": public_account(dict(current)), "session": token, "csrf": csrf}
        if result is None:
            raise SecurityError("账户或密码不正确", 401)
        return result

    def session(self, token: str, csrf: str | None = None) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute("""SELECT a.*, s.session_hash, s.csrf_hash FROM admin_sessions s
                JOIN admin_accounts a ON a.account_id=s.account_id
                WHERE s.session_hash=? AND s.expires_at>? AND a.enabled=1 AND a.version=s.account_version""",
                (digest(token), int(self.clock()))).fetchone()
            if not row:
                raise SecurityError("请登录，或重新登录已过期的账户", 401)
            if csrf is not None and not hmac.compare_digest(row["csrf_hash"], digest(csrf)):
                raise SecurityError("请求校验失败，请刷新页面")
            return {**public_account(dict(row)), "session_hash": row["session_hash"]}

    def logout(self, token: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM admin_sessions WHERE session_hash=?", (digest(token),))

    def apply_account_change(self, actor: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as db:
            current = db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (actor["account_id"],)).fetchone()
            if not current or not current["enabled"] or current["role"] != "admin" or current["version"] != actor["version"]:
                raise SecurityError("管理员权限已变化")
            if payload["action"] == "create":
                return self._insert_account(db, payload["username"], payload["password_hash"], payload["role"], payload.get("qq_id"), actor["account_id"])
            target = db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (payload["account_id"],)).fetchone()
            if not target or target["version"] != payload["expected_version"]:
                raise SecurityError("账户已经变化，请重新确认", 409)
            new = {**dict(target), **payload["changes"]}
            if new.get('identity_mode', 'legacy_qq') == 'legacy_qq':
                self.validate_account(new["username"], new["role"], new["qq_id"])
            elif new['role'] != 'admin' or new['qq_id'] is not None:
                raise SecurityError('official_admin_identity_changed')
            if target["role"] == "admin" and target["enabled"] and (new["role"] != "admin" or not new["enabled"]):
                count = db.execute("SELECT COUNT(*) FROM admin_accounts WHERE role='admin' AND enabled=1").fetchone()[0]
                if count <= 1:
                    raise SecurityError("不能停用或降级最后一个管理员", 409)
            duplicate = db.execute("SELECT account_id FROM admin_accounts WHERE account_id<>? AND qq_id=?", (target["account_id"], new["qq_id"])).fetchone()
            if duplicate:
                raise SecurityError("该 QQ 已被绑定", 409)
            db.execute("UPDATE admin_accounts SET password_hash=?, role=?, qq_id=?, enabled=?, version=version+1 WHERE account_id=?",
                       (new["password_hash"], new["role"], new["qq_id"], int(new["enabled"]), target["account_id"]))
            db.execute("DELETE FROM admin_sessions WHERE account_id=?", (target["account_id"],))
            self._audit(db, actor["account_id"], "account.updated", target["account_id"], canonical(sorted(payload["changes"])))
            return public_account(dict(db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (target["account_id"],)).fetchone()))

    def audit(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [dict(row) for row in db.execute("""SELECT security_audit_id AS audit_id, actor AS account_id,
                action, target, security_detail AS detail, created_at FROM admin_audit_log WHERE security_audit_id IS NOT NULL
                ORDER BY created_at DESC, admin_audit_log.audit_id DESC LIMIT 200""").fetchall()]

    def record_action(self, account_id: str, action: str, target: str, detail: str = "") -> None:
        with self.transaction() as db:
            self._audit(db, account_id, action, target, detail)
