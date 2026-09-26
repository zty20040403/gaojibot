from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol, Union, overload

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool, PoolTimeout


class DatabaseError(RuntimeError):
    """A storage operation failed without exposing credentials in its message."""


class StoreRow(Mapping[str, Any]):
    """Row compatible with both sqlite3.Row access styles."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._positions = {name: index for index, name in enumerate(self._columns)}

    @overload
    def __getitem__(self, key: str) -> Any: ...

    @overload
    def __getitem__(self, key: int) -> Any: ...

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._positions[key]]

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def keys(self) -> tuple[str, ...]:
        return self._columns


class StoreCursor(Protocol):
    rowcount: int

    @property
    def lastrowid(self) -> int | None: ...

    def execute(
        self,
        query: str,
        parameters: Sequence[Any] = (),
    ) -> StoreCursor: ...

    def executescript(self, script: str) -> StoreCursor: ...

    def fetchone(self) -> StoreRow | sqlite3.Row | None: ...

    def fetchall(self) -> list[StoreRow] | list[sqlite3.Row]: ...

    def close(self) -> None: ...


class StoreConnection(Protocol):
    dialect: str

    def execute(
        self,
        query: str,
        parameters: Sequence[Any] = (),
    ) -> StoreCursor: ...

    def cursor(self) -> StoreCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


DatabaseSource = Union[str, Path, "PostgresDatabase"]


_IDENTITY_COLUMNS = {
    "conversations": "conversation_id",
    "principals": "principal_id",
    "principal_identities": "identity_id",
    "messages": "canonical_message_id",
    "embeddings": "embedding_id",
    "context_compartments": "compartment_id",
    "reminders": "reminder_id",
    "deliveries": "delivery_id",
    "delivery_attempts": "attempt_id",
    "usage_events": "usage_id",
    "bridge_sources": "source_id",
    "agent_turns": "turn_id",
    "turn_journal_events": "event_id",
    "turn_edges": "edge_id",
    "media_blobs": "media_id",
    "message_media": "message_media_id",
    "media_jobs": "job_id",
    "vision_jobs": "vision_job_id",
    "media_cleanup_runs": "cleanup_id",
    "content_sources": "source_id",
    "message_sources": "message_source_id",
    "durable_jobs": "job_id",
    "message_topic_edges": "edge_id",
    "alert_events": "event_id",
    "alert_notifications": "notification_id",
    "subagent_tasks": "task_id",
    "subagent_runs": "run_id",
    "subagent_events": "event_id",
    "subagent_artifacts": "artifact_id",
    "subagent_checkpoints": "checkpoint_id",
    "subagent_run_contexts": "context_id",
    "fleet_observations": "observation_id",
    "diagnostic_runs": "run_id",
    "diagnostic_evidence": "evidence_id",
    "fleet_operation_events": "event_id",
    "fleet_job_events": "event_id",
}


class PostgresDatabase:
    """One resilient connection pool shared by every persistent bot store."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "qq_bot",
        min_size: int = 1,
        max_size: int = 10,
        timeout_seconds: float = 10.0,
        health_check_interval_seconds: float = 5.0,
        application_name: str = "gaoji",
        node_names: Sequence[str] = (),
        topology_cache_seconds: float = 10.0,
    ) -> None:
        self.dsn = dsn.strip()
        if not self.dsn:
            raise DatabaseError("AI_POSTGRES_DSN is required")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise DatabaseError("AI_POSTGRES_SCHEMA is not a valid identifier")
        # Pool acquisition timeouts do not bound I/O on an established connection.
        connection_defaults = {
            "connect_timeout": "3",
            "keepalives": "1",
            "keepalives_idle": "10",
            "keepalives_interval": "5",
            "keepalives_count": "3",
            "tcp_user_timeout": "15000",
        }
        try:
            configured = conninfo_to_dict(self.dsn)
            self.dsn = make_conninfo(
                self.dsn,
                **{key: value for key, value in connection_defaults.items() if key not in configured},
            )
        except (psycopg.Error, ValueError):
            raise DatabaseError("AI_POSTGRES_DSN is not a valid connection string") from None
        self.schema = schema
        self._closed = False
        self._application_name = application_name
        self._node_names = tuple(str(item).strip() for item in node_names if item)
        self._pool_min_size = max(int(min_size), 1)
        self._pool_max_size = max(int(max_size), self._pool_min_size)
        self._topology_cache_seconds = max(float(topology_cache_seconds), 1.0)
        self._topology_lock = threading.Lock()
        self._topology_cached_at = 0.0
        self._topology_cache: dict[str, object] | None = None
        self._health_check_interval_seconds = max(
            float(health_check_interval_seconds),
            0.5,
        )
        self._health_check_lock = threading.Lock()
        self._health_checked_at: dict[int, float] = {}
        self._pool = ConnectionPool(
            conninfo=self.dsn,
            min_size=self._pool_min_size,
            max_size=self._pool_max_size,
            timeout=max(float(timeout_seconds), 1.0),
            kwargs={"application_name": application_name},
            configure=self._configure_connection,
            check=self._check_read_write_connection,
            open=True,
        )
        try:
            self._pool.wait(timeout=max(float(timeout_seconds), 1.0))
        except (psycopg.Error, PoolTimeout, TimeoutError) as exc:
            self._pool.close()
            raise DatabaseError("PostgreSQL is unavailable") from exc

    def _check_read_write_connection(
        self,
        connection: psycopg.Connection[Any],
    ) -> None:
        now = time.monotonic()
        connection_id = id(connection)
        with self._health_check_lock:
            checked_at = self._health_checked_at.get(connection_id, 0.0)
        if now - checked_at < self._health_check_interval_seconds:
            return

        self._probe_read_write_connection(connection)
        with self._health_check_lock:
            self._health_checked_at[connection_id] = now

    @staticmethod
    def _probe_read_write_connection(
        connection: psycopg.Connection[Any],
    ) -> None:
        original_autocommit = connection.autocommit
        if not original_autocommit:
            connection.autocommit = True
        try:
            row = connection.execute("SHOW transaction_read_only").fetchone()
            if row is None or str(row[0]).strip().lower() != "off":
                raise psycopg.OperationalError(
                    "pooled PostgreSQL connection is no longer read-write"
                )
        finally:
            if not original_autocommit:
                connection.autocommit = False

    def _configure_connection(self, connection: psycopg.Connection[Any]) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}, public").format(
                        sql.Identifier(self.schema)
                    )
                )
                cursor.execute("SET TIME ZONE 'UTC'")
            connection.commit()
            self._probe_read_write_connection(connection)
            with self._health_check_lock:
                self._health_checked_at[id(connection)] = time.monotonic()
        except Exception:
            connection.rollback()
            raise

    def store_connection(self) -> StoreConnection:
        if self._closed:
            raise DatabaseError("PostgreSQL pool is closed")
        return _PostgresStoreConnection(self._pool)

    def healthcheck(self) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
        except (psycopg.Error, PoolTimeout) as exc:
            raise DatabaseError("PostgreSQL health check failed") from exc

    def topology_snapshot(self) -> dict[str, object]:
        """Return a cached, credential-free view of every configured node."""
        now = time.monotonic()
        with self._topology_lock:
            if (
                self._topology_cache is not None
                and now - self._topology_cached_at < self._topology_cache_seconds
            ):
                return self._copy_topology_snapshot(self._topology_cache)

            nodes = self._configured_nodes()
            if nodes:
                with ThreadPoolExecutor(max_workers=min(len(nodes), 4)) as executor:
                    snapshots = list(executor.map(self._probe_node, nodes))
            else:
                snapshots = []

            online_count = sum(
                1 for item in snapshots if item.get("status") == "online"
            )
            writable = next(
                (str(item["name"]) for item in snapshots if item.get("writable")),
                None,
            )
            if snapshots and online_count == len(snapshots) and writable:
                overall = "healthy"
            elif online_count:
                overall = "degraded"
            else:
                overall = "offline"

            pool_stats = self._pool.get_stats()
            snapshot: dict[str, object] = {
                "available": True,
                "overall": overall,
                "checked_at": int(time.time()),
                "writable_node": writable,
                "nodes": snapshots,
                "pool": {
                    "size": int(pool_stats.get("pool_size", 0)),
                    "available": int(pool_stats.get("pool_available", 0)),
                    "waiting": int(pool_stats.get("requests_waiting", 0)),
                    "min_size": self._pool_min_size,
                    "max_size": self._pool_max_size,
                },
            }
            self._topology_cache = snapshot
            self._topology_cached_at = time.monotonic()
            return self._copy_topology_snapshot(snapshot)

    def _configured_nodes(self) -> list[dict[str, str]]:
        try:
            values = conninfo_to_dict(self.dsn)
        except (psycopg.Error, ValueError):
            return []
        hosts = [item.strip() for item in values.get("host", "").split(",")]
        hosts = [item for item in hosts if item]
        raw_ports = [item.strip() for item in values.get("port", "").split(",")]
        ports = [item for item in raw_ports if item]
        if len(ports) == 1 and len(hosts) > 1:
            ports *= len(hosts)

        nodes: list[dict[str, str]] = []
        for index, host in enumerate(hosts):
            port = ports[index] if index < len(ports) else "5432"
            name = (
                self._node_names[index]
                if index < len(self._node_names)
                else f"数据库 {index + 1}"
            )
            nodes.append({"name": name, "host": host, "port": port})
        return nodes

    def _probe_node(self, node: dict[str, str]) -> dict[str, object]:
        started_at = time.monotonic()
        result: dict[str, object] = {
            "name": node["name"],
            "host": node["host"],
            "port": int(node["port"]),
            "status": "offline",
            "role": "unknown",
            "writable": False,
            "latency_ms": None,
            "database_size_bytes": None,
            "replication_lag_seconds": None,
            "replication_lag_bytes": None,
            "server_version": None,
            "error": "连接失败",
        }
        try:
            node_dsn = make_conninfo(
                self.dsn,
                host=node["host"],
                port=node["port"],
                target_session_attrs="any",
                connect_timeout="2",
            )
            with psycopg.connect(
                node_dsn,
                autocommit=True,
                application_name=f"{self._application_name}-admin-probe",
            ) as connection:
                row = connection.execute(
                    """
                    SELECT
                        pg_is_in_recovery(),
                        current_setting('transaction_read_only'),
                        pg_database_size(current_database()),
                        current_setting('server_version'),
                        CASE
                            WHEN pg_is_in_recovery()
                                 AND pg_last_wal_receive_lsn() IS DISTINCT FROM
                                     pg_last_wal_replay_lsn()
                            THEN EXTRACT(EPOCH FROM (
                                clock_timestamp() - pg_last_xact_replay_timestamp()
                            ))
                            ELSE 0
                        END,
                        CASE
                            WHEN pg_is_in_recovery()
                            THEN COALESCE(pg_wal_lsn_diff(
                                pg_last_wal_receive_lsn(),
                                pg_last_wal_replay_lsn()
                            ), 0)
                            ELSE 0
                        END
                    """
                ).fetchone()
            if row is None:
                return result
            in_recovery = bool(row[0])
            writable = not in_recovery and str(row[1]).lower() == "off"
            result.update(
                {
                    "status": "online",
                    "role": "secondary" if in_recovery else "primary",
                    "writable": writable,
                    "latency_ms": round(
                        (time.monotonic() - started_at) * 1000,
                        1,
                    ),
                    "database_size_bytes": int(row[2]),
                    "server_version": str(row[3]),
                    "replication_lag_seconds": (
                        round(float(row[4]), 3) if row[4] is not None else None
                    ),
                    "replication_lag_bytes": (
                        int(row[5]) if row[5] is not None else None
                    ),
                    "error": None,
                }
            )
        except (psycopg.Error, OSError, ValueError):
            pass
        return result

    @staticmethod
    def _copy_topology_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
        raw_nodes = snapshot.get("nodes", [])
        raw_pool = snapshot.get("pool", {})
        return {
            **snapshot,
            "nodes": (
                [dict(item) for item in raw_nodes]
                if isinstance(raw_nodes, list)
                else []
            ),
            "pool": dict(raw_pool) if isinstance(raw_pool, dict) else {},
        }

    def require_revision(self, expected_revision: str) -> None:
        statement = sql.SQL(
            "SELECT version_num FROM {}.alembic_version"
        ).format(sql.Identifier(self.schema))
        try:
            with self._pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement)
                    row = cursor.fetchone()
        except (psycopg.Error, PoolTimeout) as exc:
            raise DatabaseError(
                "PostgreSQL schema is missing; run the Alembic upgrade first"
            ) from exc
        current = str(row[0]) if row is not None else ""
        if current != expected_revision:
            raise DatabaseError(
                "PostgreSQL schema revision does not match this bot build: "
                f"expected {expected_revision}, got {current or 'none'}"
            )

    def close(self) -> None:
        if self._closed:
            return
        self._pool.close()
        self._closed = True


