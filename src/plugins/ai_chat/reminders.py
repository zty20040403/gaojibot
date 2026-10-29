from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Literal

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor, StoreRow

from .conversation_scope import ConversationScope


ReminderStatus = Literal["pending", "sending", "sent", "cancelled"]


@dataclass(frozen=True)
class Reminder:
    reminder_id: int
    scope_key: str
    platform: str
    conversation_kind: str
    native_conversation_id: str
    creator_native_user_id: str
    creator_principal_id: int | None
    message: str
    scheduled_for: int
    next_attempt_at: int
    status: ReminderStatus
    attempts: int
    created_at: int
    sent_at: int | None
    last_error: str

    @property
    def handle(self) -> str:
        return f"reminder#{self.reminder_id}"


class ReminderStore:
    def __init__(self, path: DatabaseSource, *, max_per_scope: int = 50) -> None:
        self.path, self._connection = open_store_connection(path)
        self.max_per_scope = max(int(max_per_scope), 1)
        self._lock = threading.RLock()
        self.source_recorder = None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create(
        self,
        scope: ConversationScope,
        *,
        creator_native_user_id: str | int,
        creator_principal_id: int | None,
        message: str,
        scheduled_for: int,
        now: int | None = None,
    ) -> Reminder:
        created_at = int(now or time.time())
        due_at = int(scheduled_for)
        content = " ".join(message.split()).strip()
        if not content:
            raise ValueError("提醒内容不能为空。")
        if due_at <= created_at:
            raise ValueError("提醒时间必须晚于当前时间。")
        with self._transaction() as cursor:
            from .adapters.onebot_route_context import guard_source_transaction
            guard_source_transaction(cursor)
            active = int(
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM reminders
                    WHERE scope_key = ? AND status IN ('pending', 'sending')
                    """,
                    (scope.key,),
                ).fetchone()[0]
            )
            if active >= self.max_per_scope:
                raise ValueError(
                    f"当前会话最多保留 {self.max_per_scope} 个待办提醒。"
                )
            cursor.execute(
                """
                INSERT INTO reminders (
                    scope_key, platform, conversation_kind,
                    native_conversation_id, creator_native_user_id,
                    creator_principal_id, message, scheduled_for,
                    next_attempt_at, status, attempts, created_at,
                    sent_at, lease_until, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, '')
                """,
                (
                    scope.key,
                    scope.platform,
                    scope.kind,
                    scope.native_conversation_id,
                    str(creator_native_user_id),
                    creator_principal_id,
                    content[:1000],
                    due_at,
                    due_at,
                    created_at,
                ),
            )
            row = cursor.execute(
                "SELECT * FROM reminders WHERE reminder_id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            if self.source_recorder is not None and row is not None and row["platform"] == "onebot-v11":
                self.source_recorder(cursor, self._row(row), created_at)
        if row is None:
            raise RuntimeError("提醒写入失败。")
        return self._row(row)

    def list_pending(self, scope: ConversationScope) -> list[Reminder]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM reminders
                WHERE scope_key = ? AND status IN ('pending', 'sending')
                ORDER BY scheduled_for ASC, reminder_id ASC
                """,
                (scope.key,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def cancel(self, scope: ConversationScope, reminder_id: int) -> bool:
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE reminders SET status = 'cancelled', lease_until = NULL
                WHERE scope_key = ? AND reminder_id = ?
                  AND status IN ('pending', 'sending')
                """,
                (scope.key, int(reminder_id)),
            )
            return cursor.rowcount > 0

    def claim_due(
        self,
        *,
        now: int | None = None,
        limit: int = 10,
        lease_seconds: int = 90,
    ) -> list[Reminder]:
        timestamp = int(now or time.time())
        bounded = min(max(int(limit), 1), 100)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE reminders
                SET status = 'pending', lease_until = NULL,
                    last_error = CASE
                        WHEN last_error = '' THEN '发送租约过期，等待重试'
                        ELSE last_error
                    END
                WHERE platform <> 'qq-official' AND status = 'sending' AND lease_until <= ?
                """,
                (timestamp,),
            )
            lock_clause = "FOR UPDATE SKIP LOCKED"
            rows = cursor.execute(
                f"""
                SELECT * FROM reminders
                WHERE platform <> 'qq-official' AND status = 'pending' AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, reminder_id ASC
                LIMIT ?
                {lock_clause}
                """,
                (timestamp, bounded),
            ).fetchall()
            ids = [int(row["reminder_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cursor.execute(
                    f"""
                    UPDATE reminders
                    SET status = 'sending', lease_until = ?, attempts = attempts + 1
                    WHERE reminder_id IN ({placeholders}) AND status = 'pending'
                    """,
                    [timestamp + max(int(lease_seconds), 10), *ids],
                )
                claimed = cursor.execute(
                    f"""
                    SELECT * FROM reminders
                    WHERE reminder_id IN ({placeholders}) AND status = 'sending'
                    ORDER BY next_attempt_at ASC, reminder_id ASC
                    """,
                    ids,
                ).fetchall()
            else:
                claimed = []
        return [self._row(row) for row in claimed]

    def mark_sent(self, reminder_id: int, *, sent_at: int | None = None) -> bool:
        timestamp = int(sent_at or time.time())
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE reminders
                SET status = 'sent', sent_at = ?, lease_until = NULL, last_error = ''
                WHERE reminder_id = ? AND status = 'sending'
                """,
                (timestamp, int(reminder_id)),
            )
            return cursor.rowcount > 0

    def park_route_attention(self, reminder: Reminder) -> bool:
        """Stop this exact claimed attempt without asserting remote delivery."""
        with self._transaction() as cursor:
            cursor.execute("""UPDATE reminders SET status='cancelled',lease_until=NULL,
                last_error='source_route_needs_attention'
                WHERE reminder_id=? AND status='sending' AND attempts=?""",
                (reminder.reminder_id, reminder.attempts))
            return cursor.rowcount == 1

    def mark_failed(
        self,
        reminder_id: int,
        error: str,
        *,
        now: int | None = None,
        retry_seconds: int = 60,
    ) -> bool:
        timestamp = int(now or time.time())
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE reminders
                SET status = 'pending', next_attempt_at = ?, lease_until = NULL,
                    last_error = ?
                WHERE reminder_id = ? AND status = 'sending'
                """,
                (
                    timestamp + max(int(retry_seconds), 10),
                    str(error)[:500],
                    int(reminder_id),
                ),
            )
            return cursor.rowcount > 0

    @contextmanager
    def _transaction(self) -> Iterator[StoreCursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()

    @staticmethod
    def _row(row: StoreRow) -> Reminder:
        return Reminder(
            reminder_id=int(row["reminder_id"]),
            scope_key=str(row["scope_key"]),
            platform=str(row["platform"]),
            conversation_kind=str(row["conversation_kind"]),
            native_conversation_id=str(row["native_conversation_id"]),
            creator_native_user_id=str(row["creator_native_user_id"]),
            creator_principal_id=(
                int(row["creator_principal_id"])
                if row["creator_principal_id"] is not None
                else None
            ),
            message=str(row["message"]),
            scheduled_for=int(row["scheduled_for"]),
            next_attempt_at=int(row["next_attempt_at"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            attempts=int(row["attempts"]),
            created_at=int(row["created_at"]),
            sent_at=int(row["sent_at"]) if row["sent_at"] is not None else None,
            last_error=str(row["last_error"]),
        )
