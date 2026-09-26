from __future__ import annotations

import json
import os
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import nonebot
import psycopg
from psycopg.conninfo import conninfo_to_dict

os.environ.setdefault("AI_ALLOW_LEGACY_SQLITE", "true")
nonebot.init()

from src.bot_storage.database import (
    PostgresDatabase,
    StoreRow,
    _convert_qmark_placeholders,
    _translate_sql,
)
from src.bot_storage.legacy_migration import capture_legacy_snapshot
from src.plugins.ai_chat.semantic_recall import SemanticDocument, _document_key


class PostgresCompatibilityTests(unittest.TestCase):
    def test_pool_bounds_broken_network_waits_and_preserves_dsn_overrides(self) -> None:
        with patch("src.bot_storage.database.ConnectionPool") as pool:
            database = PostgresDatabase(
                "postgresql://bot@example.test,backup.test/db?target_session_attrs=read-write"
            )
            options = conninfo_to_dict(pool.call_args.kwargs["conninfo"])
            self.assertEqual(options["host"], "example.test,backup.test")
            self.assertEqual(options["target_session_attrs"], "read-write")
            self.assertEqual(options["connect_timeout"], "3")
            self.assertEqual(options["keepalives_idle"], "10")
            self.assertEqual(options["keepalives_interval"], "5")
            self.assertEqual(options["keepalives_count"], "3")
            self.assertEqual(options["tcp_user_timeout"], "15000")
            database.close()

            database = PostgresDatabase(
                "host=example.test dbname=db connect_timeout=8 tcp_user_timeout=40000 keepalives_idle=20"
            )
            options = conninfo_to_dict(pool.call_args.kwargs["conninfo"])
            self.assertEqual(options["connect_timeout"], "8")
            self.assertEqual(options["tcp_user_timeout"], "40000")
            self.assertEqual(options["keepalives_idle"], "20")
            database.close()

    def test_pool_rejects_a_connection_demoted_to_read_only(self) -> None:
        class FakeConnection:
            autocommit = False

            def execute(self, query: str):
                self.query = query
                return SimpleNamespace(fetchone=lambda: ("on",))

        connection = FakeConnection()
        with self.assertRaises(psycopg.OperationalError):
            PostgresDatabase._probe_read_write_connection(connection)  # type: ignore[arg-type]

        self.assertEqual(connection.query, "SHOW transaction_read_only")
        self.assertFalse(connection.autocommit)

    def test_pool_accepts_the_current_writable_primary(self) -> None:
        class FakeConnection:
            autocommit = False

            def execute(self, _query: str):
                return SimpleNamespace(fetchone=lambda: ("off",))

        connection = FakeConnection()
        PostgresDatabase._probe_read_write_connection(connection)  # type: ignore[arg-type]

        self.assertFalse(connection.autocommit)

    def test_pool_health_check_is_cached_per_physical_connection(self) -> None:
        database = object.__new__(PostgresDatabase)
        database._health_check_interval_seconds = 5.0
        database._health_check_lock = threading.Lock()
        database._health_checked_at = {}
        connection = SimpleNamespace()

        with patch.object(
            database,
            "_probe_read_write_connection",
        ) as probe, patch(
            "src.bot_storage.database.time.monotonic",
            side_effect=(100.0, 101.0, 106.0),
        ):
            database._check_read_write_connection(connection)
            database._check_read_write_connection(connection)
            database._check_read_write_connection(connection)

        self.assertEqual(probe.call_count, 2)

    def test_topology_nodes_follow_multi_host_dsn_order(self) -> None:
        database = object.__new__(PostgresDatabase)
        database.dsn = (
            "postgresql://bot:secret@100.64.0.3:55432,"
            "100.64.0.4:55432/qq_bot"
        )
        database._node_names = ("h610", "tank")

        self.assertEqual(
            database._configured_nodes(),
            [
                {"name": "h610", "host": "100.64.0.3", "port": "55432"},
                {"name": "tank", "host": "100.64.0.4", "port": "55432"},
            ],
        )

    def test_rows_support_numeric_and_named_access(self) -> None:
        row = StoreRow(("message_id", "body"), (7, "hello"))
        self.assertEqual(row[0], 7)
        self.assertEqual(row["message_id"], 7)
        self.assertEqual(dict(row), {"message_id": 7, "body": "hello"})

    def test_qmark_translation_ignores_quoted_question_marks(self) -> None:
        query = "SELECT '?', \"?\" FROM messages WHERE id = ? AND body = ?"
        self.assertEqual(
            _convert_qmark_placeholders(query),
            "SELECT '?', \"?\" FROM messages WHERE id = %s AND body = %s",
        )

    def test_insert_translation_returns_identity_and_maps_ignore(self) -> None:
        query, identity = _translate_sql(
            "INSERT INTO messages(body_json) VALUES (?)"
        )
        self.assertEqual(identity, "canonical_message_id")
        self.assertTrue(query.endswith('RETURNING "canonical_message_id"'))

        ignored, identity = _translate_sql(
            "INSERT OR IGNORE INTO context_state(scope_key) VALUES (?)"
        )
        self.assertIsNone(identity)
        self.assertIn("ON CONFLICT DO NOTHING", ignored)


class LegacySnapshotTests(unittest.TestCase):
    def test_snapshot_has_stable_row_and_json_digests(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            connection = sqlite3.connect(state_dir / "bot_state.sqlite3")
            connection.execute(
                """
                CREATE TABLE conversations (
                    conversation_id INTEGER PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    native_conversation_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO conversations VALUES (1, 'scope', 'onebot-v11', "
                "'group', '42', 10, 11)"
            )
            connection.commit()
            connection.close()
            (state_dir / "model_preferences.json").write_text(
                json.dumps({"scope": "deepseek"}),
                encoding="utf-8",
            )
            (state_dir / "learned_stickers.json").write_text(
                json.dumps([{"type": "face", "data": {"id": "14"}}]),
                encoding="utf-8",
            )

            first = capture_legacy_snapshot(state_dir)
            second = capture_legacy_snapshot(state_dir)

            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(first.row_count, 1)
            self.assertEqual(
                first.json_states,
                {
                    "learned_stickers": [
                        {"type": "face", "data": {"id": "14"}}
                    ],
                    "model_preferences": {"scope": "deepseek"},
                },
            )

    def test_invalid_json_fails_before_any_database_write(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            (state_dir / "long_term_memory.json").write_text(
                "{broken",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "legacy JSON is invalid"):
                capture_legacy_snapshot(state_dir)

    def test_legacy_semantic_keys_are_normalized_for_postgres(self) -> None:
        document = SemanticDocument("scope", "message", "msg#1", "body", {})
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            connection = sqlite3.connect(
                state_dir / "semantic_index_state.sqlite3"
            )
            connection.execute(
                """
                CREATE TABLE semantic_index_state (
                    source_key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    indexed_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO semantic_index_state VALUES (?, 'hash', 10)",
                ("scope\0message\0msg#1",),
            )
            connection.commit()
            connection.close()

            snapshot = capture_legacy_snapshot(state_dir)
            table = next(
                item
                for item in snapshot.tables
                if item.spec.table == "semantic_index_state"
            )

            self.assertEqual(table.rows[0][0], _document_key(document))
            self.assertNotIn("\0", str(table.rows[0][0]))


if __name__ == "__main__":
    unittest.main()