class _PostgresStoreConnection:
    dialect = "postgresql"

    def __init__(self, pool: ConnectionPool[Any]) -> None:
        self._pool = pool
        self._lock = threading.RLock()
        self._active: _LivePostgresCursor | None = None
        self._active_context: Any = None
        self._closed = False

    def execute(
        self,
        query: str,
        parameters: Sequence[Any] = (),
    ) -> StoreCursor:
        self._ensure_open()
        try:
            with self._pool.connection() as connection:
                with connection.cursor() as cursor:
                    live = _LivePostgresCursor(cursor)
                    live.execute(query, parameters)
                    rows = live._remaining_rows()
                    return _BufferedCursor(
                        rows,
                        rowcount=live.rowcount,
                        lastrowid=live.lastrowid,
                    )
        except (psycopg.Error, PoolTimeout) as exc:
            raise DatabaseError("PostgreSQL query failed") from exc

    def cursor(self) -> StoreCursor:
        self._ensure_open()
        with self._lock:
            if self._active is not None:
                raise DatabaseError("nested store transactions are not supported")
            context = self._pool.connection()
            try:
                connection = context.__enter__()
                cursor = _LivePostgresCursor(connection.cursor())
            except (psycopg.Error, PoolTimeout) as exc:
                context.__exit__(type(exc), exc, exc.__traceback__)
                raise DatabaseError("PostgreSQL transaction could not start") from exc
            self._active = cursor
            self._active_context = context
            return cursor

    def commit(self) -> None:
        self._release(commit=True)

    def rollback(self) -> None:
        self._release(commit=False)

    def close(self) -> None:
        with self._lock:
            if self._active is not None:
                self._release(commit=False)
            self._closed = True

    def _release(self, *, commit: bool) -> None:
        with self._lock:
            cursor = self._active
            context = self._active_context
            if cursor is None or context is None:
                return
            try:
                connection = cursor.connection
                if commit:
                    connection.commit()
                else:
                    connection.rollback()
            except psycopg.Error as exc:
                raise DatabaseError("PostgreSQL transaction failed") from exc
            finally:
                cursor.close()
                self._active = None
                self._active_context = None
                context.__exit__(None, None, None)

    def _ensure_open(self) -> None:
        if self._closed:
            raise DatabaseError("store connection is closed")


