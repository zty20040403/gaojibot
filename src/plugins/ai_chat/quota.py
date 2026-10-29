from __future__ import annotations

import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class QuotaStatus:
    scope_key: str
    day: str
    calls: int
    input_tokens: int
    output_tokens: int
    call_limit: int
    input_limit: int
    output_limit: int

    @property
    def allowed(self) -> bool:
        return not (
            (self.call_limit > 0 and self.calls >= self.call_limit)
            or (self.input_limit > 0 and self.input_tokens >= self.input_limit)
            or (
                self.output_limit > 0
                and self.output_tokens >= self.output_limit
            )
        )


class UsageStore:
    def __init__(
        self,
        path: DatabaseSource,
        *,
        daily_call_limit: int = 0,
        daily_input_token_limit: int = 0,
        daily_output_token_limit: int = 0,
    ) -> None:
        self.path, self._connection = open_store_connection(path)
        self.daily_call_limit = max(int(daily_call_limit), 0)
        self.daily_input_token_limit = max(int(daily_input_token_limit), 0)
        self.daily_output_token_limit = max(int(daily_output_token_limit), 0)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def record(
        self,
        *,
        scope_key: str,
        source: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        turn_id: int | None = None,
        occurred_at: int | None = None,
        transaction_cursor: StoreCursor | None = None,
    ) -> int:
        timestamp = int(time.time() if occurred_at is None else occurred_at)
        with (
            nullcontext(transaction_cursor)
            if transaction_cursor is not None
            else self._transaction()
        ) as cursor:
            cursor.execute(
                """
                INSERT INTO usage_events (
                    scope_key, source, provider, model,
                    input_tokens, output_tokens, cached_tokens,
                    turn_id, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_key,
                    str(source)[:50],
                    str(provider)[:100],
                    str(model)[:200],
                    max(int(input_tokens), 0),
                    max(int(output_tokens), 0),
                    max(int(cached_tokens), 0),
                    turn_id,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def reserve_routine(
        self,
        *,
        run_id: str,
        attempt_id: str,
        now: int | None = None,
        transaction_cursor: StoreCursor | None = None,
    ) -> bool:
        """Reserve before execution; lost attempts retain their full charge.

        Limits and ownership come from persisted definitions, not tool arguments.
        Like ordinary quotas, the accounting day is always Asia/Shanghai.
        """
        if not attempt_id or len(attempt_id) > 64:
            raise ValueError("Invalid routine attempt identifier")
        timestamp = int(time.time() if now is None else now)
        _, _, day = _day_bounds(timestamp)
        with (
            nullcontext(transaction_cursor)
            if transaction_cursor is not None
            else self._transaction()
        ) as cursor:
            spec = cursor.execute(
                """SELECT a.* FROM agent_routines a JOIN routine_runs r USING(routine_id)
                   WHERE r.run_id=? AND a.enabled AND a.version=r.version
                     AND r.status IN ('queued','running') FOR UPDATE OF a""",
                (run_id,),
            ).fetchone()
            if spec is None:
                raise ValueError("Routine run is not executable")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                (f"routine-budget:{spec['budget_origin_routine_id'] or spec['routine_id']}:{day}",),
            )
            existing = cursor.execute(
                "SELECT * FROM routine_budget_reservations WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing["run_id"] != run_id:
                    raise ValueError("Routine attempt already belongs to another run")
                return existing["status"] == "reserved"
            totals = cursor.execute(
                """SELECT COALESCE(SUM(charged_tokens),0) AS charged,
                          COUNT(DISTINCT run_id) AS runs,
                          COALESCE(BOOL_OR(run_id=?),FALSE) AS same_run
                   FROM routine_budget_reservations b JOIN agent_routines origin ON origin.routine_id=b.routine_id WHERE COALESCE(origin.budget_origin_routine_id,origin.routine_id)=? AND b.day=?""",
                (run_id, spec["budget_origin_routine_id"] or spec["routine_id"], day),
            ).fetchone()
            if totals["charged"] + spec["max_tokens"] > spec["daily_tokens"] or (
                not totals["same_run"] and totals["runs"] >= spec["daily_runs"]
            ):
                return False
            cursor.execute(
                """INSERT INTO routine_budget_reservations
                   (attempt_id,run_id,routine_id,scope_key,day,reserved_tokens,
                    charged_tokens,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,'reserved',?,?)""",
                (
                    attempt_id,
                    run_id,
                    spec["routine_id"],
                    spec["scope_key"],
                    day,
                    spec["max_tokens"],
                    spec["max_tokens"],
                    timestamp,
                    timestamp,
                ),
            )
            return True

    def settle_routine(
        self,
        attempt_id: str,
        *,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        usage_known: bool,
        now: int | None = None,
    ) -> int | None:
        """Settle once, recording known usage without inventing lost receipts."""
        if any(
            type(value) is not int or value < 0
            for value in (input_tokens, output_tokens, cached_tokens)
        ):
            raise ValueError("Usage must be nonnegative integer token counts")
        if cached_tokens > input_tokens:
            raise ValueError("Cached tokens cannot exceed input tokens")
        timestamp = int(time.time() if now is None else now)
        total = input_tokens + output_tokens
        with self._transaction() as cursor:
            reservation = cursor.execute(
                "SELECT * FROM routine_budget_reservations WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if reservation is None:
                raise ValueError("Unknown routine attempt")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                (f"routine-budget:{cursor.execute('SELECT COALESCE(budget_origin_routine_id,routine_id) FROM agent_routines WHERE routine_id=?', (reservation['routine_id'],)).fetchone()[0]}:{reservation['day']}",),
            )
            reservation = cursor.execute(
                "SELECT * FROM routine_budget_reservations WHERE attempt_id=? FOR UPDATE",
                (attempt_id,),
            ).fetchone()
            if reservation["status"] != "reserved":
                return reservation["usage_event_id"]
            run = cursor.execute(
                "SELECT turn_id FROM routine_runs WHERE run_id=?",
                (reservation["run_id"],),
            ).fetchone()
            usage_id = None
            if usage_known or total:
                usage_id = self.record(
                    scope_key=reservation["scope_key"],
                    source=f"routine:{reservation['routine_id']}",
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    turn_id=run["turn_id"],
                    occurred_at=timestamp,
                    transaction_cursor=cursor,
                )
            # Unknown or partial receipts never release capacity for another retry.
            charged = (
                total if usage_known else max(total, reservation["reserved_tokens"])
            )
            cursor.execute(
                """UPDATE routine_budget_reservations SET charged_tokens=?,status=?,
                          usage_event_id=?,updated_at=? WHERE attempt_id=?""",
                (
                    charged,
                    "settled" if usage_known else "unknown",
                    usage_id,
                    timestamp,
                    attempt_id,
                ),
            )
            return usage_id

    def routine_status(
        self,
        routine_id: int,
        *,
        scope_key: str,
        bot_id: str,
        creator_id: str,
        now: int | None = None,
    ) -> dict[str, object] | None:
        _, _, day = _day_bounds(now)
        with self._lock:
            row = self._connection.execute(
                """SELECT a.routine_id,a.daily_tokens,a.daily_runs,
                          COUNT(DISTINCT b.run_id) AS runs,COUNT(b.attempt_id) AS attempts,
                          COALESCE(SUM(b.charged_tokens),0) AS charged_tokens,
                          COALESCE(SUM(CASE WHEN b.status='reserved' THEN b.charged_tokens ELSE 0 END),0) AS pending_tokens
                   FROM agent_routines a LEFT JOIN routine_budget_reservations b
                     ON b.routine_id IN (SELECT member.routine_id FROM agent_routines member
                       WHERE COALESCE(member.budget_origin_routine_id,member.routine_id)
                         =COALESCE(a.budget_origin_routine_id,a.routine_id)) AND b.day=?
                   WHERE a.routine_id=? AND a.scope_key=? AND a.bot_id=? AND a.creator_id=?
                   GROUP BY a.routine_id""",
                (day, routine_id, scope_key, bot_id, creator_id),
            ).fetchone()
        return {**dict(row), "day": day} if row is not None else None

    def status(
        self,
        scope_key: str,
        *,
        now: int | None = None,
    ) -> QuotaStatus:
        start, end, day = _day_bounds(now)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM usage_events
                WHERE scope_key = ? AND occurred_at >= ? AND occurred_at < ?
                """,
                (scope_key, start, end),
            ).fetchone()
            override = self._connection.execute(
                "SELECT * FROM quota_overrides WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        return QuotaStatus(
            scope_key=scope_key,
            day=day,
            calls=int(row["calls"] if row is not None else 0),
            input_tokens=int(row["input_tokens"] if row is not None else 0),
            output_tokens=int(row["output_tokens"] if row is not None else 0),
            call_limit=int(
                override["call_limit"]
                if override is not None
                else self.daily_call_limit
            ),
            input_limit=int(
                override["input_limit"]
                if override is not None
                else self.daily_input_token_limit
            ),
            output_limit=int(
                override["output_limit"]
                if override is not None
                else self.daily_output_token_limit
            ),
        )

    def set_override(
        self,
        scope_key: str,
        *,
        call_limit: int,
        input_limit: int,
        output_limit: int,
    ) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO quota_overrides (
                    scope_key, call_limit, input_limit, output_limit, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    call_limit = excluded.call_limit,
                    input_limit = excluded.input_limit,
                    output_limit = excluded.output_limit,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    max(int(call_limit), 0),
                    max(int(input_limit), 0),
                    max(int(output_limit), 0),
                    int(time.time()),
                ),
            )

    def daily_summary(self, *, days: int = 14) -> list[dict[str, object]]:
        bounded = min(max(int(days), 1), 365)
        cutoff = int(time.time()) - bounded * 86400
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT scope_key, source, occurred_at,
                       input_tokens, output_tokens, cached_tokens
                FROM usage_events
                WHERE occurred_at >= ?
                ORDER BY occurred_at DESC
                """,
                (cutoff,),
            ).fetchall()
        buckets: dict[tuple[str, str, str], dict[str, object]] = {}
        for row in rows:
            day = datetime.fromtimestamp(int(row["occurred_at"]), SHANGHAI).date().isoformat()
            key = (day, str(row["scope_key"]), str(row["source"]))
            bucket = buckets.setdefault(
                key,
                {
                    "day": day,
                    "scope_key": key[1],
                    "source": key[2],
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                },
            )
            bucket["calls"] = int(bucket["calls"]) + 1
            for field in ("input_tokens", "output_tokens", "cached_tokens"):
                bucket[field] = int(bucket[field]) + int(row[field])
        return sorted(
            buckets.values(),
            key=lambda item: (str(item["day"]), str(item["scope_key"])),
            reverse=True,
        )

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


def _day_bounds(now: int | None) -> tuple[int, int, str]:
    timestamp = int(time.time() if now is None else now)
    local = datetime.fromtimestamp(timestamp, SHANGHAI)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local.fromtimestamp(start_local.timestamp() + 86400, SHANGHAI)
    return int(start_local.timestamp()), int(end_local.timestamp()), start_local.date().isoformat()
