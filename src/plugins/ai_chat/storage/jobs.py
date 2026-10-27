from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Iterator, Literal, Mapping

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor, StoreRow


DurableJobStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class DurableJob:
    job_id: int
    kind: str
    idempotency_key: str
    scope_key: str
    payload: dict[str, Any]
    status: DurableJobStatus
    priority: int
    attempts: int
    max_attempts: int
    next_attempt_at: int
    lease_owner: str
    lease_until: int | None
    result: dict[str, Any]
    last_error: str
    created_at: int
    updated_at: int
    finished_at: int | None

    @property
    def handle(self) -> str:
        return f"job#{self.job_id}"


@dataclass(frozen=True)
class JobSummary:
    """Operator-safe metadata that deliberately omits the job payload."""

    job_id: int
    kind: str
    scope_key: str
    status: DurableJobStatus
    priority: int
    attempts: int
    max_attempts: int
    next_attempt_at: int
    lease_owner: str
    lease_until: int | None
    last_error: str
    created_at: int
    updated_at: int
    finished_at: int | None

    @property
    def handle(self) -> str:
        return f"job#{self.job_id}"


class DurableJobStore:
    """Lease-based application job queue shared by all bot instances."""

    def __init__(
        self,
        source: DatabaseSource,
        *,
        lease_seconds: int = 300,
        default_max_attempts: int = 3,
    ) -> None:
        self.path, self._connection = open_store_connection(source)
        self.lease_seconds = max(int(lease_seconds), 10)
        self.default_max_attempts = min(max(int(default_max_attempts), 1), 50)
        self._lock = threading.RLock()
        self.recovered_jobs = self.recover_expired_leases()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def enqueue(
        self,
        *,
        kind: str,
        idempotency_key: str,
        payload: Mapping[str, Any] | None = None,
        scope_key: str = "",
        priority: int = 100,
        max_attempts: int | None = None,
        resume_on_lease_loss: bool = False,
        run_at: int | None = None,
        now: int | None = None,
        transaction_cursor: StoreCursor | None = None,
    ) -> tuple[DurableJob, bool]:
        """Optionally enlist in the caller's transaction on the same database."""
        clean_kind = " ".join(str(kind).split())[:120]
        clean_key = " ".join(str(idempotency_key).split())[:300]
        if not clean_kind:
            raise ValueError("job kind must not be empty")
        if not clean_key:
            raise ValueError("job idempotency key must not be empty")
        encoded_payload = json.dumps(
            dict(payload or {}), ensure_ascii=False, separators=(",", ":")
        )
        timestamp = int(time.time() if now is None else now)
        due_at = timestamp if run_at is None else int(run_at)
        attempts_limit = min(
            max(int(max_attempts or self.default_max_attempts), 1), 50
        )
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self._transaction()) as cursor:
            cursor.execute(
                """
                INSERT INTO durable_jobs (
                    kind, idempotency_key, scope_key, payload_json,
                    status, priority, attempts, max_attempts,
                    next_attempt_at, lease_owner, lease_until,
                    result_json, last_error, created_at, updated_at, finished_at, resume_on_lease_loss
                ) VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?, '', NULL,
                          '{}', '', ?, ?, NULL, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    clean_kind,
                    clean_key,
                    str(scope_key)[:300],
                    encoded_payload,
                    int(priority),
                    attempts_limit,
                    due_at,
                    timestamp,
                    timestamp,
                    int(resume_on_lease_loss),
                ),
            )
            created = cursor.rowcount == 1
            row = cursor.execute(
                "SELECT * FROM durable_jobs WHERE idempotency_key = ?",
                (clean_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("durable job was not stored")
        return self._row(row), created

    def claim_due(
        self,
        owner: str,
        *,
        limit: int = 10,
        now: int | None = None,
        kinds: tuple[str, ...] | None = None,
        per_scope_limit: int | None = None,
    ) -> list[DurableJob]:
        clean_owner = " ".join(str(owner).split())[:160]
        if not clean_owner:
            raise ValueError("lease owner must not be empty")
        timestamp = int(time.time() if now is None else now)
        bounded = min(max(int(limit), 1), 100)
        claimed: list[DurableJob] = []
        if kinds == ():
            return claimed
        kind_clause = "" if kinds is None else " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        scope_clause = "" if per_scope_limit is None else " AND (SELECT COUNT(*) FROM durable_jobs active WHERE active.scope_key=durable_jobs.scope_key AND active.kind=durable_jobs.kind AND active.status='running') < ?"
        with self._transaction() as cursor:
            self._recover_expired(cursor, timestamp)
            lock_clause = "FOR UPDATE SKIP LOCKED"
            rows = cursor.execute(
                f"""
                SELECT * FROM durable_jobs
                WHERE status = 'pending' AND next_attempt_at <= ?
                  AND attempts < max_attempts
                  {kind_clause}
                  {scope_clause}
                ORDER BY priority ASC, next_attempt_at ASC, job_id ASC
                LIMIT ?
                {lock_clause}
                """,
                (
                    timestamp,
                    *(kinds or ()),
                    *((per_scope_limit,) if per_scope_limit is not None else ()),
                    bounded,
                ),
            ).fetchall()
            for row in rows:
                if per_scope_limit is not None:
                    active = cursor.execute("SELECT COUNT(*) AS n FROM durable_jobs WHERE scope_key=? AND kind=? AND status='running'", (row["scope_key"], row["kind"])).fetchone()
                    if int(active["n"]) >= per_scope_limit:
                        continue
                job_id = int(row["job_id"])
                cursor.execute(
                    """
                    UPDATE durable_jobs
                    SET status = 'running', attempts = attempts + 1,
                        lease_owner = ?, lease_until = ?, updated_at = ?,
                        last_error = ''
                    WHERE job_id = ? AND status = 'pending'
                    """,
                    (
                        clean_owner,
                        timestamp + self.lease_seconds,
                        timestamp,
                        job_id,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                stored = cursor.execute(
                    "SELECT * FROM durable_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if stored is not None:
                    claimed.append(self._row(stored))
        return claimed

    def mark_succeeded(
        self,
        job_id: int,
        owner: str,
        *,
        result: Mapping[str, Any] | None = None,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time() if now is None else now)
        encoded_result = json.dumps(
            dict(result or {}), ensure_ascii=False, separators=(",", ":")
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE durable_jobs
                SET status = 'succeeded', result_json = ?, lease_owner = '',
                    lease_until = NULL, last_error = '', updated_at = ?,
                    finished_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (encoded_result, timestamp, timestamp, int(job_id), str(owner)),
            )
            return cursor.rowcount == 1

    def defer(self, job: DurableJob, *, delay_seconds: int = 15, reason: str = "", now: int | None = None) -> bool:
        """Release a continuation lease without charging a failure attempt."""
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            cursor.execute("""UPDATE durable_jobs SET status='pending', attempts=attempts-1,
                next_attempt_at=?, lease_owner='', lease_until=NULL, last_error=?, updated_at=?
                WHERE job_id=? AND status='running' AND lease_owner=? AND attempts=? AND lease_until>?""",
                (timestamp + max(delay_seconds, 0), reason[:2000], timestamp,
                 job.job_id, job.lease_owner, job.attempts, timestamp))
            return cursor.rowcount == 1

    def renew_lease(
        self,
        job_id: int,
        owner: str,
        *,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE durable_jobs
                SET lease_until = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    timestamp + self.lease_seconds,
                    timestamp,
                    int(job_id),
                    str(owner),
                ),
            )
            return cursor.rowcount == 1

    def mark_failed(
        self,
        job_id: int,
        owner: str,
        error: str,
        *,
        retryable: bool = True,
        retry_delay_seconds: int = 10,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            row = cursor.execute(
                """
                SELECT attempts, max_attempts FROM durable_jobs
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (int(job_id), str(owner)),
            ).fetchone()
            if row is None:
                return False
            can_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
            status = "pending" if can_retry else "failed"
            next_attempt_at = timestamp + max(int(retry_delay_seconds), 0)
            finished_at = None if can_retry else timestamp
            cursor.execute(
                """
                UPDATE durable_jobs
                SET status = ?, next_attempt_at = ?, lease_owner = '',
                    lease_until = NULL, last_error = ?, updated_at = ?,
                    finished_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    status,
                    next_attempt_at,
                    str(error)[:2000],
                    timestamp,
                    finished_at,
                    int(job_id),
                    str(owner),
                ),
            )
            return cursor.rowcount == 1

    def cancel(self, job_id: int, *, now: int | None = None) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE durable_jobs
                SET status = 'cancelled', lease_owner = '', lease_until = NULL,
                    updated_at = ?, finished_at = ?
                WHERE job_id = ? AND status IN ('pending', 'running', 'failed')
                """,
                (timestamp, timestamp, int(job_id)),
            )
            return cursor.rowcount == 1

    def get(self, job_id: int) -> DurableJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM durable_jobs WHERE job_id = ?",
                (int(job_id),),
            ).fetchone()
        return self._row(row) if row is not None else None

    def requeue(self, job_id: int, *, now: int | None = None) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE durable_jobs
                SET status = 'pending', attempts = 0, next_attempt_at = ?,
                    lease_owner = '', lease_until = NULL, result_json = '{}',
                    last_error = '', updated_at = ?, finished_at = NULL
                WHERE job_id = ? AND status IN ('failed', 'cancelled')
                """,
                (timestamp, timestamp, int(job_id)),
            )
            return cursor.rowcount == 1

    def recover_expired_leases(self, *, now: int | None = None) -> int:
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            return self._recover_expired(cursor, timestamp)

    def recent_summaries(self, *, limit: int = 100) -> list[JobSummary]:
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT job_id, kind, scope_key, status, priority, attempts,
                       max_attempts, next_attempt_at, lease_owner, lease_until,
                       last_error, created_at, updated_at, finished_at
                FROM durable_jobs
                ORDER BY job_id DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [self._summary_row(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM durable_jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            status: counts.get(status, 0)
            for status in ("pending", "running", "succeeded", "failed", "cancelled")
        }

    @staticmethod
    def _recover_expired(cursor: StoreCursor, timestamp: int) -> int:
        cursor.execute(
            """
            UPDATE durable_jobs
            SET status = CASE
                    WHEN resume_on_lease_loss=1 OR attempts < max_attempts THEN 'pending'
                    ELSE 'failed'
                END,
                attempts = CASE WHEN resume_on_lease_loss=1 AND attempts>0 THEN attempts-1 ELSE attempts END,
                next_attempt_at = ?, lease_owner = '', lease_until = NULL,
                last_error = 'worker lease expired; task recovered',
                updated_at = ?,
                finished_at = CASE
                    WHEN resume_on_lease_loss=1 OR attempts < max_attempts THEN NULL
                    ELSE ?
                END
            WHERE status = 'running' AND lease_until IS NOT NULL
              AND lease_until <= ?
            """,
            (timestamp, timestamp, timestamp, timestamp),
        )
        return max(int(cursor.rowcount), 0)

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
    def _row(row: StoreRow) -> DurableJob:
        return DurableJob(
            job_id=int(row["job_id"]),
            kind=str(row["kind"]),
            idempotency_key=str(row["idempotency_key"]),
            scope_key=str(row["scope_key"]),
            payload=_json_object(row["payload_json"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            priority=int(row["priority"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=int(row["next_attempt_at"]),
            lease_owner=str(row["lease_owner"]),
            lease_until=(
                int(row["lease_until"]) if row["lease_until"] is not None else None
            ),
            result=_json_object(row["result_json"]),
            last_error=str(row["last_error"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            finished_at=(
                int(row["finished_at"]) if row["finished_at"] is not None else None
            ),
        )

    @staticmethod
    def _summary_row(row: StoreRow) -> JobSummary:
        return JobSummary(
            job_id=int(row["job_id"]),
            kind=str(row["kind"]),
            scope_key=str(row["scope_key"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            priority=int(row["priority"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=int(row["next_attempt_at"]),
            lease_owner=str(row["lease_owner"]),
            lease_until=(
                int(row["lease_until"]) if row["lease_until"] is not None else None
            ),
            last_error=str(row["last_error"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            finished_at=(
                int(row["finished_at"]) if row["finished_at"] is not None else None
            ),
        )


def _json_object(value: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
