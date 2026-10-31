from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import psycopg
from psycopg import sql

from .schema import HEAD_REVISION


@dataclass(frozen=True)
class TableSpec:
    source_file: str
    table: str
    primary_key: tuple[str, ...]
    identity_column: str | None = None


TABLES: tuple[TableSpec, ...] = (
    TableSpec("bot_state.sqlite3", "conversations", ("conversation_id",), "conversation_id"),
    TableSpec("bot_state.sqlite3", "principals", ("principal_id",), "principal_id"),
    TableSpec("bot_state.sqlite3", "principal_identities", ("identity_id",), "identity_id"),
    TableSpec("bot_state.sqlite3", "messages", ("canonical_message_id",), "canonical_message_id"),
    TableSpec("bot_state.sqlite3", "conversation_visibility", ("conversation_id",)),
    TableSpec("bot_state.sqlite3", "embeddings", ("embedding_id",), "embedding_id"),
    TableSpec("delivery_outbox.sqlite3", "deliveries", ("delivery_id",), "delivery_id"),
    TableSpec("delivery_outbox.sqlite3", "delivery_attempts", ("attempt_id",), "attempt_id"),
    TableSpec("usage.sqlite3", "usage_events", ("usage_id",), "usage_id"),
    TableSpec("usage.sqlite3", "quota_overrides", ("scope_key",)),
    TableSpec("semantic_index_state.sqlite3", "semantic_index_state", ("source_key",)),
    TableSpec("maintenance_state.sqlite3", "maintenance_state", ("job_name",)),
    TableSpec("turn_journal.sqlite3", "agent_turns", ("turn_id",), "turn_id"),
    TableSpec("turn_journal.sqlite3", "turn_journal_events", ("event_id",), "event_id"),
    TableSpec("turn_journal.sqlite3", "turn_edges", ("edge_id",), "edge_id"),
    TableSpec("turn_journal.sqlite3", "turn_send_links", ("canonical_message_id",)),
    TableSpec("turn_journal.sqlite3", "turn_archives", ("turn_id",)),
    TableSpec("turn_journal.sqlite3", "turn_visibility", ("scope_key",)),
    TableSpec("turn_journal.sqlite3", "turn_digests", ("turn_id",)),
)

JSON_STATES = {
    "conversation_history.json": "conversation_history",
    "group_context.json": "group_context",
    "long_term_memory.json": "long_term_memory",
    "user_profiles.json": "user_profiles",
    "model_preferences.json": "model_preferences",
    "learned_stickers.json": "learned_stickers",
}

_LEGACY_DEFAULTS: dict[tuple[str, str], Any] = {
    ("messages", "message_kind"): "chat",
    ("messages", "raw_event_json"): "{}",
    ("deliveries", "reply_to_native_message_id"): "",
}


@dataclass(frozen=True)
class TableSnapshot:
    spec: TableSpec
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    digest: str


@dataclass(frozen=True)
class LegacySnapshot:
    state_dir: Path
    tables: tuple[TableSnapshot, ...]
    json_states: dict[str, object]
    fingerprint: str

    @property
    def row_count(self) -> int:
        return sum(len(table.rows) for table in self.tables)


