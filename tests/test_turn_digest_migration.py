"""0116 one-to-one turn digest migration coverage on an owned test schema."""

from __future__ import annotations

import unittest

import psycopg
from sqlalchemy.exc import DBAPIError

from src.bot_storage.database import PostgresDatabase
from src.plugins.ai_chat.turn_journal import TurnJournal
from tests import test_qq_official_outbox_migration as migration

BASE = "0115_group_interest_sharing"
HEAD = "0116_inline_turn_digests"


def insert_turn(connection, ordinal: int, *, status: str = "succeeded") -> int:
    finished_at = None if status == "running" else ordinal + 100
    return connection.execute(
        """INSERT INTO agent_turns (
          scope_key, turn_ordinal, status, provider, model, profile, objective,
          started_at, finished_at, prompt_version, tool_catalog_version
        ) VALUES ('TEST_SCOPE', %s, %s, 'TEST_PROVIDER', 'TEST_MODEL',
          'TEST_PROFILE', %s, %s, %s, 'TEST_PROMPT', 'TEST_TOOLS')
        RETURNING turn_id""",
        (ordinal, status, f"objective {ordinal}", ordinal, finished_at),
    ).fetchone()[0]


class TurnDigestMigrationTests(unittest.TestCase):
    def schema(self) -> str:
        value = migration.owned_schema()
        self.addCleanup(migration.drop, value)
        migration.migrate(value, BASE)
        return value

    def test_round_trip_preserves_values_absence_catalog_and_new_writes(self) -> None:
        schema = self.schema()
        with migration.connect(schema) as connection:
            missing = insert_turn(connection, 1, status="running")
            empty = insert_turn(connection, 2)
            populated = insert_turn(connection, 3)
            connection.execute(
                "INSERT INTO turn_digests VALUES (%s, '', '', 120)", (empty,)
            )
            connection.execute(
                "INSERT INTO turn_digests VALUES (%s, %s, %s, 987654321)",
                (populated, "skeleton text", "note text"),
            )

        migration.migrate(schema, HEAD)
        with migration.connect(schema) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT digest_created_at FROM agent_turns WHERE turn_id=%s",
                    (missing,),
                ).fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    """SELECT turn_id,digest_skeleton,digest_approach,digest_created_at
                    FROM agent_turns WHERE digest_created_at IS NOT NULL ORDER BY turn_id"""
                ).fetchall(),
                [
                    (empty, "", "", 120),
                    (populated, "skeleton text", "note text", 987654321),
                ],
            )
            with self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(
                    "UPDATE agent_turns SET digest_skeleton='partial' WHERE turn_id=%s",
                    (missing,),
                )
            connection.rollback()
            new_missing = insert_turn(connection, 4)
            new_digest = insert_turn(connection, 5)
            connection.execute(
                """UPDATE agent_turns SET digest_skeleton=%s, digest_approach='',
                digest_created_at=555 WHERE turn_id=%s""",
                ("new skeleton", new_digest),
            )

        migration.migrate(schema, BASE, downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT turn_id,skeleton,approach,created_at FROM turn_digests ORDER BY turn_id"
                ).fetchall(),
                [
                    (empty, "", "", 120),
                    (populated, "skeleton text", "note text", 987654321),
                    (new_digest, "new skeleton", "", 555),
                ],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT column_default FROM information_schema.columns
                    WHERE table_schema=%s AND table_name='turn_digests'
                      AND column_name='approach'""",
                    (schema,),
                ).fetchone()[0],
                "''::text",
            )
            constraints = dict(
                connection.execute(
                    """SELECT conname,pg_get_constraintdef(c.oid)
                    FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
                    WHERE n.nspname=%s AND conrelid='turn_digests'::regclass""",
                    (schema,),
                ).fetchall()
            )
            self.assertEqual(constraints["turn_digests_pkey"], "PRIMARY KEY (turn_id)")
            self.assertIn("ON DELETE CASCADE", constraints["turn_digests_turn_id_fkey"])
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM turn_digests WHERE turn_id IN (%s,%s)",
                    (missing, new_missing),
                ).fetchone()[0],
                0,
            )

        migration.migrate(schema, HEAD)
        database = PostgresDatabase(migration.checked_test_dsn(), schema=schema)
        journal = TurnJournal(database)
        try:
            self.assertEqual(journal.digest_for_turn(empty), "")
            self.assertEqual(
                journal.digest_for_turn(populated), "skeleton text\nnote text"
            )
            self.assertEqual(journal.digest_for_turn(new_digest), "new skeleton")
            self.assertEqual(journal.digest_for_turn(missing), "")
        finally:
            journal.close()
            database.close()

    def test_upgrade_dependency_failure_rolls_back_every_change(self) -> None:
        schema = self.schema()
        with migration.connect(schema) as connection:
            turn_id = insert_turn(connection, 1)
            connection.execute(
                "INSERT INTO turn_digests VALUES (%s, 'kept', '', 77)",
                (turn_id,),
            )
            connection.execute(
                "CREATE VIEW test_digest_dependency AS SELECT * FROM turn_digests"
            )

        with self.assertRaises(DBAPIError):
            migration.migrate(schema, HEAD)

        with migration.connect(schema) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM test_digest_dependency").fetchall(),
                [(turn_id, "kept", "", 77)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0],
                BASE,
            )
            columns = connection.execute(
                """SELECT column_name FROM information_schema.columns
                WHERE table_schema=%s AND table_name='agent_turns'
                  AND column_name LIKE 'digest_%%'""",
                (schema,),
            ).fetchall()
            self.assertEqual(columns, [])
