"""Verify 0107 drops dead tables and downgrade recreates empty schemas."""
from __future__ import annotations

import importlib
import unittest

from src.bot_storage.database import PostgresDatabase
from src.bot_storage.schema import HEAD_REVISION
from tests import test_qq_official_outbox_migration as migration

drop_migration = importlib.import_module("migrations.versions.0107_drop_dead_tables")
DEAD_TABLES = frozenset(drop_migration.DEAD_TABLES)


def _schema_snapshot(connection, schema: str) -> tuple[set[str], set[tuple], set[tuple], set[str]]:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (schema,),
        )
    }
    columns = {
        tuple(row)
        for row in connection.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = %s",
            (schema,),
        )
    }
    constraints = {
        tuple(row)
        for row in connection.execute(
            "SELECT table_name, constraint_name, constraint_type "
            "FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND constraint_name NOT LIKE '%%_not_null'",
            (schema,),
        )
    }
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
            (schema,),
        )
    }
    return tables, columns, constraints, indexes


class DropDeadTablesMigrationTests(unittest.TestCase):
    def test_upgrade_drops_dead_tables_and_downgrade_recreates_empty_schema(self) -> None:
        self.assertEqual(HEAD_REVISION, "0107_drop_dead_tables")
        self.assertEqual(len(DEAD_TABLES), 34)
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)

        migration.migrate(schema, "0106_question_notices")
        with migration.connect(schema) as connection:
            before_tables, before_cols, before_cons, before_idx = _schema_snapshot(connection, schema)
            self.assertTrue(DEAD_TABLES.issubset(before_tables))
            connection.execute(
                "INSERT INTO qq_official_accounts(account_key, enabled, fencing_epoch, gateway_state, created_at, updated_at) "
                "VALUES ('qq-official:TEST_APP:test', TRUE, 1, 'active', 1, 1)"
            )
            connection.execute(
                "INSERT INTO alert_events(alert_key, fingerprint, incident_key, name, severity, status, starts_at, first_seen_at, last_seen_at) "
                "VALUES ('TEST_ALERT', 'fp1', 'inc1', 'test', 'warning', 'firing', 1, 1, 1)"
            )
            connection.execute(
                "INSERT INTO fleet_workers(worker_id, host_id, boot_id, protocol_version, availability, last_seen_at) "
                "VALUES ('TEST_WORKER', 'TEST_HOST', 'boot1', 1, 'available', 1)"
            )
            connection.execute(
                "INSERT INTO admin_fleet_watches(path, account_json, bot_id, actor, origin, updated_at) "
                "VALUES ('/test', '{}', 'TEST_BOT', 'TEST_ACTOR', 'https://test', 1)"
            )
            connection.execute(
                "INSERT INTO host_api_quota(scope_key, api_name, hour, used) VALUES ('TEST_SCOPE', 'test', 1, 2)"
            )

        migration.migrate(schema, "0107_drop_dead_tables")
        db = PostgresDatabase(migration.checked_test_dsn(), schema=schema)
        try:
            db.healthcheck()
            db.require_revision(HEAD_REVISION)
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

        migration.migrate(schema, "0106_question_notices", downgrade=True)
        with migration.connect(schema) as connection:
            down_tables, down_cols, down_cons, down_idx = _schema_snapshot(connection, schema)
            self.assertEqual(down_tables, before_tables)
            self.assertEqual(down_cols, before_cols)
            self.assertEqual(down_cons, before_cons)
            self.assertEqual(down_idx, before_idx)
            for table in sorted(DEAD_TABLES):
                count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                self.assertEqual(count, 0, table)

        migration.migrate(schema, "0107_drop_dead_tables")
        with migration.connect(schema) as connection:
            final_tables, _, _, _ = _schema_snapshot(connection, schema)
            self.assertTrue(DEAD_TABLES.isdisjoint(final_tables))