class _LivePostgresCursor:
    def __init__(self, cursor: psycopg.Cursor[Any]) -> None:
        self._cursor = cursor
        self._lastrowid: int | None = None
        self.rowcount = -1

    @property
    def connection(self) -> psycopg.Connection[Any]:
        return self._cursor.connection

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    def execute(
        self,
        query: str,
        parameters: Sequence[Any] = (),
    ) -> StoreCursor:
        translated, identity_column = _translate_sql(query)
        try:
            self._cursor.execute(translated, tuple(parameters))
            self.rowcount = self._cursor.rowcount
            self._lastrowid = None
            if identity_column is not None and self._cursor.description is not None:
                row = self._cursor.fetchone()
                if row is not None:
                    self._lastrowid = int(row[0])
            return self
        except psycopg.Error as exc:
            raise DatabaseError("PostgreSQL query failed") from exc

    def executescript(self, script: str) -> StoreCursor:
        raise DatabaseError("schema changes must be applied through Alembic")

    def fetchone(self) -> StoreRow | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return _make_row(self._cursor, row)

    def fetchall(self) -> list[StoreRow]:
        return [_make_row(self._cursor, row) for row in self._cursor.fetchall()]

    def _remaining_rows(self) -> list[StoreRow]:
        if self._cursor.description is None:
            return []
        return self.fetchall()

    def close(self) -> None:
        if not self._cursor.closed:
            self._cursor.close()


