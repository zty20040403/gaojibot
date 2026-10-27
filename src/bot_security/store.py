from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

from src.bot_storage.database import DatabaseError, DatabaseSource, PostgresDatabase
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


class SecurityStore:
    """Serialize short security transactions across workers, including OTP consumption.

    No network or password hashing takes place while the database lock is held.
    All instances share PostgreSQL; schema changes are managed by Alembic.
    """

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

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @contextmanager
    def transaction(self):
        with self._lock:
            if self._closed:
                raise DatabaseError("Security store is closed")
            connection = self._database.store_connection()
            cursor = connection.cursor()
            try:
                cursor.execute("INSERT INTO admin_security_lock VALUES (1, 0) ON CONFLICT (lock_id) DO NOTHING")
                cursor.execute("UPDATE admin_security_lock SET revision=revision+1 WHERE lock_id=1")
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

    def _insert_account(self, db, username: str, encoded: str, role: str, qq_id: str | None, actor: str) -> dict[str, Any]:
        self.validate_account(username, role, qq_id)
        if db.execute("SELECT account_id FROM admin_accounts WHERE username=? OR (qq_id IS NOT NULL AND qq_id=?)", (username, qq_id)).fetchone():
            raise SecurityError("账户名或 QQ 已被绑定", 409)
        account_id = secrets.token_hex(16)
        db.execute("INSERT INTO admin_accounts VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
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
            db.execute("UPDATE admin_approvals SET status='cancelled', code_hash='', updated_at=? WHERE session_hash=? AND status IN ('sending','pending','queued')", (int(self.clock()), key))

    def _current(self, db, request: dict[str, Any]) -> bool:
        row = db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (request["account_id"],)).fetchone()
        if not row or not row["enabled"] or row["role"] != "admin" or row["qq_id"] != request["qq_id"] or row["version"] != request["account_version"]:
            return False
        if request["session_hash"]:
            session = db.execute("SELECT expires_at FROM admin_sessions WHERE session_hash=?", (request["session_hash"],)).fetchone()
            if not session or session["expires_at"] <= self.clock():
                return False
        return True

    def _code_hash(self, request: dict[str, Any], code: str) -> str:
        binding = [request[k] for k in ("approval_id", "account_id", "account_version", "qq_id", "bot_id", "payload_hash", "expires_at", "code_generation")]
        return hmac.new(self.secret, (canonical(binding) + ":" + code).encode(), hashlib.sha256).hexdigest()

    def _code_fingerprint(self, approval_id: str, code: str) -> str:
        return hmac.new(self.secret, f"previous-code:{approval_id}:{code}".encode(), hashlib.sha256).hexdigest()

    def propose(self, account: dict[str, Any], *, bot_id: str, kind: str, payload: dict[str, Any], summary: str, reference: str = "") -> tuple[dict[str, Any], str | None]:
        encoded = canonical(payload)
        if len(encoded.encode()) > 128 * 1024 or not 1 <= len(summary) <= 12000:
            raise SecurityError("操作内容过长，请拆分为可核对的独立操作", 400)
        now = int(self.clock())
        request = dict(approval_id="AP-" + secrets.token_hex(6).upper(), account_id=account["account_id"],
                       account_version=account["version"], qq_id=account["qq_id"], bot_id=bot_id,
                       session_hash=account.get("session_hash", ""), kind=kind, payload_json=encoded,
                       payload_hash=digest(encoded), summary=summary, code_hash="", code_generation=1,
                       attempts=0, expires_at=now + 180, status="sending", result_json="{}",
                       created_at=now, updated_at=now, consumed_at=None, execution_owner="", lease_until=0, notice_sent=0)
        with self.transaction() as db:
            if not self._current(db, request):
                raise SecurityError("仅已绑定 QQ 的有效管理员可以发起管理操作")
            if reference:
                previous = db.execute("SELECT a.* FROM admin_approvals a JOIN admin_approval_references r ON r.approval_id=a.approval_id WHERE r.reference=?", (reference,)).fetchone()
                if previous:
                    if previous["account_id"] != account["account_id"] or previous["payload_hash"] != request["payload_hash"]:
                        raise SecurityError("操作引用或参数已变化", 409)
                    return dict(previous), None
            existing = db.execute("""SELECT * FROM admin_approvals WHERE account_id=? AND account_version=?
                AND session_hash=? AND kind=? AND payload_hash=? AND status IN ('sending','pending','queued','executing')
                AND (expires_at>? OR status IN ('queued','executing')) ORDER BY created_at DESC LIMIT 1""",
                (request["account_id"], request["account_version"], request["session_hash"], kind, request["payload_hash"], now)).fetchone()
            if existing:
                return dict(existing), None
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
        return request, code

    def delivered(self, approval_id: str, generation: int, success: bool) -> None:
        with self.transaction() as db:
            db.execute("UPDATE admin_approvals SET status=?, updated_at=? WHERE approval_id=? AND code_generation=? AND status='sending'",
                       ("pending" if success else "delivery_failed", int(self.clock()), approval_id, generation))

    def resend(self, approval_id: str, qq_id: str, bot_id: str) -> tuple[dict[str, Any], str]:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND qq_id=? AND bot_id=?", (approval_id, qq_id, bot_id)).fetchone()
            if not row or row["status"] not in {"pending", "delivery_failed", "expired", "locked"}:
                raise SecurityError("这项操作不能重新发送口令")
            request = dict(row)
            if not self._current(db, request):
                raise SecurityError("账户或登录状态已变化，请重新发起操作")
            self._limit(db, "resend:" + request["account_id"], 3, 180)
            # A six-digit value from ANY earlier resend must never become valid again.
            code = f"{secrets.randbelow(1_000_000):06d}"
            while db.execute("SELECT 1 FROM admin_approval_codes WHERE approval_id=? AND code_fingerprint=?", (approval_id, self._code_fingerprint(approval_id, code))).fetchone():
                code = f"{secrets.randbelow(1_000_000):06d}"
            db.execute("INSERT INTO admin_approval_codes VALUES (?, ?)", (approval_id, self._code_fingerprint(approval_id, code)))
            request.update(code_generation=request["code_generation"] + 1, attempts=0,
                           expires_at=int(self.clock()) + 180, status="sending", updated_at=int(self.clock()))
            request["code_hash"] = self._code_hash(request, code)
            db.execute("UPDATE admin_approvals SET code_generation=?, attempts=0, expires_at=?, status='sending', updated_at=?, code_hash=? WHERE approval_id=?",
                       (request["code_generation"], request["expires_at"], request["updated_at"], request["code_hash"], approval_id))
            self._audit(db, request["account_id"], "approval.resent", approval_id)
            return request, code

    def confirm(self, approval_id: str | None, qq_id: str, bot_id: str, code: str) -> dict[str, Any]:
        error = "口令无效、已使用，或不是本人的操作"
        accepted = None
        with self.transaction() as db:
            self._limit(db, "verify:" + qq_id, 15, 60)
            if approval_id is not None:
                row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND qq_id=? AND bot_id=?", (approval_id, qq_id, bot_id)).fetchone()
                rows = [row] if row else []
            else:
                rows = db.execute("SELECT * FROM admin_approvals WHERE qq_id=? AND bot_id=? AND expires_at>?",
                                  (qq_id, bot_id, int(self.clock()))).fetchall()
                # Include consumed and superseded codes: a repeated code must not
                # accidentally select a different pending operation with the same digits.
                history = db.execute("""SELECT c.approval_id, c.code_fingerprint FROM admin_approval_codes c
                    JOIN admin_approvals a ON a.approval_id=c.approval_id
                    WHERE a.qq_id=? AND a.bot_id=? AND a.expires_at>?""",
                    (qq_id, bot_id, int(self.clock()))).fetchall()
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
                    status = "queued"
                    accepted = request
                    self._audit(db, request["account_id"], "approval.confirmed", approval_id, request["payload_hash"])
                db.execute("UPDATE admin_approvals SET status=?, attempts=?, updated_at=?, consumed_at=? WHERE approval_id=?",
                           (status, attempts, int(self.clock()), int(self.clock()) if accepted else None, approval_id))
        if accepted is None:
            raise SecurityError(error)
        return {**accepted, "status": "queued"}

    def cancel(self, approval_id: str, qq_id: str, bot_id: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND qq_id=? AND bot_id=?", (approval_id, qq_id, bot_id)).fetchone()
            if not row or not self._current(db, dict(row)) or row["status"] not in {"sending", "pending", "queued", "delivery_failed", "expired", "locked"}:
                raise SecurityError("操作不存在、无权取消或已经开始执行")
            db.execute("UPDATE admin_approvals SET status='cancelled', updated_at=? WHERE approval_id=?", (int(self.clock()), approval_id))
            self._audit(db, row["account_id"], "approval.cancelled", approval_id)

    def get(self, approval_id: str, account_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            now = int(self.clock())
            db.execute("UPDATE admin_approvals SET status='expired', updated_at=? WHERE approval_id=? AND account_id=? AND status IN ('sending','pending') AND expires_at<=?", (now, approval_id, account_id, now))
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND account_id=?", (approval_id, account_id)).fetchone()
            if not row:
                raise SecurityError("操作不存在", 404)
            return self._public_request(dict(row))

    @staticmethod
    def _public_request(row: dict[str, Any]) -> dict[str, Any]:
        status = row["status"]
        return {"approval_id": row["approval_id"], "status": status, "kind": row["kind"], "summary": row["summary"],
                "expires_at": row["expires_at"], "created_at": row["created_at"],
                "result": json.loads(row["result_json"]), "approval_required": status in {"sending", "pending"}}

    def pending(self, account_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            now = int(self.clock())
            db.execute("UPDATE admin_approvals SET status='expired', updated_at=? WHERE status IN ('sending','pending') AND expires_at<=?", (now, now))
            return [self._public_request(dict(row)) for row in db.execute("SELECT * FROM admin_approvals WHERE account_id=? ORDER BY created_at DESC LIMIT 100", (account_id,)).fetchall()]

    def claim(self, owner: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            now = int(self.clock())
            # A lost execution receipt never causes an automatic replay.
            db.execute("UPDATE admin_approvals SET status='needs_attention', updated_at=? WHERE status='executing' AND lease_until<=?", (now, now))
            for row in db.execute("SELECT * FROM admin_approvals WHERE status='queued' ORDER BY created_at LIMIT 100").fetchall():
                request = dict(row)
                if not self._current(db, request) or request["expires_at"] <= now or digest(request["payload_json"]) != request["payload_hash"]:
                    db.execute("UPDATE admin_approvals SET status='cancelled', updated_at=? WHERE approval_id=?", (now, request["approval_id"]))
                    continue
                db.execute("UPDATE admin_approvals SET status='executing', execution_owner=?, lease_until=?, updated_at=? WHERE approval_id=?",
                           (owner, now + 60, now, request["approval_id"]))
                account = dict(db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (request["account_id"],)).fetchone())
                return {**request, "account": public_account(account), "payload": json.loads(request["payload_json"])}
        return None

    def renew(self, approval_id: str, owner: str) -> bool:
        with self.transaction() as db:
            result = db.execute("UPDATE admin_approvals SET lease_until=? WHERE approval_id=? AND execution_owner=? AND status='executing'", (int(self.clock()) + 60, approval_id, owner))
            return result.rowcount == 1

    def finish(self, approval_id: str, owner: str, result: dict[str, Any], *, status: str) -> None:
        if status not in {"succeeded", "failed", "needs_attention"}:
            raise ValueError("invalid execution result")
        with self.transaction() as db:
            row = db.execute("SELECT account_id FROM admin_approvals WHERE approval_id=? AND execution_owner=? AND status='executing'", (approval_id, owner)).fetchone()
            if row:
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
            self.validate_account(new["username"], new["role"], new["qq_id"])
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
            db.execute("UPDATE admin_approvals SET status='cancelled', updated_at=? WHERE account_id=? AND status IN ('sending','pending','queued')", (int(self.clock()), target["account_id"]))
            self._audit(db, actor["account_id"], "account.updated", target["account_id"], canonical(sorted(payload["changes"])))
            return public_account(dict(db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (target["account_id"],)).fetchone()))

    def audit(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM admin_security_audit ORDER BY created_at DESC LIMIT 200").fetchall()]

    def record_action(self, account_id: str, action: str, target: str, detail: str = "") -> None:
        with self.transaction() as db:
            self._audit(db, account_id, action, target, detail)

    def task_authorization(self, account: dict[str, Any], bot_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute("""SELECT * FROM admin_approvals WHERE account_id=?
                AND account_version=? AND bot_id=? AND kind='server_task' AND payload_hash=?
                ORDER BY created_at DESC LIMIT 1""",
                (account["account_id"], account["version"], bot_id, digest(canonical(payload)))).fetchone()
            if row and self._current(db, dict(row)):
                return self._public_request(dict(row))
        return None

    def unsent_receipts(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM admin_approvals WHERE notice_sent=0 AND status IN ('succeeded','failed','needs_attention') ORDER BY updated_at LIMIT 20").fetchall()
            return [{**dict(row), "result": json.loads(row["result_json"])} for row in rows if self._current(db, dict(row))]

    def receipt_sent(self, approval_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE admin_approvals SET notice_sent=1 WHERE approval_id=?", (approval_id,))

    def watch_fleet(self, path: str, account: dict[str, Any], bot_id: str, actor: str, origin: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO admin_fleet_watches (path, account_json, bot_id, actor, origin, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (path) DO NOTHING",
                (path, canonical(account), bot_id, actor, origin, int(self.clock())))

    def fleet_watches(self) -> list[dict[str, Any]]:
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
