"""Real historical CLI import followed by current CLI/schema upgrades."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from src.bot_storage.schema import HEAD_REVISION
from tests import test_qq_official_outbox_migration as migration
from tests.legacy_import_history import HistoricalImport, REVISION, ROOT
from tests.test_drop_dead_tables_migration import _schema_snapshot

BASE = "0113_group_attention_scheduler"
TABLES = ("maintenance_state", "embeddings")
RESIDUE = {"storage_migration_runs", "message_topic_edges", "delivery_attempt_quotas"}


class RetireLegacyImportTests(HistoricalImport, unittest.TestCase):
    def schema(self, revision=BASE):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, revision)
        return schema

    def test_published_cli_import_verify_then_current_upgrade(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        state = self.history / "TEST_STATE"
        state.mkdir()
        source = self.legacy.sqlite3.connect(state / "bot_state.sqlite3")
        with source:
            source.execute("CREATE TABLE conversations (conversation_id INTEGER PRIMARY KEY, "
                           "scope_key TEXT, platform TEXT, kind TEXT, native_conversation_id TEXT, "
                           "created_at INTEGER, updated_at INTEGER)")
            source.execute("INSERT INTO conversations VALUES "
                           "(42,'TEST_SCOPE','TEST_PLATFORM','group','TEST_NATIVE',1,1)")
        source.close()
        (state / "model_preferences.json").write_text('{"TEST_SCOPE":"TEST_MODEL"}')
        original = {p.name: p.read_bytes() for p in state.iterdir()}
        self.assertIn(REVISION, (ROOT / "docs/postgresql-migration.md").read_text())
        for entry in ("inspect-legacy", "migrate-legacy", "verify-legacy"):
            self.assertIn(entry, self.cli(self.history, schema, "--help"))
        self.cli(self.history, schema, "upgrade")
        inspect = json.loads(self.cli(self.history, schema, "inspect-legacy", "--state-dir", str(state)))
        self.assertEqual(inspect["rows"], 1)
        imported = json.loads(self.cli(self.history, schema, "migrate-legacy", "--state-dir", str(state), "--apply"))
        self.assertTrue(imported["verified"])
        verified = json.loads(self.cli(self.history, schema, "verify-legacy", "--state-dir", str(state)))
        self.assertTrue(verified["verified"])
        self.cli(ROOT, schema, "upgrade")
        self.assertIn(HEAD_REVISION, self.cli(ROOT, schema, "check"))
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute("SELECT conversation_id,scope_key FROM conversations").fetchall(), [(42, "TEST_SCOPE")])
            self.assertEqual(json.loads(connection.execute("SELECT payload_json FROM state_blobs WHERE namespace='model_preferences'").fetchone()[0]), {"TEST_SCOPE": "TEST_MODEL"})
            self.assertGreater(connection.execute("INSERT INTO conversations(scope_key,platform,kind,native_conversation_id,created_at,updated_at) VALUES ('TEST_NEXT','TEST_PLATFORM','group','TEST_NEXT',2,2) RETURNING conversation_id").fetchone()[0], 42)
        self.assertEqual(original, {p.name: p.read_bytes() for p in state.iterdir()})

    def test_published_nonempty_import_is_preserved_at_0113(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        state = self.history / "TEST_NONEMPTY"
        state.mkdir()
        for filename, ddl, insert, values in (
            ("maintenance_state.sqlite3", "CREATE TABLE maintenance_state(job_name TEXT PRIMARY KEY,last_success_key TEXT,updated_at INTEGER)", "INSERT INTO maintenance_state VALUES (?,?,?)", ("TEST_JOB", "TEST_KEY", 1)),
            ("bot_state.sqlite3", "CREATE TABLE embeddings(embedding_id INTEGER PRIMARY KEY,scope_key TEXT,source_type TEXT,source_id TEXT,model TEXT,dimensions INTEGER,content_hash TEXT,vector BLOB,created_at INTEGER)", "INSERT INTO embeddings VALUES (?,?,?,?,?,?,?,?,?)", (7, "TEST_SCOPE", "TEST_TYPE", "TEST_ID", "TEST_MODEL", 1, "TEST_HASH", b"\x00TEST_VECTOR", 1)),
        ):
            source = self.legacy.sqlite3.connect(state / filename)
            with source:
                source.execute(ddl)
                source.execute(insert, values)
            source.close()
        self.cli(self.history, schema, "upgrade")
        self.cli(self.history, schema, "migrate-legacy", "--state-dir", str(state), "--apply")
        self.assertTrue(json.loads(self.cli(self.history, schema, "verify-legacy", "--state-dir", str(state)))["verified"])
        self.assertIn("maintenance_state", self.cli(ROOT, schema, "upgrade", expected=1))
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute("SELECT * FROM maintenance_state").fetchall(), [("TEST_JOB", "TEST_KEY", 1)])
            self.assertEqual(connection.execute("SELECT embedding_id,vector FROM embeddings").fetchall(), [(7, b"\x00TEST_VECTOR")])
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], BASE)
            self.assertEqual(connection.execute("SELECT status FROM storage_migration_runs").fetchall(), [("verified",)])

    def test_fresh_cli_install_and_retired_entry_errors(self):
        schema = self.schema("head")
        self.cli(ROOT, schema, "upgrade")
        self.assertIn(HEAD_REVISION, self.cli(ROOT, schema, "check"))
        self.assertIn(HEAD_REVISION, self.cli(ROOT, schema, "current"))
        self.assertIn("docs/postgresql-migration.md", self.cli(ROOT, schema, "--help"))
        with migration.connect(schema) as connection:
            tables = _schema_snapshot(connection, schema)[0]
            self.assertTrue(set(TABLES).isdisjoint(tables))
            self.assertTrue(RESIDUE.isdisjoint(tables))
        for entry in ("inspect-legacy", "migrate-legacy", "verify-legacy"):
            self.assertIn("invalid choice", self.cli(ROOT, schema, entry, expected=2))

    def test_nonempty_either_table_aborts_without_changing_transaction(self):
        schema = self.schema()
        with migration.connect(schema) as connection:
            before = _schema_snapshot(connection, schema)
        for table, statement in (
            ("maintenance_state", "INSERT INTO maintenance_state VALUES ('TEST_JOB','TEST_KEY',1)"),
            ("embeddings", "INSERT INTO embeddings(scope_key,source_type,source_id,model,dimensions,content_hash,vector,created_at) VALUES ('TEST_SCOPE','TEST_TYPE','TEST_ID','TEST_MODEL',1,'TEST_HASH',decode('0001','hex'),1)"),
        ):
            with self.subTest(table=table):
                with migration.connect(schema) as connection:
                    connection.execute(statement)
                self.assertIn(table, self.cli(ROOT, schema, "upgrade", expected=1))
                with migration.connect(schema) as connection:
                    self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], BASE)
                    self.assertEqual(_schema_snapshot(connection, schema), before)
                    self.assertEqual(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0], 1)
                    connection.execute(f'DELETE FROM "{table}"')

    def test_downgrade_recreates_exact_catalog_and_identity(self):
        schema = self.schema()

        def catalog(connection):
            queries = (
                "SELECT table_name,column_name,data_type,is_nullable,column_default,is_identity,"
                "identity_generation,identity_start,identity_increment FROM information_schema.columns "
                "WHERE table_schema=%s ORDER BY table_name,ordinal_position",
                "SELECT c.conname,pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=%s ORDER BY 1,2",
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname",
                "SELECT sequencename,data_type,start_value,min_value,max_value,increment_by,cycle,cache_size "
                "FROM pg_sequences WHERE schemaname=%s ORDER BY sequencename",
            )
            return tuple(connection.execute(query, (schema,)).fetchall() for query in queries)

        with migration.connect(schema) as connection:
            before = catalog(connection)
            tables = _schema_snapshot(connection, schema)[0]
            connection.execute("INSERT INTO storage_migration_runs VALUES ('TEST_RUN','TEST_FINGERPRINT','verified','{}',1,1)")
            connection.execute("INSERT INTO message_topic_edges(scope_key,source_message_id,target_message_id,relation,weight,created_at,updated_at) VALUES ('TEST_SCOPE',1,2,'TEST_RELATION',1,1,1)")
            fixture = migration.OfficialOutboxMigrationTests()
            fixture.connection = connection
            endpoint = fixture.endpoint()
            attempt = fixture.attempt(endpoint, fixture.delivery(endpoint))
            quota = fixture.quota(endpoint)
            migration.insert(connection, "delivery_attempt_quotas", dict(
                attempt_id=attempt, quota_key=quota["quota_key"], account_key=endpoint.account.key,
                endpoint_key=endpoint.key, quota_endpoint_key=endpoint.key,
                units=1, charge_state="reserved", created_at=110, updated_at=110,
            ))
            kept = {name: connection.execute(f'SELECT * FROM {name}').fetchall() for name in RESIDUE}
        migration.migrate(schema, "0114_retire_legacy_import")
        with migration.connect(schema) as connection:
            self.assertEqual(tables - _schema_snapshot(connection, schema)[0], set(TABLES))
            for name, rows in kept.items():
                self.assertEqual(connection.execute(f'SELECT * FROM {name}').fetchall(), rows)
        migration.migrate(schema, BASE, downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(catalog(connection), before)
            self.assertEqual(connection.execute("INSERT INTO embeddings(scope_key,source_type,source_id,model,dimensions,content_hash,vector,created_at) VALUES ('TEST_SCOPE','TEST_TYPE','TEST_ID','TEST_MODEL',1,'TEST_HASH',decode('0001','hex'),1) RETURNING embedding_id").fetchone()[0], 1)
            connection.execute("DELETE FROM embeddings")
        migration.migrate(schema, "0118_retire_semantic_state")
        parents = ('deliveries', 'delivery_attempts', 'qq_official_quota_periods')
        with migration.connect(schema) as connection:
            before = _schema_snapshot(connection, schema)[0]
            retained = {name: connection.execute(f'SELECT * FROM {name}').fetchall() for name in parents}
            connection.execute('CREATE VIEW test_import_audit AS SELECT * FROM storage_migration_runs')
        self.assertIn('depend', self.cli(ROOT, schema, 'upgrade', expected=1))
        with migration.connect(schema) as connection:
            for name, rows in kept.items():
                self.assertEqual(connection.execute(f'SELECT * FROM {name}').fetchall(), rows)
            connection.execute('DROP VIEW test_import_audit')
        migration.migrate(schema, '0119_retire_import_residue')
        with migration.connect(schema) as connection:
            self.assertEqual(_schema_snapshot(connection, schema)[0], before - RESIDUE)
            for name, rows in retained.items():
                self.assertEqual(connection.execute(f'SELECT * FROM {name}').fetchall(), rows)
        with self.assertRaisesRegex(RuntimeError, 'pre-0119 full database backup'):
            migration.migrate(schema, '0118_retire_semantic_state', downgrade=True)
        migration.migrate(schema, '0122_retire_security_lock')
        with migration.connect(schema) as connection:
            before = _schema_snapshot(connection, schema)[0]
            connection.execute('UPDATE qq_official_quota_periods SET reserved_count=1')
        with self.assertRaisesRegex(RuntimeError, 'outstanding legacy files or quota'):
            migration.migrate(schema, '0123_retire_outbox_diagnostics')
        with migration.connect(schema) as connection:
            connection.execute('UPDATE qq_official_quota_periods SET reserved_count=0, used_count=6')
        migration.migrate(schema, '0123_retire_outbox_diagnostics')
        with migration.connect(schema) as connection:
            self.assertEqual(before - _schema_snapshot(connection, schema)[0],
                             {'qq_official_file_artifacts','qq_official_file_deliveries','qq_official_quota_periods'})
            for name in ('deliveries', 'delivery_attempts'):
                self.assertEqual(connection.execute(f'SELECT * FROM {name}').fetchall(), retained[name])

    def test_uncommitted_writer_blocks_migration_then_committed_data_refuses(self):
        schema = self.schema()
        with migration.connect(schema) as writer:
            writer.execute("INSERT INTO maintenance_state VALUES ('TEST_WRITER','TEST_KEY',1)")
            with patch.dict(os.environ, {"PGOPTIONS": "-c lock_timeout=200ms"}):
                self.assertIn("lock timeout", self.cli(ROOT, schema, "upgrade", expected=1))
        self.assertIn("maintenance_state", self.cli(ROOT, schema, "upgrade", expected=1))
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute("SELECT * FROM maintenance_state").fetchall(), [("TEST_WRITER", "TEST_KEY", 1)])
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], BASE)
            self.assertIn("embeddings", _schema_snapshot(connection, schema)[0])

    def test_external_dependency_is_not_cascaded_and_first_drop_rolls_back(self):
        schema = self.schema()
        with migration.connect(schema) as connection:
            connection.execute("CREATE VIEW test_dependency AS SELECT * FROM embeddings")
            before = _schema_snapshot(connection, schema)
        self.assertIn("depend", self.cli(ROOT, schema, "upgrade", expected=1))
        with migration.connect(schema) as connection:
            self.assertEqual(_schema_snapshot(connection, schema), before)
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], BASE)
            self.assertEqual(connection.execute("SELECT * FROM test_dependency").fetchall(), [])
