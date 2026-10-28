from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar


T = TypeVar("T")
_RESOURCE_KEY = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,199}$")


class AdminVersionConflict(RuntimeError):
    def __init__(self, resource_key: str, expected: int, current: int) -> None:
        super().__init__(
            f"resource {resource_key} changed: expected version {expected}, "
            f"current version is {current}"
        )
        self.resource_key = resource_key
        self.expected = expected
        self.current = current


@dataclass(frozen=True)
class MutationResult:
    value: Any
    resource_key: str
    resource_version: int


class AdminControlStore:
    """Serializes admin mutations and keeps their versions and audit trail."""

    def __init__(self, database: Any = None) -> None:
        self._database = (
            database if callable(getattr(database, "store_connection", None)) else None
        )
        self._lock = threading.RLock()
        self._memory_versions: dict[str, int] = {}
        self._memory_audit: list[dict[str, object]] = []
        self._memory_tools: dict[str, bool] = {}

    @property
    def persistent(self) -> bool:
        return self._database is not None

    def version(self, resource_key: str) -> int:
        key = _normalized_resource_key(resource_key)
        if self._database is None:
            return self._memory_versions.get(key, 0)
        connection = self._database.store_connection()
        try:
            row = connection.execute(
                "SELECT version FROM admin_resource_versions WHERE resource_key = ?",
                (key,),
            ).fetchone()
        finally:
            connection.close()
        return int(row["version"]) if row is not None else 0

    def versions(self) -> dict[str, int]:
        if self._database is None:
            return dict(sorted(self._memory_versions.items()))
        connection = self._database.store_connection()
        try:
            rows = connection.execute(
                "SELECT resource_key, version FROM admin_resource_versions "
                "ORDER BY resource_key"
            ).fetchall()
        finally:
            connection.close()
        return {str(row["resource_key"]): int(row["version"]) for row in rows}

    def mutate(
        self,
        resource_key: str,
        *,
        expected_version: int | None,
        actor: str,
        action: str,
        target: str = "",
        before: object = None,
        operation: Callable[[int], T],
    ) -> MutationResult:
        key = _normalized_resource_key(resource_key)
        normalized_actor = _safe_text(actor, 160) or "admin-console"
        normalized_action = _safe_text(action, 120) or "update"
        normalized_target = _safe_text(target, 300)
        with self._lock:
            current = self.version(key)
            if expected_version is not None and expected_version != current:
                raise AdminVersionConflict(key, expected_version, current)
            next_version = current + 1
            try:
                value = operation(next_version)
            except Exception as exc:
                self._record_audit(
                    key,
                    current,
                    normalized_actor,
                    normalized_action,
                    normalized_target,
                    "failed",
                    before,
                    None,
                    str(exc),
                )
                raise
            self._record_success(
                key,
                next_version,
                normalized_actor,
                normalized_action,
                normalized_target,
                before,
                value,
            )
            return MutationResult(value, key, next_version)

    def audit(self, *, limit: int = 200) -> list[dict[str, object]]:
        bounded = min(max(int(limit), 1), 500)
        if self._database is None:
            return [dict(item) for item in self._memory_audit[-bounded:]][::-1]
        connection = self._database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT audit_id, resource_key, resource_version, actor, action,
                       target, status, before_json, after_json, error, created_at
                FROM admin_audit_log
                ORDER BY audit_id DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "audit_id": int(row["audit_id"]),
                "resource_key": str(row["resource_key"]),
                "resource_version": int(row["resource_version"]),
                "actor": str(row["actor"]),
                "action": str(row["action"]),
                "target": str(row["target"]),
                "status": str(row["status"]),
                "before": _decode_json(row["before_json"]),
                "after": _decode_json(row["after_json"]),
                "error": str(row["error"]),
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]

    def mutate_transaction(
        self, cursor, resource_key, *, expected_version, actor, action, target,
        operation,
    ) -> MutationResult:
        """Use the caller's transaction for DB-only writes and their audit receipt.

        The caller owns rollback. Do not perform network or filesystem effects
        inside this operation; an audit failure must roll back the domain write.
        """
        if not self.persistent or expected_version is None:
            raise ValueError("Persistent mutation requires an expected resource version")
        key, now = _normalized_resource_key(resource_key), int(time.time())
        cursor.execute("""INSERT INTO admin_resource_versions(resource_key,version,updated_at)
            VALUES (?,0,?) ON CONFLICT(resource_key) DO NOTHING""", (key, now))
        current = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key=? FOR UPDATE",
                                 (key,)).fetchone()[0]
        if current != expected_version:
            raise AdminVersionConflict(key, expected_version, current)
        before, value = operation()
        version = current + 1
        cursor.execute("UPDATE admin_resource_versions SET version=?,updated_at=? WHERE resource_key=?",
                       (version, now, key))
        cursor.execute("""INSERT INTO admin_audit_log(resource_key,resource_version,actor,action,target,
            status,before_json,after_json,error,created_at) VALUES (?,?,?,?,?,'succeeded',?,?,'',?)""",
            (key, version, _safe_text(actor, 160), _safe_text(action, 120), _safe_text(target, 300),
             _encode_json(before), _encode_json(value), now))
        return MutationResult(value, key, version)

    def tool_overrides(self) -> dict[str, bool]:
        if self._database is None:
            return dict(self._memory_tools)
        connection = self._database.store_connection()
        try:
            rows = connection.execute(
                "SELECT tool_name, enabled FROM admin_tool_overrides ORDER BY tool_name"
            ).fetchall()
        finally:
            connection.close()
        return {str(row["tool_name"]): bool(row["enabled"]) for row in rows}

    def set_tool_override(
        self,
        tool_name: str,
        enabled: bool,
        *,
        actor: str,
        resource_version: int,
    ) -> dict[str, object]:
        name = _safe_text(tool_name, 160)
        if not name:
            raise ValueError("tool name is required")
        if self._database is None:
            self._memory_tools[name] = bool(enabled)
        else:
            now = int(time.time())
            connection = self._database.store_connection()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO admin_tool_overrides (
                        tool_name, enabled, resource_version, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(tool_name) DO UPDATE SET
                        enabled = EXCLUDED.enabled,
                        resource_version = EXCLUDED.resource_version,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        name,
                        int(bool(enabled)),
                        int(resource_version),
                        _safe_text(actor, 160) or "admin-console",
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return {"tool_name": name, "enabled": bool(enabled)}

    def _record_success(
        self,
        resource_key: str,
        resource_version: int,
        actor: str,
        action: str,
        target: str,
        before: object,
        after: object,
    ) -> None:
        now = int(time.time())
        if self._database is None:
            self._memory_versions[resource_key] = resource_version
            self._append_memory_audit(
                resource_key,
                resource_version,
                actor,
                action,
                target,
                "succeeded",
                before,
                after,
                "",
                now,
            )
            return
        connection = self._database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO admin_resource_versions (resource_key, version, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                    version = EXCLUDED.version,
                    updated_at = EXCLUDED.updated_at
                """,
                (resource_key, resource_version, now),
            )
            cursor.execute(
                """
                INSERT INTO admin_audit_log (
                    resource_key, resource_version, actor, action, target,
                    status, before_json, after_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?, '', ?)
                """,
                (
                    resource_key,
                    resource_version,
                    actor,
                    action,
                    target,
                    _encode_json(before),
                    _encode_json(after),
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _record_audit(
        self,
        resource_key: str,
        resource_version: int,
        actor: str,
        action: str,
        target: str,
        status: str,
        before: object,
        after: object,
        error: str,
    ) -> None:
        now = int(time.time())
        if self._database is None:
            self._append_memory_audit(
                resource_key,
                resource_version,
                actor,
                action,
                target,
                status,
                before,
                after,
                error,
                now,
            )
            return
        connection = self._database.store_connection()
        try:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    resource_key, resource_version, actor, action, target,
                    status, before_json, after_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resource_key,
                    resource_version,
                    actor,
                    action,
                    target,
                    status,
                    _encode_json(before),
                    _encode_json(after),
                    _safe_text(error, 1000),
                    now,
                ),
            )
        finally:
            connection.close()

    def _append_memory_audit(
        self,
        resource_key: str,
        resource_version: int,
        actor: str,
        action: str,
        target: str,
        status: str,
        before: object,
        after: object,
        error: str,
        created_at: int,
    ) -> None:
        self._memory_audit.append(
            {
                "audit_id": len(self._memory_audit) + 1,
                "resource_key": resource_key,
                "resource_version": resource_version,
                "actor": actor,
                "action": action,
                "target": target,
                "status": status,
                "before": before,
                "after": after,
                "error": _safe_text(error, 1000),
                "created_at": created_at,
            }
        )
        if len(self._memory_audit) > 1000:
            del self._memory_audit[:-1000]


def parse_expected_version(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    normalized = normalized.strip('"')
    if not normalized.isdigit():
        raise ValueError("If-Match must contain a numeric resource version")
    return int(normalized)


def _normalized_resource_key(value: str) -> str:
    normalized = str(value).strip().lower()
    if not _RESOURCE_KEY.fullmatch(normalized):
        raise ValueError("invalid admin resource key")
    return normalized


def _safe_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _encode_json(value: object) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _decode_json(value: object) -> object:
    try:
        return json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
