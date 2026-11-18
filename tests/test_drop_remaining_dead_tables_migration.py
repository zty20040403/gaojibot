"""Verify 0110 drops remaining 18 dead tables and downgrade recreates empty schemas."""
from __future__ import annotations

import importlib
import unittest

from src.bot_storage.database import PostgresDatabase
from tests import test_qq_official_outbox_migration as migration
from tests.test_drop_dead_tables_migration import _schema_snapshot

drop_migration = importlib.import_module("migrations.versions.0110_drop_remaining_dead_tables")
DEAD_TABLES = frozenset(drop_migration.DEAD_TABLES)


class DropRemainingDeadTablesMigrationTests(unittest.TestCase):
    def test_upgrade_drops_remaining_dead_tables_and_downgrade_recreates_empty_schema(self) -> None:
        self.assertEqual(drop_migration.revision, "0110_drop_remaining_dead_tables")
        self.assertEqual(len(DEAD_TABLES), 18)
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)

        migration.migrate(schema, "0109_schedule_turn_kind")
        with migration.connect(schema) as connection:
            before_tables, before_cols, before_cons, before_idx = _schema_snapshot(connection, schema)
            self.assertTrue(DEAD_TABLES.issubset(before_tables))
            connection.execute(
                "INSERT INTO qq_official_accounts(account_key, enabled, fencing_epoch, gateway_state, created_at, updated_at) "
                "VALUES ('qq-official:TEST_APP:test', TRUE, 1, 'active', 1, 1)"
            )
            connection.execute(
                "INSERT INTO program_quota(scope_key, kind, hour, used) VALUES ('TEST_SCOPE', 'say', 1, 2)"
            )

        migration.migrate(schema, "0110_drop_remaining_dead_tables")
        db = PostgresDatabase(migration.checked_test_dsn(), schema=schema)
        try:
            db.healthcheck()
            db.require_revision("0110_drop_remaining_dead_tables")
        finally:
            db.close()

        with migration.connect(schema) as connection:
            after_tables, _, _, _ = _schema_snapshot(connection, schema)
            self.assertEqual(before_tables - after_tables, DEAD_TABLES)
            self.assertTrue(DEAD_TABLES.isdisjoint(after_tables))
            kept = connection.execute(
                "SELECT count(*) FROM qq_official_accounts WHERE account_key = 'qq-official:TEST_APP:test'"
            ).fetchone()[0]
            self.assertEqual(kept, 1)

        migration.migrate(schema, "0109_schedule_turn_kind", downgrade=True)
        with migration.connect(schema) as connection:
            down_tables, down_cols, down_cons, down_idx = _schema_snapshot(connection, schema)
            self.assertEqual(down_tables, before_tables)
            self.assertEqual(down_cols, before_cols)
            self.assertEqual(down_cons, before_cons)
            self.assertEqual(down_idx, before_idx)
            for table in sorted(DEAD_TABLES):
                count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                self.assertEqual(count, 0, table)

        migration.migrate(schema, "0110_drop_remaining_dead_tables")
        with migration.connect(schema) as connection:
            final_tables, _, _, _ = _schema_snapshot(connection, schema)
            self.assertTrue(DEAD_TABLES.isdisjoint(final_tables))
