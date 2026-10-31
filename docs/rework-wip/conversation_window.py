"""What the model sees of a conversation: a stable chat tail and the per-conversation memory.

The tail starts after a stored message id. It only moves when the tail grows past
the high watermark (then jumps forward to the low watermark) or on /clear, so the
request prefix stays byte-identical between moves and provider caches keep hitting.
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from src.bot_storage import DatabaseSource, open_store_connection

from .context_store import estimate_tokens
from .conversation_scope import ConversationScope
from .ledger import MessageLedger
from .timeline import is_noise, render_line

MAX_TAIL_ROWS = 3000
MEMORY_TOKENS = 3000


class ConversationStore:
    """Window starts and memory files, one row per conversation scope."""

    def __init__(self, path: DatabaseSource) -> None:
        self.path, self._connection = open_store_connection(path)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            cursor = self._connection.cursor()
            try:
                yield cursor
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def tail_after(self, scope_key: str) -> int:
        with self._transaction() as cursor:
            row = cursor.execute("SELECT tail_after FROM conversation_windows WHERE scope_key = ?", (scope_key,)).fetchone()
        return int(row["tail_after"]) if row is not None else 0

    def set_tail_after(self, scope_key: str, message_id: int) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                """INSERT INTO conversation_windows (scope_key, tail_after, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET tail_after = excluded.tail_after, updated_at = excluded.updated_at""",
                (scope_key, int(message_id), int(time.time())),
            )

    def memory(self, scope_key: str) -> str:
        with self._transaction() as cursor:
            row = cursor.execute("SELECT content FROM conversation_memory WHERE scope_key = ?", (scope_key,)).fetchone()
        return str(row["content"]) if row is not None else ""

    def write_memory(self, scope_key: str, content: str, *, append: bool = False) -> str:
        with self._transaction() as cursor:
            row = cursor.execute("SELECT content FROM conversation_memory WHERE scope_key = ? FOR UPDATE",
                                 (scope_key,)).fetchone()
            current = str(row["content"]) if row is not None else ""
            text = (current.rstrip("\n") + "\n" + content.strip() + "\n").lstrip("\n") if append else content
            cursor.execute(
                """INSERT INTO conversation_memory (scope_key, content, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at""",
                (scope_key, text, int(time.time())),
            )
        return text


def build_tail(ledger: MessageLedger, store: ConversationStore, scope: ConversationScope, *,
               high_tokens: int, exclude_ids: frozenset[int] = frozenset()) -> str:
    anchor = store.tail_after(scope.key)
    rows = ledger.history(scope, after_id=anchor, limit=MAX_TAIL_ROWS, newest=True)
    lines = [(message.canonical_message_id, render_line(message, max_chars=2000)) for message in rows
             if message.canonical_message_id not in exclude_ids and not is_noise(message)]
    total = sum(estimate_tokens(line) for _, line in lines)
    if total > high_tokens or len(rows) >= MAX_TAIL_ROWS:
        # Jump forward to about half the budget; the start then stays put again.
        keep, used = [], 0
        for item in reversed(lines):
            used += estimate_tokens(item[1])
            if used > high_tokens // 2:
                break
            keep.append(item)
        keep.reverse()
        if keep:
            store.set_tail_after(scope.key, keep[0][0] - 1)
        lines = keep
    return "\n".join(line for _, line in lines)
