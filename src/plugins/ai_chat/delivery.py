from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Literal

from src.bot_storage import DatabaseSource, PostgresDatabase, open_store_connection

from .conversation_scope import ConversationScope
from .message_ir import MessageBody, body_from_json, body_to_json, render_fallback_text


DeliveryStatus = Literal[
    "pending",
    "sending",
    "ambiguous",
    "committed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class Delivery:
    delivery_id: int
    idempotency_key: str
    source_scope_key: str
    source_canonical_message_id: int | None
    turn_id: int | None
    target_platform: str
    target_kind: str
    target_native_conversation_id: str
    reply_to_native_message_id: str
    body: MessageBody
    content_fingerprint: str
    status: DeliveryStatus
    attempts: int
    next_attempt_at: int
    lease_until: int | None
    native_message_id: str
    last_error: str
    created_at: int
    updated_at: int

    @property
    def handle(self) -> str:
        return f"delivery#{self.delivery_id}"

    @property
    def target_scope(self) -> ConversationScope:
        return ConversationScope(
            self.target_platform,
            self.target_kind,  # type: ignore[arg-type]
            self.target_native_conversation_id,
        )


@dataclass(frozen=True)
class DeliverySummary:
    """Delivery metadata for operator views, without the potentially large body."""

    delivery_id: int
    idempotency_key: str
    source_scope_key: str
    source_canonical_message_id: int | None
    turn_id: int | None
    target_platform: str
    target_kind: str
    target_native_conversation_id: str
    reply_to_native_message_id: str
    content_fingerprint: str
    status: DeliveryStatus
    attempts: int
    next_attempt_at: int
    lease_until: int | None
    native_message_id: str
    last_error: str
    created_at: int
    updated_at: int

    @property
    def handle(self) -> str:
        return f"delivery#{self.delivery_id}"


class DeliveryStore:
    """Durable, lease-based outbound delivery queue.

    A timeout is deliberately parked as ``ambiguous``. It is never retried
    automatically because the remote platform may already have accepted it.
    A later self-message echo can reconcile it, or an operator can explicitly
    requeue it after inspecting the target conversation.
    """

    def __init__(
        self,
        path: DatabaseSource,
        *,
        max_attempts: int = 5,
        lease_seconds: int = 90,
        recover_interrupted: bool = True,
    ) -> None:
        self._legacy_sqlite = not isinstance(path, PostgresDatabase)
        self.path, self._connection = open_store_connection(path)
        self.max_attempts = min(max(int(max_attempts), 1), 50)
        self.lease_seconds = max(int(lease_seconds), 10)
        self._lock = threading.RLock()
        if self._legacy_sqlite:
            self._configure()
            self._migrate()
        # Only a restarted sender may recover leases; maintenance clients coexist with it.
        self.recovered_ambiguous = self.park_interrupted_attempts() if recover_interrupted else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def enqueue(
        self,
        *,
        idempotency_key: str,
        source_scope_key: str,
        target_scope: ConversationScope,
        body: MessageBody,
        reply_to_native_message_id: str | int | None = None,
        source_canonical_message_id: int | None = None,
        turn_id: int | None = None,
        now: int | None = None,
    ) -> tuple[Delivery, bool]:
        key = " ".join(str(idempotency_key).split())[:300]
        if not key:
            raise ValueError("delivery idempotency key must not be empty")
        timestamp = int(time.time() if now is None else now)
        encoded_body = body_to_json(body)
        reply_target = str(reply_to_native_message_id or "")
        fingerprint = body_fingerprint(body, reply_target)
        with self._transaction() as cursor:
            existing = cursor.execute(
                "SELECT * FROM deliveries WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                return self._row(existing), False
            cursor.execute(
                """
                INSERT INTO deliveries (
                    idempotency_key, source_scope_key,
                    source_canonical_message_id, turn_id,
                    target_platform, target_kind,
                    target_native_conversation_id,
                    reply_to_native_message_id, body_json,
                    content_fingerprint, status, attempts,
                    next_attempt_at, lease_until, native_message_id,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0,
                          ?, NULL, '', '', ?, ?)
                """,
                (
                    key,
                    source_scope_key,
                    source_canonical_message_id,
                    turn_id,
                    target_scope.platform,
                    target_scope.kind,
                    target_scope.native_conversation_id,
                    reply_target,
                    encoded_body,
                    fingerprint,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = cursor.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        if row is None:
            raise RuntimeError("delivery was not stored")
        return self._row(row), True

    def begin_direct_attempt(
        self,
        delivery_id: int,
        *,
        now: int | None = None,
    ) -> Delivery | None:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (int(delivery_id),),
            ).fetchone()
            if row is None or str(row["status"]) not in {"pending", "failed"}:
                return None
            attempt = int(row["attempts"]) + 1
            cursor.execute(
                """
                UPDATE deliveries
                SET status = 'sending', attempts = ?, lease_until = ?,
                    updated_at = ?, last_error = ''
                WHERE delivery_id = ?
                """,
                (
                    attempt,
                    timestamp + self.lease_seconds,
                    timestamp,
                    int(delivery_id),
                ),
            )
            self._insert_attempt(cursor, int(delivery_id), attempt, timestamp)
            stored = cursor.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (int(delivery_id),),
            ).fetchone()
        return self._row(stored) if stored is not None else None

    def claim_due(
        self,
        *,
        now: int | None = None,
        limit: int = 20,
        exclude_platforms: tuple[str, ...] = (),
    ) -> list[Delivery]:
        timestamp = int(time.time() if now is None else now)
        bounded = min(max(int(limit), 1), 100)
        platform_clause = " AND target_platform NOT IN (" + ",".join("?" for _ in exclude_platforms) + ")" if exclude_platforms else ""
        with self._transaction() as cursor:
            lock_clause = "" if self._legacy_sqlite else "FOR UPDATE SKIP LOCKED"
            rows = cursor.execute(
                f"""
                SELECT * FROM deliveries
                WHERE status = 'pending' AND next_attempt_at <= ?
                  AND attempts < ?
                  {platform_clause}
                ORDER BY next_attempt_at ASC, delivery_id ASC
                LIMIT ?
                {lock_clause}
                """,
                (timestamp, self.max_attempts, *exclude_platforms, bounded),
            ).fetchall()
            claimed = []
            for row in rows:
                delivery_id = int(row["delivery_id"])
                attempt = int(row["attempts"]) + 1
                cursor.execute(
                    """
                    UPDATE deliveries
                    SET status = 'sending', attempts = ?, lease_until = ?,
                        updated_at = ?, last_error = ''
                    WHERE delivery_id = ? AND status = 'pending'
                    """,
                    (
                        attempt,
                        timestamp + self.lease_seconds,
                        timestamp,
                        delivery_id,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                self._insert_attempt(cursor, delivery_id, attempt, timestamp)
                stored = cursor.execute(
                    "SELECT * FROM deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if stored is not None:
                    claimed.append(stored)
        return [self._row(row) for row in claimed]

    def defer_unsent(self, delivery: Delivery, reason: str, *, delay_seconds: int = 30) -> bool:
        """Only before calling the transport: disconnection is not a send attempt."""
        now = int(time.time())
        with self._transaction() as cursor:
            cursor.execute("""UPDATE deliveries SET status='pending', attempts=attempts-1,
                next_attempt_at=?, lease_until=NULL, updated_at=?, last_error=?
                WHERE delivery_id=? AND status='sending' AND attempts=? AND lease_until>?""",
                (now + delay_seconds, now, reason[:1000], delivery.delivery_id, delivery.attempts, now))
            changed = cursor.rowcount == 1
            if changed:
                cursor.execute("DELETE FROM delivery_attempts WHERE delivery_id=? AND attempt=? AND state='sending'",
                    (delivery.delivery_id, delivery.attempts))
            return changed

    def ambiguous_finals(self, *, limit: int = 100) -> list[Delivery]:
        with self._lock:
            rows = self._connection.execute("""SELECT * FROM deliveries
                WHERE status='ambiguous' AND idempotency_key LIKE ?
                ORDER BY updated_at, delivery_id LIMIT ?""", ("subagent-final:%", limit)).fetchall()
        return [self._row(row) for row in rows]

    def mark_committed(
        self,
        delivery_id: int,
        *,
        native_message_id: str | int = "",
        now: int | None = None,
        detail: str = "",
    ) -> bool:
        return self._finish(
            delivery_id,
            "committed",
            native_message_id=str(native_message_id or ""),
            error=detail,
            now=now,
        )

    def mark_ambiguous(
        self,
        delivery_id: int,
        error: str,
        *,
        now: int | None = None,
    ) -> bool:
        return self._finish(
            delivery_id,
            "ambiguous",
            error=error,
            now=now,
        )

    def mark_failed(
        self,
        delivery_id: int,
        error: str,
        *,
        retryable: bool = False,
        retry_seconds: int = 30,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT attempts, status FROM deliveries WHERE delivery_id = ?",
                (int(delivery_id),),
            ).fetchone()
            if row is None or str(row["status"]) != "sending":
                return False
            should_retry = retryable and int(row["attempts"]) < self.max_attempts
            status = "pending" if should_retry else "failed"
            next_attempt = (
                timestamp + max(int(retry_seconds), 5)
                if should_retry
                else timestamp
            )
            cursor.execute(
                """
                UPDATE deliveries
                SET status = ?, next_attempt_at = ?, lease_until = NULL,
                    last_error = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'sending'
                """,
                (
                    status,
                    next_attempt,
                    str(error)[:1000],
                    timestamp,
                    int(delivery_id),
                ),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._finish_attempt(
                    cursor,
                    int(delivery_id),
                    status,
                    str(error)[:1000],
                    timestamp,
                )
            return changed

    def requeue(self, delivery_id: int, *, now: int | None = None) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE deliveries
                SET status = 'pending', next_attempt_at = ?, lease_until = NULL,
                    last_error = '', updated_at = ?
                WHERE delivery_id = ?
                  AND status IN ('ambiguous', 'failed')
                  AND attempts < ?
                """,
                (timestamp, timestamp, int(delivery_id), self.max_attempts),
            )
            return cursor.rowcount == 1

    def cancel(self, delivery_id: int, *, now: int | None = None) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE deliveries
                SET status = 'cancelled', lease_until = NULL, updated_at = ?
                WHERE delivery_id = ?
                  AND status IN ('pending', 'ambiguous', 'failed')
                """,
                (timestamp, int(delivery_id)),
            )
            return cursor.rowcount == 1

    def reconcile_echo(
        self,
        target_scope: ConversationScope,
        body: MessageBody,
        *,
        native_message_id: str | int,
        reply_to_native_message_id: str | int | None = None,
        observed_at: int | None = None,
        window_seconds: int = 600,
    ) -> Delivery | None:
        timestamp = int(time.time() if observed_at is None else observed_at)
        fingerprint = body_fingerprint(
            body,
            str(reply_to_native_message_id or ""),
        )
        with self._transaction() as cursor:
            row = cursor.execute(
                """
                SELECT * FROM deliveries
                WHERE target_platform = ? AND target_kind = ?
                  AND target_native_conversation_id = ?
                  AND content_fingerprint = ?
                  AND status IN ('sending', 'ambiguous', 'pending')
                  AND created_at BETWEEN ? AND ?
                ORDER BY
                  CASE status WHEN 'ambiguous' THEN 0 WHEN 'sending' THEN 1 ELSE 2 END,
                  delivery_id ASC
                LIMIT 1
                """,
                (
                    target_scope.platform,
                    target_scope.kind,
                    target_scope.native_conversation_id,
                    fingerprint,
                    timestamp - max(int(window_seconds), 30),
                    timestamp + 60,
                ),
            ).fetchone()
            if row is None:
                return None
            delivery_id = int(row["delivery_id"])
            cursor.execute(
                """
                UPDATE deliveries
                SET status = 'committed', native_message_id = ?,
                    lease_until = NULL, last_error = '', updated_at = ?
                WHERE delivery_id = ?
                """,
                (str(native_message_id), timestamp, delivery_id),
            )
            self._finish_attempt(
                cursor,
                delivery_id,
                "committed",
                "reconciled by self-message echo",
                timestamp,
            )
            stored = cursor.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return self._row(stored) if stored is not None else None

    def park_interrupted_attempts(self, *, now: int | None = None) -> int:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            rows = cursor.execute(
                "SELECT delivery_id FROM deliveries WHERE status = 'sending'"
            ).fetchall()
            cursor.execute(
                """
                UPDATE deliveries
                SET status = 'ambiguous', lease_until = NULL,
                    last_error = '进程在确认平台回执前中断', updated_at = ?
                WHERE status = 'sending'
                """,
                (timestamp,),
            )
            for row in rows:
                self._finish_attempt(
                    cursor,
                    int(row["delivery_id"]),
                    "ambiguous",
                    "process interrupted before receipt confirmation",
                    timestamp,
                )
            return len(rows)

    def park_expired_attempts(self, *, now: int | None = None) -> int:
        """Park sends whose worker disappeared after claiming the lease.

        Expiry does not prove that the platform rejected the message. Treating
        it as retryable could duplicate a successful send, so it follows the
        same outcome-unknown path as a process restart.
        """
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            rows = cursor.execute(
                """
                SELECT delivery_id FROM deliveries
                WHERE status = 'sending'
                  AND lease_until IS NOT NULL
                  AND lease_until <= ?
                """,
                (timestamp,),
            ).fetchall()
            cursor.execute(
                """
                UPDATE deliveries
                SET status = 'ambiguous', lease_until = NULL,
                    last_error = '投递租约到期，平台结果未知', updated_at = ?
                WHERE status = 'sending'
                  AND lease_until IS NOT NULL
                  AND lease_until <= ?
                """,
                (timestamp, timestamp),
            )
            for row in rows:
                self._finish_attempt(
                    cursor,
                    int(row["delivery_id"]),
                    "ambiguous",
                    "delivery lease expired before receipt confirmation",
                    timestamp,
                )
            return len(rows)

    def get(self, delivery_id: int) -> Delivery | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (int(delivery_id),),
            ).fetchone()
        return self._row(row) if row is not None else None

    def find_by_key(self, idempotency_key: str) -> Delivery | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM deliveries WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self._row(row) if row is not None else None

    def recent(self, *, limit: int = 100) -> list[Delivery]:
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM deliveries ORDER BY delivery_id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def recent_summaries(self, *, limit: int = 100) -> list[DeliverySummary]:
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    delivery_id, idempotency_key, source_scope_key,
                    source_canonical_message_id, turn_id, target_platform,
                    target_kind, target_native_conversation_id,
                    reply_to_native_message_id, content_fingerprint, status,
                    attempts, next_attempt_at, lease_until, native_message_id,
                    last_error, created_at, updated_at
                FROM deliveries
                ORDER BY delivery_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [self._summary_row(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status"
            ).fetchall()
        result = {
            "pending": 0,
            "sending": 0,
            "ambiguous": 0,
            "committed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for row in rows:
            result[str(row["status"])] = int(row["count"])
        return result

    def _finish(
        self,
        delivery_id: int,
        status: Literal["committed", "ambiguous"],
        *,
        native_message_id: str = "",
        error: str = "",
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE deliveries
                SET status = ?, native_message_id = ?, lease_until = NULL,
                    last_error = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'sending'
                """,
                (
                    status,
                    native_message_id,
                    str(error)[:1000],
                    timestamp,
                    int(delivery_id),
                ),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._finish_attempt(
                    cursor,
                    int(delivery_id),
                    status,
                    str(error)[:1000],
                    timestamp,
                )
            return changed

    @staticmethod
    def _insert_attempt(
        cursor: sqlite3.Cursor,
        delivery_id: int,
        attempt: int,
        started_at: int,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO delivery_attempts (
                delivery_id, attempt, state, detail, started_at, finished_at
            ) VALUES (?, ?, 'sending', '', ?, NULL)
            """,
            (delivery_id, attempt, started_at),
        )

    @staticmethod
    def _finish_attempt(
        cursor: sqlite3.Cursor,
        delivery_id: int,
        state: str,
        detail: str,
        finished_at: int,
    ) -> None:
        cursor.execute(
            """
            UPDATE delivery_attempts
            SET state = ?, detail = ?, finished_at = ?
            WHERE attempt_id = (
                SELECT attempt_id FROM delivery_attempts
                WHERE delivery_id = ?
                ORDER BY attempt DESC, attempt_id DESC LIMIT 1
            ) AND finished_at IS NULL
            """,
            (state, detail, finished_at, delivery_id),
        )

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
            if self.path is not None:
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")

    def _migrate(self) -> None:
        with self._transaction() as cursor:
            cursor.executescript(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    source_scope_key TEXT NOT NULL,
                    source_canonical_message_id INTEGER,
                    turn_id INTEGER,
                    target_platform TEXT NOT NULL,
                    target_kind TEXT NOT NULL CHECK(target_kind IN ('group', 'private')),
                    target_native_conversation_id TEXT NOT NULL,
                    reply_to_native_message_id TEXT NOT NULL DEFAULT '',
                    body_json TEXT NOT NULL,
                    content_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending', 'sending', 'ambiguous',
                        'committed', 'failed', 'cancelled'
                    )),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL,
                    lease_until INTEGER,
                    native_message_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deliveries_due
                    ON deliveries(status, next_attempt_at, delivery_id);
                CREATE INDEX IF NOT EXISTS idx_deliveries_echo
                    ON deliveries(
                        target_platform, target_kind,
                        target_native_conversation_id,
                        content_fingerprint, status, created_at
                    );

                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
                    attempt INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    UNIQUE(delivery_id, attempt)
                );
                """
            )
            columns = {
                str(row[1])
                for row in cursor.execute(
                    "PRAGMA table_info(deliveries)"
                ).fetchall()
            }
            if "reply_to_native_message_id" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE deliveries
                    ADD COLUMN reply_to_native_message_id TEXT NOT NULL DEFAULT ''
                    """
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Delivery:
        lease = row["lease_until"]
        source_message_id = row["source_canonical_message_id"]
        turn_id = row["turn_id"]
        return Delivery(
            delivery_id=int(row["delivery_id"]),
            idempotency_key=str(row["idempotency_key"]),
            source_scope_key=str(row["source_scope_key"]),
            source_canonical_message_id=(
                int(source_message_id) if source_message_id is not None else None
            ),
            turn_id=int(turn_id) if turn_id is not None else None,
            target_platform=str(row["target_platform"]),
            target_kind=str(row["target_kind"]),
            target_native_conversation_id=str(
                row["target_native_conversation_id"]
            ),
            reply_to_native_message_id=str(row["reply_to_native_message_id"]),
            body=body_from_json(str(row["body_json"])),
            content_fingerprint=str(row["content_fingerprint"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            attempts=int(row["attempts"]),
            next_attempt_at=int(row["next_attempt_at"]),
            lease_until=int(lease) if lease is not None else None,
            native_message_id=str(row["native_message_id"]),
            last_error=str(row["last_error"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def _summary_row(row: sqlite3.Row) -> DeliverySummary:
        lease = row["lease_until"]
        source_message_id = row["source_canonical_message_id"]
        turn_id = row["turn_id"]
        return DeliverySummary(
            delivery_id=int(row["delivery_id"]),
            idempotency_key=str(row["idempotency_key"]),
            source_scope_key=str(row["source_scope_key"]),
            source_canonical_message_id=(
                int(source_message_id) if source_message_id is not None else None
            ),
            turn_id=int(turn_id) if turn_id is not None else None,
            target_platform=str(row["target_platform"]),
            target_kind=str(row["target_kind"]),
            target_native_conversation_id=str(
                row["target_native_conversation_id"]
            ),
            reply_to_native_message_id=str(row["reply_to_native_message_id"]),
            content_fingerprint=str(row["content_fingerprint"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            attempts=int(row["attempts"]),
            next_attempt_at=int(row["next_attempt_at"]),
            lease_until=int(lease) if lease is not None else None,
            native_message_id=str(row["native_message_id"]),
            last_error=str(row["last_error"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


def body_fingerprint(body: MessageBody, reply_to_native_message_id: str = "") -> str:
    visible = " ".join(render_fallback_text(body).split()).casefold()
    payload = f"reply:{reply_to_native_message_id}\n{visible}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
