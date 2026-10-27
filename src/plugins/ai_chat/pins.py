from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor, StoreRow

from .conversation_scope import ConversationScope
from .ledger import CanonicalMessage, MessageLedger


@dataclass(frozen=True)
class PinnedMessage:
    scope_key: str
    canonical_message_id: int
    pinned_by_principal_id: int | None
    created_at: int


class PinStore:
    def __init__(self, path: DatabaseSource, *, max_per_scope: int = 50) -> None:
        self.path, self._connection = open_store_connection(path)
        self.max_per_scope = max(int(max_per_scope), 1)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def pin(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        canonical_message_id: int,
        *,
        pinned_by_principal_id: int | None,
    ) -> tuple[PinnedMessage, bool]:
        message_id = int(canonical_message_id)
        if ledger.get_any_in_scope(scope, message_id) is None:
            raise ValueError("当前会话中找不到这条规范消息。")
        now = int(time.time())
        with self._transaction() as cursor:
            existing = cursor.execute(
                """
                SELECT * FROM pinned_messages
                WHERE scope_key = ? AND canonical_message_id = ?
                """,
                (scope.key, message_id),
            ).fetchone()
            if existing is not None:
                return self._row(existing), False
            count = int(
                cursor.execute(
                    "SELECT COUNT(*) FROM pinned_messages WHERE scope_key = ?",
                    (scope.key,),
                ).fetchone()[0]
            )
            if count >= self.max_per_scope:
                raise ValueError(
                    f"当前会话最多固定 {self.max_per_scope} 条消息，请先取消旧固定。"
                )
            cursor.execute(
                """
                INSERT INTO pinned_messages (
                    scope_key, canonical_message_id,
                    pinned_by_principal_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    scope.key,
                    message_id,
                    pinned_by_principal_id,
                    now,
                ),
            )
            row = cursor.execute(
                """
                SELECT * FROM pinned_messages
                WHERE scope_key = ? AND canonical_message_id = ?
                """,
                (scope.key, message_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("固定消息写入失败。")
        return self._row(row), True

    def unpin(
        self,
        scope: ConversationScope,
        canonical_message_id: int,
    ) -> bool:
        with self._transaction() as cursor:
            cursor.execute(
                """
                DELETE FROM pinned_messages
                WHERE scope_key = ? AND canonical_message_id = ?
                """,
                (scope.key, int(canonical_message_id)),
            )
            return cursor.rowcount > 0

    def list(self, scope: ConversationScope) -> list[PinnedMessage]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM pinned_messages
                WHERE scope_key = ?
                ORDER BY created_at ASC, canonical_message_id ASC
                """,
                (scope.key,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def protected_message_ids(self, scope: ConversationScope) -> tuple[int, ...]:
        return tuple(item.canonical_message_id for item in self.list(scope))

    def messages(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
    ) -> list[tuple[PinnedMessage, CanonicalMessage]]:
        resolved: list[tuple[PinnedMessage, CanonicalMessage]] = []
        for pin in self.list(scope):
            message = ledger.get_any_in_scope(scope, pin.canonical_message_id)
            if message is not None:
                resolved.append((pin, message))
        return resolved

    def search(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        query: str,
        *,
        limit: int = 10,
    ) -> list[tuple[PinnedMessage, CanonicalMessage]]:
        folded = query.strip().casefold()
        if not folded:
            return []
        return [
            item
            for item in self.messages(ledger, scope)
            if folded in item[1].rendered_text.casefold()
        ][: min(max(int(limit), 1), 50)]

    def render(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        *,
        max_chars: int = 3000,
    ) -> str:
        lines: list[str] = []
        used = 0
        for _pin, message in self.messages(ledger, scope):
            sender = (
                f"[mention#{message.sender_principal_id}] {message.sender_display}"
                if message.sender_principal_id is not None
                else message.sender_display
            )
            line = (
                f"[pinned msg#{message.canonical_message_id} | {sender}] "
                f"{message.rendered_text}"
            )
            if lines and used + len(line) + 1 > max_chars:
                break
            lines.append(line[:max_chars] if not lines else line)
            used += len(lines[-1]) + 1
        return "\n".join(lines)

    @contextmanager
    def _transaction(self) -> Iterator[StoreCursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()

    @staticmethod
    def _row(row: StoreRow) -> PinnedMessage:
        raw_actor = row["pinned_by_principal_id"]
        return PinnedMessage(
            scope_key=str(row["scope_key"]),
            canonical_message_id=int(row["canonical_message_id"]),
            pinned_by_principal_id=(
                int(raw_actor) if raw_actor is not None else None
            ),
            created_at=int(row["created_at"]),
        )