def capture_legacy_snapshot(state_dir: Path) -> LegacySnapshot:
    root = state_dir.expanduser().resolve()
    table_snapshots: list[TableSnapshot] = []
    for spec in TABLES:
        source = root / spec.source_file
        if not source.exists():
            table_snapshots.append(TableSnapshot(spec, (), (), _digest_rows((), ())))
            continue
        uri = f"file:{source.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (spec.table,),
            ).fetchone()
            if exists is None:
                table_snapshots.append(
                    TableSnapshot(spec, (), (), _digest_rows((), ()))
                )
                continue
            columns = tuple(
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{spec.table}")'
                ).fetchall()
            )
            ordering = ", ".join(f'"{name}"' for name in spec.primary_key)
            rows = tuple(
                tuple(
                    _normalize_legacy_value(spec.table, name, row[name])
                    for name in columns
                )
                for row in connection.execute(
                    f'SELECT * FROM "{spec.table}" ORDER BY {ordering}'
                ).fetchall()
            )
        finally:
            connection.close()
        table_snapshots.append(
            TableSnapshot(spec, columns, rows, _digest_rows(columns, rows))
        )

    json_states: dict[str, object] = {}
    for filename, namespace in JSON_STATES.items():
        path = root / filename
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"legacy JSON is invalid: {filename}") from exc
        json_states[namespace] = payload

    fingerprint_payload = {
        "tables": [
            {
                "file": table.spec.source_file,
                "table": table.spec.table,
                "columns": table.columns,
                "count": len(table.rows),
                "digest": table.digest,
            }
            for table in table_snapshots
        ],
        "json": {
            namespace: _digest_json(payload)
            for namespace, payload in sorted(json_states.items())
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return LegacySnapshot(root, tuple(table_snapshots), json_states, fingerprint)


def migrate_legacy_snapshot(
    snapshot: LegacySnapshot,
    dsn: str,
    *,
    schema: str = "qq_bot",
    resume: bool = False,
) -> dict[str, object]:
    _validate_schema(schema)
    migration_id = str(uuid.uuid4())
    started_at = int(time.time())
    report: dict[str, object] = {
        "migration_id": migration_id,
        "source": str(snapshot.state_dir),
        "source_fingerprint": snapshot.fingerprint,
        "started_at": started_at,
        "tables": {},
        "json_namespaces": sorted(snapshot.json_states),
    }
    connection = psycopg.connect(dsn, connect_timeout=10)
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET LOCAL search_path TO {}, public").format(
                        sql.Identifier(schema)
                    )
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"{schema}:legacy-import",),
                )
                _require_head(cursor, schema)
                _require_empty_target(cursor, schema, snapshot, resume=resume)
                cursor.execute(
                    """
                    INSERT INTO storage_migration_runs (
                        migration_id, source_fingerprint, status,
                        report_json, started_at, finished_at
                    ) VALUES (%s, %s, 'running', '{}', %s, NULL)
                    """,
                    (migration_id, snapshot.fingerprint, started_at),
                )

                table_report: dict[str, object] = {}
                for table in snapshot.tables:
                    imported = _import_table(cursor, schema, table)
                    verification = _verify_table(cursor, schema, table)
                    table_report[table.spec.table] = {
                        "source_file": table.spec.source_file,
                        "rows": imported,
                        "source_digest": table.digest,
                        "target_digest": verification["digest"],
                        "verified": verification["verified"],
                    }
                    if not verification["verified"]:
                        raise RuntimeError(
                            f"consistency check failed for {table.spec.table}"
                        )
                    if table.spec.identity_column is not None:
                        _reset_identity(cursor, schema, table.spec)

                for namespace, payload in snapshot.json_states.items():
                    encoded = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    cursor.execute(
                        """
                        INSERT INTO state_blobs(namespace, payload_json, updated_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT(namespace) DO UPDATE SET
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (namespace, encoded, started_at),
                    )
                    stored = cursor.execute(
                        "SELECT payload_json FROM state_blobs WHERE namespace = %s",
                        (namespace,),
                    ).fetchone()
                    if stored is None or _digest_json(json.loads(stored[0])) != _digest_json(payload):
                        raise RuntimeError(
                            f"consistency check failed for JSON state {namespace}"
                        )

                finished_at = int(time.time())
                report["tables"] = table_report
                report["finished_at"] = finished_at
                report["rows"] = snapshot.row_count
                report["verified"] = True
                cursor.execute(
                    """
                    UPDATE storage_migration_runs
                    SET status = 'verified', report_json = %s, finished_at = %s
                    WHERE migration_id = %s
                    """,
                    (
                        json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                        finished_at,
                        migration_id,
                    ),
                )
    finally:
        connection.close()
    return report


def verify_legacy_snapshot(
    snapshot: LegacySnapshot,
    dsn: str,
    *,
    schema: str = "qq_bot",
) -> dict[str, object]:
    _validate_schema(schema)
    report: dict[str, object] = {"tables": {}, "verified": True}
    connection = psycopg.connect(dsn, connect_timeout=10)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SET search_path TO {}, public").format(
                    sql.Identifier(schema)
                )
            )
            _require_head(cursor, schema)
            table_report: dict[str, object] = {}
            for table in snapshot.tables:
                result = _verify_table(cursor, schema, table)
                table_report[table.spec.table] = result
                report["verified"] = bool(report["verified"] and result["verified"])
            report["tables"] = table_report
            json_report: dict[str, object] = {}
            for namespace, payload in snapshot.json_states.items():
                row = cursor.execute(
                    "SELECT payload_json FROM state_blobs WHERE namespace = %s",
                    (namespace,),
                ).fetchone()
                target_digest = (
                    _digest_json(json.loads(row[0])) if row is not None else ""
                )
                source_digest = _digest_json(payload)
                verified = source_digest == target_digest
                json_report[namespace] = {
                    "source_digest": source_digest,
                    "target_digest": target_digest,
                    "verified": verified,
                }
                report["verified"] = bool(report["verified"] and verified)
            report["json"] = json_report
    finally:
        connection.close()
    return report


def snapshot_report(snapshot: LegacySnapshot) -> dict[str, object]:
    return {
        "state_dir": str(snapshot.state_dir),
        "fingerprint": snapshot.fingerprint,
        "rows": snapshot.row_count,
        "tables": {
            table.spec.table: {
                "source_file": table.spec.source_file,
                "rows": len(table.rows),
                "digest": table.digest,
            }
            for table in snapshot.tables
        },
        "json_namespaces": sorted(snapshot.json_states),
    }


def _import_table(
    cursor: psycopg.Cursor[Any],
    schema: str,
    snapshot: TableSnapshot,
) -> int:
    if not snapshot.rows:
        return 0
    target_columns = _target_columns(cursor, schema, snapshot.spec.table)
    source_positions = {name: index for index, name in enumerate(snapshot.columns)}
    columns = tuple(
        name
        for name in target_columns
        if name in source_positions or (snapshot.spec.table, name) in _LEGACY_DEFAULTS
    )
    missing_keys = [name for name in snapshot.spec.primary_key if name not in columns]
    if missing_keys:
        raise RuntimeError(
            f"legacy table {snapshot.spec.table} is missing key columns: {missing_keys}"
        )

    values = [
        tuple(
            row[source_positions[name]]
            if name in source_positions
            else _LEGACY_DEFAULTS[(snapshot.spec.table, name)]
            for name in columns
        )
        for row in snapshot.rows
    ]
    updates = [name for name in columns if name not in snapshot.spec.primary_key]
    statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT ({}) ").format(
        sql.Identifier(schema),
        sql.Identifier(snapshot.spec.table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        sql.SQL(", ").join(map(sql.Identifier, snapshot.spec.primary_key)),
    )
    if updates:
        statement += sql.SQL("DO UPDATE SET {} ").format(
            sql.SQL(", ").join(
                sql.SQL("{} = excluded.{}").format(
                    sql.Identifier(name),
                    sql.Identifier(name),
                )
                for name in updates
            )
        )
    else:
        statement += sql.SQL("DO NOTHING")
    cursor.executemany(statement, values)
    return len(values)


def _verify_table(
    cursor: psycopg.Cursor[Any],
    schema: str,
    snapshot: TableSnapshot,
) -> dict[str, object]:
    if not snapshot.columns:
        target_count = cursor.execute(
            sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                sql.Identifier(schema),
                sql.Identifier(snapshot.spec.table),
            )
        ).fetchone()[0]
        verified = int(target_count) == 0
        return {"rows": int(target_count), "digest": snapshot.digest, "verified": verified}

    target_columns = set(_target_columns(cursor, schema, snapshot.spec.table))
    columns = tuple(name for name in snapshot.columns if name in target_columns)
    ordering = sql.SQL(", ").join(map(sql.Identifier, snapshot.spec.primary_key))
    selected = sql.SQL(", ").join(map(sql.Identifier, columns))
    cursor.execute(
        sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
            selected,
            sql.Identifier(schema),
            sql.Identifier(snapshot.spec.table),
            ordering,
        )
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    target_digest = _digest_rows(columns, rows)
    verified = len(rows) == len(snapshot.rows) and target_digest == snapshot.digest
    return {"rows": len(rows), "digest": target_digest, "verified": verified}


def _target_columns(
    cursor: psycopg.Cursor[Any],
    schema: str,
    table: str,
) -> tuple[str, ...]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    columns = tuple(str(row[0]) for row in cursor.fetchall())
    if not columns:
        raise RuntimeError(f"target table is missing: {schema}.{table}")
    return columns


def _require_empty_target(
    cursor: psycopg.Cursor[Any],
    schema: str,
    snapshot: LegacySnapshot,
    *,
    resume: bool,
) -> None:
    if resume:
        return
    nonempty: list[str] = []
    for table in snapshot.tables:
        count = cursor.execute(
            sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                sql.Identifier(schema),
                sql.Identifier(table.spec.table),
            )
        ).fetchone()[0]
        if int(count) > 0:
            nonempty.append(table.spec.table)
    state_count = cursor.execute("SELECT COUNT(*) FROM state_blobs").fetchone()[0]
    if int(state_count) > 0:
        nonempty.append("state_blobs")
    if nonempty:
        raise RuntimeError(
            "target contains data; refusing to overwrite without --resume: "
            + ", ".join(nonempty)
        )


def _reset_identity(
    cursor: psycopg.Cursor[Any],
    schema: str,
    spec: TableSpec,
) -> None:
    column = spec.identity_column
    if column is None:
        return
    cursor.execute(
        sql.SQL(
            "SELECT setval(pg_get_serial_sequence(%s, %s), "
            "GREATEST(COALESCE(MAX({}), 0), 1), COALESCE(MAX({}), 0) > 0) "
            "FROM {}.{}"
        ).format(
            sql.Identifier(column),
            sql.Identifier(column),
            sql.Identifier(schema),
            sql.Identifier(spec.table),
        ),
        (f'"{schema}"."{spec.table}"', column),
    )


def _require_head(cursor: psycopg.Cursor[Any], schema: str) -> None:
    row = cursor.execute(
        sql.SQL("SELECT version_num FROM {}.alembic_version").format(
            sql.Identifier(schema)
        )
    ).fetchone()
    current = str(row[0]) if row is not None else ""
    if current != HEAD_REVISION:
        raise RuntimeError(
            f"target schema must be at {HEAD_REVISION}; current is {current or 'none'}"
        )


def _digest_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(list(columns), separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")
    for row in rows:
        payload = [_canonical_value(value) for value in row]
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float):
        return {"float": format(value, ".17g")}
    return value


def _normalize_legacy_value(table: str, column: str, value: Any) -> Any:
    if table == "semantic_index_state" and column == "source_key":
        parts = str(value).split("\0")
        if len(parts) == 3:
            return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return value


def _digest_json(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_schema(schema: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid PostgreSQL schema")
