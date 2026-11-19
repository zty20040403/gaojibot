"""0117 conversation history-floor migration coverage on an owned test schema."""

import unittest

import psycopg
from sqlalchemy.exc import DBAPIError

from tests import test_qq_official_outbox_migration as migration

BASE = "0116_inline_turn_digests"
HEAD = "0117_conversation_history_floor"


def insert_conversation(connection, suffix: str) -> int:
    return connection.execute(
        """INSERT INTO conversations
        (scope_key,platform,kind,native_conversation_id,created_at,updated_at)
        VALUES (%s,'onebot-v11','group',%s,1,2) RETURNING conversation_id""",
        (f"scope:onebot-v11:group:TEST_{suffix}", f"TEST_{suffix}"),
    ).fetchone()[0]


class ConversationVisibilityMigrationTests(unittest.TestCase):
    def schema(self) -> str:
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, BASE)
        return schema

    def test_round_trip_preserves_rows_absence_values_and_catalog(self) -> None:
        schema = self.schema()
        with migration.connect(schema) as connection:
            missing = insert_conversation(connection, "MISSING")
            initial = insert_conversation(connection, "INITIAL")
            cleared = insert_conversation(connection, "CLEARED")
            connection.execute(
                "INSERT INTO conversation_visibility VALUES (%s,1,NULL)",
                (initial,),
            )
            connection.execute(
                "INSERT INTO conversation_visibility VALUES (%s,42,987654321)",
                (cleared,),
            )

        migration.migrate(schema, HEAD)
        with migration.connect(schema) as connection:
            self.assertIsNone(
                connection.execute("SELECT to_regclass('conversation_visibility')").fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    """SELECT conversation_id,min_canonical_message_id,history_cleared_at
                    FROM conversations WHERE conversation_id=ANY(%s) ORDER BY conversation_id""",
                    ([missing, initial, cleared],),
                ).fetchall(),
                [(missing, None, None), (initial, 1, None), (cleared, 42, 987654321)],
            )
            columns = connection.execute(
                """SELECT column_name,is_nullable,column_default
                FROM information_schema.columns WHERE table_schema=%s
                  AND table_name='conversations'
                  AND column_name IN ('min_canonical_message_id','history_cleared_at')
                ORDER BY column_name""",
                (schema,),
            ).fetchall()
            self.assertEqual(
                columns,
                [("history_cleared_at", "YES", None), ("min_canonical_message_id", "YES", None)],
            )
            with self.assertRaises(psycopg.errors.CheckViolation), connection.transaction():
                connection.execute('UPDATE conversations SET history_cleared_at=1 WHERE conversation_id=%s', (missing,))
            new_missing = insert_conversation(connection, "NEW_MISSING")
            new_visible = insert_conversation(connection, "NEW_VISIBLE")
            connection.execute(
                """UPDATE conversations SET min_canonical_message_id=7,
                history_cleared_at=123 WHERE conversation_id=%s""",
                (new_visible,),
            )

        migration.migrate(schema, BASE, downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT conversation_id,min_canonical_message_id,cleared_at
                    FROM conversation_visibility ORDER BY conversation_id"""
                ).fetchall(),
                [(initial, 1, None), (cleared, 42, 987654321), (new_visible, 7, 123)],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT column_default,is_nullable FROM information_schema.columns
                    WHERE table_schema=%s AND table_name='conversation_visibility'
                      AND column_name='min_canonical_message_id'""",
                    (schema,),
                ).fetchone(),
                ("1", "NO"),
            )
            constraints = dict(
                connection.execute(
                    """SELECT conname,pg_get_constraintdef(c.oid)
                    FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
                    WHERE n.nspname=%s AND conrelid='conversation_visibility'::regclass""",
                    (schema,),
                ).fetchall()
            )
            self.assertEqual(
                constraints["conversation_visibility_pkey"],
                "PRIMARY KEY (conversation_id)",
            )
            self.assertNotIn(
                "ON DELETE CASCADE",
                constraints["conversation_visibility_conversation_id_fkey"],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM conversation_visibility WHERE conversation_id=ANY(%s)",
                    ([missing, new_missing],),
                ).fetchone()[0],
                0,
            )

    def test_restrict_dependency_failure_rolls_back_all_changes(self) -> None:
        schema = self.schema()
        with migration.connect(schema) as connection:
            conversation = insert_conversation(connection, "DEPENDENCY")
            connection.execute(
                "INSERT INTO conversation_visibility VALUES (%s,9,10)",
                (conversation,),
            )
            connection.execute(
                "CREATE VIEW test_visibility_dependency AS SELECT * FROM conversation_visibility"
            )

        with self.assertRaises(DBAPIError):
            migration.migrate(schema, HEAD)

        with migration.connect(schema) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM test_visibility_dependency").fetchall(),
                [(conversation, 9, 10)],
            )
            self.assertEqual(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
                BASE,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT column_name FROM information_schema.columns
                    WHERE table_schema=%s AND table_name='conversations'
                      AND column_name IN ('min_canonical_message_id','history_cleared_at')""",
                    (schema,),
                ).fetchall(),
                [],
            )


if __name__ == "__main__":
    unittest.main()