class _BufferedCursor:
    def __init__(
        self,
        rows: Sequence[StoreRow],
        *,
        rowcount: int,
        lastrowid: int | None,
    ) -> None:
        self._rows = list(rows)
        self._position = 0
        self.rowcount = rowcount
        self._lastrowid = lastrowid

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    def execute(
        self,
        query: str,
        parameters: Sequence[Any] = (),
    ) -> StoreCursor:
        raise DatabaseError("buffered cursors cannot execute another query")

    def executescript(self, script: str) -> StoreCursor:
        raise DatabaseError("buffered cursors cannot execute scripts")

    def fetchone(self) -> StoreRow | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> list[StoreRow]:
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows

    def close(self) -> None:
        self._rows.clear()


def open_store_connection(
    source: DatabaseSource,
) -> tuple[Path | None, StoreConnection | sqlite3.Connection]:
    if isinstance(source, PostgresDatabase):
        return None, source.store_connection()

    path = Path(source) if str(source) != ":memory:" else None
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(source),
        timeout=10.0,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return path, connection


def _make_row(cursor: psycopg.Cursor[Any], values: Sequence[Any]) -> StoreRow:
    description = cursor.description or ()
    return StoreRow([column.name for column in description], values)


def _translate_sql(query: str) -> tuple[str, str | None]:
    stripped = query.strip()
    if stripped.upper() == "BEGIN IMMEDIATE":
        return "BEGIN", None

    ignored_insert = bool(re.match(r"(?is)^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", query))
    translated = re.sub(
        r"(?is)^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+",
        "INSERT INTO ",
        query,
        count=1,
    )
    translated = _convert_qmark_placeholders(translated)
    translated = re.sub(
        r"(?is)\bLIMIT\s+-1\s+OFFSET\s+%s",
        "OFFSET %s",
        translated,
    )
    translated = translated.strip().rstrip(";")
    if ignored_insert and "ON CONFLICT" not in translated.upper():
        translated += " ON CONFLICT DO NOTHING"

    identity_column: str | None = None
    insert = re.match(
        r'(?is)^INSERT\s+INTO\s+(?:"?[A-Za-z_][A-Za-z0-9_]*"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?',
        translated,
    )
    if insert is not None and " RETURNING " not in translated.upper():
        identity_column = _IDENTITY_COLUMNS.get(insert.group(1).lower())
        if identity_column is not None:
            translated += f' RETURNING "{identity_column}"'
    return translated, identity_column


def _convert_qmark_placeholders(query: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(query):
        character = query[index]
        if quote is not None:
            output.append(character)
            if character == quote:
                if index + 1 < len(query) and query[index + 1] == quote:
                    output.append(query[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?":
            output.append("%s")
        else:
            output.append(character)
        index += 1
    return "".join(output)
