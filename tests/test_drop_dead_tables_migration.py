"""Verify the exact catalogs and retained data on both historical dead-table retirements."""
from __future__ import annotations

from contextlib import closing
import importlib
import unittest

from src.bot_storage.database import PostgresDatabase
from tests import test_qq_official_outbox_migration as migration


def _schema_snapshot(connection, schema: str) -> tuple[set[str], set[tuple], set[tuple], set[str]]:
    def rows(query):
        return set(connection.execute(query, (schema,)))

    tables = rows("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE'")
    columns = rows("SELECT table_name,column_name,data_type,is_nullable FROM information_schema.columns WHERE table_schema=%s")
    constraints = rows("SELECT table_name,constraint_name,constraint_type FROM information_schema.table_constraints "
                       "WHERE table_schema=%s AND constraint_name NOT LIKE '%%_not_null'")
    indexes = rows("SELECT indexname FROM pg_indexes WHERE schemaname=%s")
    return {name for name, in tables}, columns, constraints, {name for name, in indexes}


class DropDeadTablesMigrationTests(unittest.TestCase):
    def assert_round_trip(self, base, revision, count, seed_sql):
        module = importlib.import_module('migrations.versions.' + revision)
        self.assertEqual(module.revision, revision)
        retired = set(module.DEAD_TABLES)
        self.assertEqual(len(retired), count)
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, base)
        with migration.connect(schema) as db:
            before = _schema_snapshot(db, schema)
            self.assertTrue(retired <= before[0])
            db.execute("INSERT INTO qq_official_accounts(account_key,enabled,fencing_epoch,gateway_state,created_at,updated_at) "
                       "VALUES ('qq-official:TEST_APP:test',TRUE,1,'active',1,1)")
            for sql in seed_sql:
                db.execute(sql)
        migration.migrate(schema, revision)
        with closing(PostgresDatabase(migration.checked_test_dsn(), schema=schema)) as store:
            store.healthcheck()
            store.require_revision(revision)
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema)[0], before[0] - retired)
            self.assertEqual(db.execute("SELECT count(*) FROM qq_official_accounts WHERE account_key='qq-official:TEST_APP:test'").fetchone(), (1,))
        migration.migrate(schema, base, downgrade=True)
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema), before)
            for table in sorted(retired):
                self.assertEqual(db.execute(f'SELECT count(*) FROM "{table}"').fetchone(), (0,), table)
        migration.migrate(schema, revision)
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema)[0], before[0] - retired)

    def test_0107(self):
        self.assert_round_trip('0106_question_notices', '0107_drop_dead_tables', 34, (
            "INSERT INTO alert_events(alert_key,fingerprint,incident_key,name,severity,status,starts_at,first_seen_at,last_seen_at) "
            "VALUES ('TEST_ALERT','fp1','inc1','test','warning','firing',1,1,1)",
            "INSERT INTO fleet_workers(worker_id,host_id,boot_id,protocol_version,availability,last_seen_at) "
            "VALUES ('TEST_WORKER','TEST_HOST','boot1',1,'available',1)",
            "INSERT INTO admin_fleet_watches(path,account_json,bot_id,actor,origin,updated_at) "
            "VALUES ('/test','{}','TEST_BOT','TEST_ACTOR','https://test',1)",
            "INSERT INTO host_api_quota(scope_key,api_name,hour,used) VALUES ('TEST_SCOPE','test',1,2)",
        ))

    def test_0110(self):
        self.assert_round_trip('0109_schedule_turn_kind', '0110_drop_remaining_dead_tables', 18, (
            "INSERT INTO program_quota(scope_key,kind,hour,used) VALUES ('TEST_SCOPE','say',1,2)",
        ))
