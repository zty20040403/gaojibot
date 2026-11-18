"""Verify 0111 single-row scope_leases + checkpoint columns replace scattered advisory locks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest

from src.bot_storage.database import PostgresDatabase
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.scope_lease import serialize_scope_writer
from tests import test_qq_official_outbox_migration as migration
from tests.test_drop_dead_tables_migration import _schema_snapshot


class ScopeLeaseMigrationTests(unittest.TestCase):
    def test_zero_scattered_advisory_xact_locks_in_ai_chat(self) -> None:
        root = Path(__file__).parents[1] / "src" / "plugins" / "ai_chat"
        hits = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "pg_advisory_xact_lock" in text:
                hits.append(path.relative_to(root).as_posix())
        self.assertEqual(hits, [])

    def test_upgrade_downgrade_and_single_row_serialization(self) -> None:
        self.assertEqual(HEAD_REVISION, "0111_scope_leases_checkpoints")
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)

        migration.migrate(schema, "0110_drop_remaining_dead_tables")
        with migration.connect(schema) as connection:
            before_tables, before_cols, _, _ = _schema_snapshot(connection, schema)
            self.assertNotIn("scope_leases", before_tables)

        migration.migrate(schema, "0111_scope_leases_checkpoints")
        db = PostgresDatabase(migration.checked_test_dsn(), schema=schema)
        try:
            db.healthcheck()
            db.require_revision("0111_scope_leases_checkpoints")
        finally:
            db.close()

        with migration.connect(schema) as connection:
            after_tables, after_cols, _, _ = _schema_snapshot(connection, schema)
            self.assertIn("scope_leases", after_tables)
            col_pairs = {(c[0], c[1]) for c in after_cols}
            self.assertIn(("qq_official_turn_executions", "checkpoint_json"), col_pairs)
            self.assertIn(("qq_official_turn_executions", "checkpoint_step"), col_pairs)
            self.assertIn(("agent_turns", "checkpoint_json"), col_pairs)
            self.assertEqual(sum(1 for c in after_cols if c[0] == "scope_leases"), 5)

        # Concurrent writers on the same scope_key serialize via the single row and advance generation.
        barrier = threading.Barrier(4)

        def worker(idx: int) -> int:
            with migration.connect(schema) as conn:
                barrier.wait(timeout=10)
                with conn.transaction():
                    gen = serialize_scope_writer(conn, "turn:TEST_SCOPE", owner=f"w{idx}", now=1000 + idx)
                return gen

        with ThreadPoolExecutor(max_workers=4) as pool:
            gens = sorted(pool.map(worker, range(4)))
        self.assertEqual(gens, [1, 2, 3, 4])

        with migration.connect(schema) as connection:
            rows = connection.execute("SELECT scope_key, lease_generation FROM scope_leases").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0][1]), 4)

        migration.migrate(schema, "0110_drop_remaining_dead_tables", downgrade=True)
        with migration.connect(schema) as connection:
            down_tables, down_cols, _, _ = _schema_snapshot(connection, schema)
            self.assertEqual(down_tables, before_tables)
            self.assertEqual(down_cols, before_cols)


if __name__ == "__main__":
    unittest.main()
