from __future__ import annotations

import hashlib
import json
from typing import Any

from src.bot_storage import PostgresDatabase


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(_json(params).encode("utf-8")).hexdigest()


class FleetProjectionStore:
    """Small durable projection; Ops and Prometheus remain source systems."""

    def __init__(self, database: PostgresDatabase, *, backend_name: str = "ops") -> None:
        self.database = database
        self.backend_name = backend_name

    def close(self) -> None:
        self.database.close()

    def record_backend(
        self,
        *,
        state: str,
        catalog_version: int,
        operations: list[str],
        checked_at: int,
        last_success_at: int | None,
        error_code: str = "",
    ) -> None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO fleet_backend_states (
                    backend_name, state, catalog_version, operations_json,
                    error_code, last_success_at, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(backend_name) DO UPDATE SET
                    state = EXCLUDED.state,
                    catalog_version = EXCLUDED.catalog_version,
                    operations_json = EXCLUDED.operations_json,
                    error_code = EXCLUDED.error_code,
                    last_success_at = EXCLUDED.last_success_at,
                    checked_at = EXCLUDED.checked_at
                """,
                (
                    self.backend_name,
                    state,
                    catalog_version,
                    _json(operations),
                    error_code,
                    last_success_at,
                    checked_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def record_observation(
        self,
        *,
        operation: str,
        params: dict[str, Any],
        target_key: str,
        status: str,
        payload: Any,
        observed_at: int | None,
        received_at: int,
        expires_at: int | None,
        duration_ms: int | None,
        error_code: str = "",
        sensitive: bool = False,
    ) -> int:
        params_hash = _params_hash(params)
        payload_json = None if sensitive else _json(payload)
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                """
                INSERT INTO fleet_observations (
                    source_backend, operation, target_key, params_hash,
                    status, payload_json, sensitive, observed_at, received_at,
                    expires_at, duration_ms, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING observation_id
                """,
                (
                    self.backend_name,
                    operation,
                    target_key,
                    params_hash,
                    status,
                    payload_json,
                    sensitive,
                    observed_at,
                    received_at,
                    expires_at,
                    duration_ms,
                    error_code,
                ),
            ).fetchone()
            connection.commit()
            return int(row[0]) if row is not None else 0
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def latest(
        self,
        *,
        operation: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                """
                SELECT operation, status, payload_json, sensitive, observed_at,
                       received_at, expires_at, duration_ms, error_code
                FROM fleet_observations
                WHERE source_backend = ?
                  AND operation = ?
                  AND params_hash = ?
                  AND sensitive = FALSE
                  AND payload_json IS NOT NULL
                  AND status IN ('fresh', 'stale')
                ORDER BY observation_id DESC
                LIMIT 1
                """,
                (self.backend_name, operation, _params_hash(params)),
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            try:
                payload["data"] = json.loads(str(payload.pop("payload_json")))
            except (TypeError, json.JSONDecodeError):
                return None
            return payload
        finally:
            cursor.close()
            connection.close()

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            rows = cursor.execute(
                """
                SELECT observation_id, source_backend, operation, target_key,
                       status, sensitive, observed_at, received_at, expires_at,
                       duration_ms, error_code
                FROM fleet_observations
                ORDER BY observation_id DESC
                LIMIT ?
                """,
                (min(max(int(limit), 1), 200),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            cursor.close()
            connection.close()

    def backend_snapshot(self) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                """
                SELECT backend_name, state, catalog_version, operations_json,
                       error_code, last_success_at, checked_at
                FROM fleet_backend_states WHERE backend_name = ?
                """, (self.backend_name,)
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            try:
                payload["operations"] = json.loads(
                    str(payload.pop("operations_json") or "[]")
                )
            except json.JSONDecodeError:
                payload["operations"] = []
            return payload
        finally:
            cursor.close()
            connection.close()
