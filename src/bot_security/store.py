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

from src.bot_storage.database import DatabaseError, DatabaseSource, PostgresDatabase
from .passwords import DUMMY_HASH, hash_password, verify_password
from .quorum import ApprovalQuorum


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


class SecurityStore(ApprovalQuorum):
    """Serialize short security transactions across workers, including OTP consumption.

    No network or password hashing takes place while the database lock is held.
    All instances share PostgreSQL; schema changes are managed by Alembic.
    """

    def __init__(self, source: DatabaseSource, secret: bytes, *, clock: Callable[[], float] = time.time, transport=None):
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
        self.transport = transport
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
        if self.transport is not None:
            self.transport.begin(cursor)
        cursor.execute("INSERT INTO admin_security_lock VALUES (1, 0) ON CONFLICT (lock_id) DO NOTHING")
        cursor.execute("UPDATE admin_security_lock SET revision=revision+1 WHERE lock_id=1")

    def _store_where(self, alias=''):
        if self.transport is not None:
            return self.transport.store_where(alias)
        return alias + "identity_transport='onebot'", ()

    def _recipient_where(self, qq_id, bot_id, recipient=None, alias=''):
        if self.transport is not None:
            if qq_id is not None or bot_id is not None:
                raise SecurityError('official_approval_identity_required')
            return self.transport.recipient_where(recipient, alias)
        if recipient is not None:
            raise SecurityError('legacy_approval_identity_required')
        return alias + "identity_transport='onebot' AND " + alias + "qq_id=? AND " + alias + "bot_id=?", (qq_id, bot_id)

    def _same_channel(self, first, second):
        if first.get('identity_transport', 'onebot') != second.get('identity_transport', 'onebot'):
            return False
        if first.get('identity_transport', 'onebot') == 'onebot':
            return first['bot_id'] == second['bot_id']
        return self.transport is not None and self.transport.same_channel(first, second)

    def _voter_key(self, request):
        return request['official_actor_key'] if request.get('identity_transport') == 'qq_official' else request['qq_id']

    def _reuse(self, previous, request):
        if self.transport is not None and not self.transport.same_identity(previous, request):
            raise SecurityError('official_approval_reference_changed', 409)
        return dict(previous), None

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
                cursor.close()
                connection.close()

    def _audit(self, db, account: str, action: str, target: str = "", detail: str = "") -> None:
        db.execute("INSERT INTO admin_security_audit VALUES (?, ?, ?, ?, ?, ?)",
                   (secrets.token_hex(16), account, action, target, detail[:2000], int(self.clock())))

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
            key = digest(token)
            db.execute("DELETE FROM admin_sessions WHERE session_hash=?", (key,))
            db.execute("UPDATE admin_approvals SET status='cancelled', code_hash='', updated_at=? WHERE session_hash=? AND status IN ('sending','pending','awaiting_quorum','queued')", (int(self.clock()), key))

    def _current(self, db, request: dict[str, Any]) -> bool:
        row = db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (request["account_id"],)).fetchone()
        if not row or not row["enabled"] or row["role"] != "admin" or row["version"] != request["account_version"]:
            return False
        if request.get('identity_transport', 'onebot') == 'qq_official':
            if self.transport is None or not self.transport.current(db, request):
                return False
        elif self.transport is not None or request['qq_id'] is None or row['qq_id'] != request['qq_id']:
            return False
        if request["session_hash"]:
            session = db.execute("SELECT expires_at FROM admin_sessions WHERE session_hash=?", (request["session_hash"],)).fetchone()
            if not session or session["expires_at"] <= self.clock():
                return False
        return True

    def _code_hash(self, request: dict[str, Any], code: str) -> str:
        binding = [request[k] for k in ("approval_id", "account_id", "account_version", "qq_id", "bot_id", "payload_hash", "expires_at", "code_generation", "quorum", "parent_approval_id", "parent_generation")]
        if request.get('identity_transport') == 'qq_official':
            if self.transport is None:
                raise SecurityError('official_approval_transport_required')
            binding.extend(self.transport.code_binding(request))
        return hmac.new(self.secret, (canonical(binding) + ":" + code).encode(), hashlib.sha256).hexdigest()

    def _code_fingerprint(self, approval_id: str, code: str) -> str:
        return hmac.new(self.secret, f"previous-code:{approval_id}:{code}".encode(), hashlib.sha256).hexdigest()

    def propose(self, account: dict[str, Any], *, bot_id: str | None, kind: str, payload: dict[str, Any], summary: str, reference: str = "", quorum: int = 1, _parent=None, transport_context=None) -> tuple[dict[str, Any], str | None]:
        quorum = self.required_quorum(kind, quorum)
        if (kind == "quorum_vote") != (_parent is not None):
            raise SecurityError("无效复核对象")
        encoded = canonical(payload)
        if len(encoded.encode()) > 128 * 1024 or not 1 <= len(summary) <= 12000:
            raise SecurityError("操作内容过长，请拆分为可核对的独立操作", 400)
        now = int(self.clock())
        request = dict(approval_id="AP-" + secrets.token_hex(6).upper(), account_id=account["account_id"],
                       account_version=account["version"], qq_id=account["qq_id"], bot_id=bot_id,
                       session_hash=account.get("session_hash", ""), kind=kind, payload_json=encoded,
                       payload_hash=digest(encoded), summary=summary, code_hash="", code_generation=1,
                       attempts=0, expires_at=now + 180, status="sending", result_json="{}",
                       created_at=now, updated_at=now, consumed_at=None, execution_owner="", lease_until=0, notice_sent=0,
                       quorum=quorum, parent_approval_id=_parent[0] if _parent else None,
                       parent_generation=_parent[1] if _parent else None)
        with self.transaction() as db:
            if self.transport is not None:
                if bot_id is not None:
                    raise SecurityError('official_approval_identity_required')
                request.update(self.transport.request_fields(db, account, transport_context, kind=kind, payload=payload))
            elif transport_context is not None:
                raise SecurityError('legacy_approval_identity_required')
            else:
                request['identity_transport'] = 'onebot'
            if not self._current(db, request):
                raise SecurityError("仅已绑定 QQ 的有效管理员可以发起管理操作")
            if _parent:
                parent = self._vote_parent(db, request)
                request["expires_at"] = parent["expires_at"]
                previous = db.execute("SELECT * FROM admin_approvals WHERE parent_approval_id=? AND parent_generation=? AND account_id=?", (*_parent, account["account_id"])).fetchone()
                if previous:
                    return self._reuse(previous, request)
            if quorum == 2:
                count = (self.transport.eligible_count(db, request) if self.transport is not None else
                    db.execute("SELECT COUNT(DISTINCT qq_id) FROM admin_accounts WHERE enabled=1 AND role='admin'").fetchone()[0])
                if count < 2:
                    raise SecurityError("高风险操作需要两位不同的已绑定 QQ 管理员；请先配置第二位管理员", 409)
            if reference:
                previous = db.execute("SELECT a.* FROM admin_approvals a JOIN admin_approval_references r ON r.approval_id=a.approval_id WHERE r.reference=?", (reference,)).fetchone()
                if previous:
                    if previous["account_id"] != account["account_id"] or previous["kind"] != kind or previous["quorum"] != quorum or previous["payload_hash"] != request["payload_hash"]:
                        raise SecurityError("操作引用或参数已变化", 409)
                    return self._reuse(previous, request)
            channel, channel_args = self._store_where()
            existing = db.execute("""SELECT * FROM admin_approvals WHERE account_id=? AND account_version=?
                AND session_hash=? AND kind=? AND payload_hash=? AND quorum=? AND status IN ('sending','pending','awaiting_quorum','queued','executing')
                AND (expires_at>? OR status IN ('queued','executing')) AND """ + channel + " ORDER BY created_at DESC LIMIT 1",
                (request["account_id"], request["account_version"], request["session_hash"], kind, request["payload_hash"], quorum, now, *channel_args)).fetchone()
            if existing:
                return self._reuse(existing, request)
            self._limit(db, "issue:" + request["account_id"], 5, 60)
            code = f"{secrets.randbelow(1_000_000):06d}"
            request["code_hash"] = self._code_hash(request, code)
            columns = ",".join(request)
            placeholders = ",".join("?" for _ in request)
            db.execute(f"INSERT INTO admin_approvals ({columns}) VALUES ({placeholders})", tuple(request.values()))
            if reference:
                db.execute("INSERT INTO admin_approval_references VALUES (?, ?)", (reference, request["approval_id"]))
            db.execute("INSERT INTO admin_approval_codes VALUES (?, ?)", (request["approval_id"], self._code_fingerprint(request["approval_id"], code)))
            self._audit(db, request["account_id"], "approval.created", request["approval_id"], request["payload_hash"])
            if self.transport is not None and not self.transport.issued(db, request, code, self._code_hash):
                db.execute("UPDATE admin_approvals SET status='cancelled',code_hash='' WHERE approval_id=?", (request['approval_id'],))
                request.update(status='cancelled', code_hash='')
                code = None
        return request, code

    def delivered(self, approval_id: str, generation: int, success: bool) -> None:
        with self.transaction() as db:
            if self.transport is not None:
                self.transport.delivered(db, approval_id, generation, success, self._current)
                return
            db.execute("UPDATE admin_approvals SET status=?, updated_at=? WHERE approval_id=? AND code_generation=? AND status='sending'",
                       ("pending" if success else "delivery_failed", int(self.clock()), approval_id, generation))

    def resend(self, approval_id: str, qq_id: str | None, bot_id: str | None, *, recipient=None) -> tuple[dict[str, Any], str | None]:
        identity, identity_args = self._recipient_where(qq_id, bot_id, recipient)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND " + identity, (approval_id, *identity_args)).fetchone()
            if not row or row["status"] not in {"pending", "awaiting_quorum", "delivery_failed", "expired", "locked"}:
                raise SecurityError("这项操作不能重新发送口令")
            request = dict(row)
            if not self._current(db, request):
                raise SecurityError("账户或登录状态已变化，请重新发起操作")
            parent = self._vote_parent(db, request) if request["parent_approval_id"] else None
            self._limit(db, "resend:" + request["account_id"], 3, 180)
            # A six-digit value from ANY earlier resend must never become valid again.
            code = f"{secrets.randbelow(1_000_000):06d}"
            while db.execute("SELECT 1 FROM admin_approval_codes WHERE approval_id=? AND code_fingerprint=?", (approval_id, self._code_fingerprint(approval_id, code))).fetchone():
                code = f"{secrets.randbelow(1_000_000):06d}"
            db.execute("INSERT INTO admin_approval_codes VALUES (?, ?)", (approval_id, self._code_fingerprint(approval_id, code)))
            request.update(code_generation=request["code_generation"] + 1, attempts=0,
                           expires_at=parent["expires_at"] if parent else int(self.clock()) + 180,
                           status="sending", updated_at=int(self.clock()), consumed_at=None)
            if self.transport is not None:
                request['expires_at'] = min(request['expires_at'], request['official_source_deadline_at'])
            request["code_hash"] = self._code_hash(request, code)
            db.execute("UPDATE admin_approvals SET code_generation=?, attempts=0, expires_at=?, status='sending', consumed_at=NULL, updated_at=?, code_hash=? WHERE approval_id=?",
                       (request["code_generation"], request["expires_at"], request["updated_at"], request["code_hash"], approval_id))
            db.execute("UPDATE admin_approvals SET status='cancelled', code_hash='', updated_at=? WHERE parent_approval_id=?", (int(self.clock()), approval_id))
            self._audit(db, request["account_id"], "approval.resent", approval_id)
            if self.transport is not None and not self.transport.issued(db, request, code, self._code_hash):
                db.execute("UPDATE admin_approvals SET status='cancelled',code_hash='' WHERE approval_id=?", (approval_id,))
                request.update(status='cancelled', code_hash='')
                code = None
            return request, code

    def confirm(self, approval_id: str | None, qq_id: str | None, bot_id: str | None, code: str, *, recipient=None) -> dict[str, Any]:
        identity, identity_args = self._recipient_where(qq_id, bot_id, recipient)
        history_identity, history_args = self._recipient_where(qq_id, bot_id, recipient, 'a.')
        error = "口令无效、已使用，或不是本人的操作"
        accepted = None
        with self.transaction() as db:
            bucket = self.transport.recipient_bucket(recipient) if self.transport is not None else qq_id
            self._limit(db, "verify:" + bucket, 15, 60)
            if approval_id is not None:
                row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND " + identity, (approval_id, *identity_args)).fetchone()
                rows = [row] if row else []
            else:
                rows = db.execute("SELECT * FROM admin_approvals WHERE " + identity + " AND expires_at>?",
                                  (*identity_args, int(self.clock()))).fetchall()
                # Include consumed and superseded codes: a repeated code must not
                # accidentally select a different pending operation with the same digits.
                history = db.execute("""SELECT c.approval_id, c.code_fingerprint FROM admin_approval_codes c
                    JOIN admin_approvals a ON a.approval_id=c.approval_id
                    WHERE """ + history_identity + " AND a.expires_at>?",
                    (*history_args, int(self.clock()))).fetchall()
                matches = {r["approval_id"] for r in history if hmac.compare_digest(
                    r["code_fingerprint"], self._code_fingerprint(r["approval_id"], code))}
                if len(matches) > 1:
                    error, rows = "验证码对应多项操作，未批准任何操作；请重发目标操作的验证码。", []
                elif matches:
                    rows = [r for r in rows if r["approval_id"] in matches]
            for row in rows:
                if row["status"] != "pending":
                    continue
                request = dict(row)
                approval_id = request["approval_id"]
                status = "pending"
                attempts = int(request["attempts"])
                if not self._current(db, request):
                    status, error = "cancelled", "账户或登录状态已变化，请重新发起操作"
                elif request["expires_at"] <= self.clock():
                    status, error = "expired", "口令已过期，可私聊重发"
                elif digest(request["payload_json"]) != request["payload_hash"]:
                    status, error = "cancelled", "操作内容已变化，请重新发起"
                elif not re.fullmatch(r"[0-9]{6}", code) or not hmac.compare_digest(request["code_hash"], self._code_hash(request, code)):
                    attempts += 1
                    if attempts >= 5:
                        status, error = "locked", "口令输错 5 次，已作废"
                    self._audit(db, request["account_id"], "approval.code_failed", approval_id)
                else:
                    if request["parent_approval_id"]:
                        self._vote_parent(db, request)
                    status = "succeeded" if request["parent_approval_id"] else "awaiting_quorum" if request["quorum"] > 1 else "queued"
                    accepted = request
                    self._audit(db, request["account_id"], "approval.confirmed", approval_id, request["payload_hash"])
                db.execute("UPDATE admin_approvals SET status=?, attempts=?, updated_at=?, consumed_at=? WHERE approval_id=?",
                           (status, attempts, int(self.clock()), int(self.clock()) if accepted else None, approval_id))
                if accepted is not None:
                    if request["parent_approval_id"]:
                        db.execute("UPDATE admin_approvals SET notice_sent=1, code_hash='' WHERE approval_id=?", (approval_id,))
                    accepted = self._settle_vote(db, request)
        if accepted is None:
            raise SecurityError(error)
        return accepted

    def cancel(self, approval_id: str, qq_id: str | None, bot_id: str | None, *, recipient=None) -> None:
        identity, identity_args = self._recipient_where(qq_id, bot_id, recipient)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND " + identity, (approval_id, *identity_args)).fetchone()
            if not row or not self._current(db, dict(row)) or row["status"] not in {"sending", "pending", "awaiting_quorum", "queued", "delivery_failed", "expired", "locked"}:
                raise SecurityError("操作不存在、无权取消或已经开始执行")
            db.execute("UPDATE admin_approvals SET status='cancelled',code_hash=CASE WHEN identity_transport='qq_official' THEN '' ELSE code_hash END, updated_at=? WHERE approval_id=?", (int(self.clock()), approval_id))
            self._audit(db, row["account_id"], "approval.cancelled", approval_id)

    def get(self, approval_id: str, account_id: str) -> dict[str, Any]:
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            now = int(self.clock())
            db.execute("UPDATE admin_approvals SET status='expired', updated_at=? WHERE approval_id=? AND account_id=? AND status IN ('sending','pending','awaiting_quorum') AND expires_at<=? AND " + channel, (now, approval_id, account_id, now, *channel_args))
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND account_id=? AND " + channel, (approval_id, account_id, *channel_args)).fetchone()
            if not row:
                raise SecurityError("操作不存在", 404)
            return self._public_request(dict(row), db)

    def _public_request(self, row: dict[str, Any], db) -> dict[str, Any]:
        status = row["status"]
        return {"approval_id": row["approval_id"], "status": status, "kind": row["kind"], "summary": row["summary"],
                "expires_at": row["expires_at"], "created_at": row["created_at"],
                "result": json.loads(row["result_json"]), "approval_required": status in {"sending", "pending", "awaiting_quorum"},
                **self.quorum_snapshot(db, row)}

    def pending(self, account_id: str) -> list[dict[str, Any]]:
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            now = int(self.clock())
            db.execute("UPDATE admin_approvals SET status='expired', updated_at=? WHERE status IN ('sending','pending','awaiting_quorum') AND expires_at<=? AND " + channel, (now, now, *channel_args))
            return [self._public_request(dict(row), db) for row in db.execute("SELECT * FROM admin_approvals WHERE account_id=? AND " + channel + " ORDER BY created_at DESC LIMIT 100", (account_id, *channel_args)).fetchall()]

    def claim(self, owner: str) -> dict[str, Any] | None:
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            now = int(self.clock())
            # A lost execution receipt never causes an automatic replay.
            db.execute("UPDATE admin_approvals SET status='needs_attention', updated_at=? WHERE status='executing' AND lease_until<=? AND " + channel, (now, now, *channel_args))
            for row in db.execute("SELECT * FROM admin_approvals WHERE status='queued' AND " + channel + " ORDER BY created_at LIMIT 100", channel_args).fetchall():
                request = dict(row)
                if request["expires_at"] <= now or not self._quorum_valid(db, request):
                    db.execute("UPDATE admin_approvals SET status='cancelled', updated_at=? WHERE approval_id=?", (now, request["approval_id"]))
                    continue
                db.execute("UPDATE admin_approvals SET status='executing', execution_owner=?, lease_until=?, updated_at=? WHERE approval_id=?",
                           (owner, now + 60, now, request["approval_id"]))
                account = dict(db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (request["account_id"],)).fetchone())
                return {**request, "account": public_account(account), "payload": json.loads(request["payload_json"])}
        return None

    def renew(self, approval_id: str, owner: str) -> bool:
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            result = db.execute("UPDATE admin_approvals SET lease_until=? WHERE approval_id=? AND execution_owner=? AND status='executing' AND " + channel, (int(self.clock()) + 60, approval_id, owner, *channel_args))
            return result.rowcount == 1

    def finish(self, approval_id: str, owner: str, result: dict[str, Any], *, status: str) -> None:
        if status not in {"succeeded", "failed", "needs_attention"}:
            raise ValueError("invalid execution result")
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            row = db.execute("SELECT account_id,identity_transport,result_json FROM admin_approvals WHERE approval_id=? AND execution_owner=? AND status='executing' AND " + channel + " FOR UPDATE", (approval_id, owner, *channel_args)).fetchone()
            if row:
                if row['identity_transport'] == 'qq_official':
                    # Host attempts are append-only facts attached to this grant.
                    # A delayed worker result cannot erase or forge them.
                    result = {key: value for key, value in result.items() if key != 'official_effects'}
                    effects = json.loads(row['result_json']).get('official_effects')
                    if effects is not None:
                        result['official_effects'] = effects
                db.execute("UPDATE admin_approvals SET status=?, result_json=?, updated_at=?, code_hash='' WHERE approval_id=?", (status, canonical(result), int(self.clock()), approval_id))
                self._audit(db, row["account_id"], "execution." + status, approval_id)

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
            db.execute("UPDATE admin_approvals SET status='cancelled', updated_at=? WHERE account_id=? AND status IN ('sending','pending','awaiting_quorum','queued')", (int(self.clock()), target["account_id"]))
            self._audit(db, actor["account_id"], "account.updated", target["account_id"], canonical(sorted(payload["changes"])))
            return public_account(dict(db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (target["account_id"],)).fetchone()))

    def audit(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM admin_security_audit ORDER BY created_at DESC LIMIT 200").fetchall()]

    def record_action(self, account_id: str, action: str, target: str, detail: str = "") -> None:
        with self.transaction() as db:
            self._audit(db, account_id, action, target, detail)

    def task_authorization(self, account: dict[str, Any], bot_id: str | None, payload: dict[str, Any]) -> dict[str, Any] | None:
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            row = db.execute("""SELECT * FROM admin_approvals WHERE account_id=?
                AND account_version=? AND bot_id IS NOT DISTINCT FROM ? AND kind='server_task' AND payload_hash=?
                AND """ + channel + " ORDER BY created_at DESC LIMIT 1",
                (account["account_id"], account["version"], bot_id, digest(canonical(payload)), *channel_args)).fetchone()
            if row and self._current(db, dict(row)):
                if row["status"] == "succeeded" and not self._quorum_valid(db, dict(row)):
                    return None
                return self._public_request(dict(row), db)
        return None

    def unsent_receipts(self) -> list[dict[str, Any]]:
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM admin_approvals WHERE notice_sent=0 AND status IN ('succeeded','failed','needs_attention') AND " + channel + " ORDER BY updated_at LIMIT 20", channel_args).fetchall()
            return [{**dict(row), "result": json.loads(row["result_json"])} for row in rows if self._current(db, dict(row))]

    def receipt_sent(self, approval_id: str) -> None:
        channel, channel_args = self._store_where()
        with self.transaction() as db:
            db.execute("UPDATE admin_approvals SET notice_sent=1 WHERE approval_id=? AND " + channel, (approval_id, *channel_args))

    def watch_fleet(self, path: str, account: dict[str, Any], bot_id: str, actor: str, origin: str) -> None:
        if self.transport is not None:
            raise SecurityError('official_fleet_watch_source_not_connected')
        with self.transaction() as db:
            db.execute("INSERT INTO admin_fleet_watches (path, account_json, bot_id, actor, origin, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (path) DO NOTHING",
                (path, canonical(account), bot_id, actor, origin, int(self.clock())))

    def fleet_watches(self) -> list[dict[str, Any]]:
        if self.transport is not None:
            return []
        with self.transaction() as db:
            now = int(self.clock())
            rows = db.execute("SELECT * FROM admin_fleet_watches WHERE status='watching' AND lease_until<=? ORDER BY updated_at LIMIT 20", (now,)).fetchall()
            result = []
            for row in rows:
                account = json.loads(row["account_json"])
                binding = {**account, "account_version": account["version"], "session_hash": account.get("session_hash", "")}
                if not self._current(db, binding):
                    db.execute("UPDATE admin_fleet_watches SET status='cancelled' WHERE path=?", (row["path"],))
                    continue
                db.execute("UPDATE admin_fleet_watches SET lease_until=?, updated_at=? WHERE path=?", (now + 60, now, row["path"]))
                result.append({**dict(row), "account": account})
            return result

    def finish_fleet_watch(self, path: str, *, done: bool = False) -> None:
        with self.transaction() as db:
            db.execute("UPDATE admin_fleet_watches SET status=?, lease_until=? WHERE path=?", ("done" if done else "watching", int(self.clock()) + 10, path))
