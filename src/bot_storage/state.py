from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Protocol, Union

from .database import PostgresDatabase


class JsonState(Protocol):
    def load(self) -> Any | None: ...

    def save(self, payload: object) -> None: ...


StateSource = Union[Path, PostgresDatabase, None]


class FileJsonState:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def load(self) -> Any | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, payload: object) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        except OSError:
            return


class PostgresJsonState:
    def __init__(self, database: PostgresDatabase, namespace: str) -> None:
        self.namespace = namespace
        self._connection = database.store_connection()
        self._lock = threading.RLock()

    def load(self) -> Any | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM state_blobs WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(str(row[0]))
        except json.JSONDecodeError:
            return None

    def save(self, payload: object) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                cursor.execute(
                    """
                    INSERT INTO state_blobs(namespace, payload_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(namespace) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (self.namespace, encoded, int(time.time())),
                )
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()


def open_json_state(source: StateSource, namespace: str) -> JsonState:
    if isinstance(source, PostgresDatabase):
        return PostgresJsonState(source, namespace)
    return FileJsonState(source)
