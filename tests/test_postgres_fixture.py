from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.postgres_fixture import postgres_for_test, validate_test_database


class PostgresFixtureSafetyTests(unittest.TestCase):
    def test_rejects_missing_remote_non_test_and_alternate_routes(self):
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": ""}):
            for value in (
                "",
                "dbname=gaoji_test",
                "host=localhost dbname=gaoji_test",
                "host=100.64.0.1 dbname=gaoji_test",
                "host=127.0.0.1 dbname=production",
                "host=127.0.0.1 hostaddr=100.64.0.1 dbname=gaoji_test",
                "host=127.0.0.1 service=prod dbname=gaoji_test",
            ):
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    validate_test_database(value)

    def test_rejects_same_bot_database_even_with_different_credentials(self):
        with (
            patch.dict(
                os.environ, {"AI_POSTGRES_DSN": "postgresql://bot@127.0.0.1/gaoji_test"}
            ),
            self.assertRaisesRegex(RuntimeError, "differ"),
        ):
            validate_test_database(
                "host=127.0.0.1 port=5432 user=tests dbname=gaoji_test"
            )

    def test_accepts_dedicated_loopback_test_database(self):
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": ""}):
            validate_test_database("host=127.0.0.1 dbname=gaoji_test")

    def test_rejects_inherited_alternate_routes(self):
        for name in ("PGHOSTADDR", "PGSERVICE"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "override"}):
                with self.assertRaisesRegex(RuntimeError, "routing overrides"):
                    validate_test_database("host=127.0.0.1 dbname=gaoji_test")

    def test_loopback_alias_cannot_bypass_bot_database_guard(self):
        for host in ("localhost", "::1"):
            with (
                self.subTest(host=host),
                patch.dict(
                    os.environ,
                    {
                        "AI_POSTGRES_DSN": f"host={host} dbname=gaoji_test",
                    },
                ),
                self.assertRaisesRegex(RuntimeError, "differ"),
            ):
                validate_test_database("host=127.0.0.1 dbname=gaoji_test")


class PostgresFixtureLifecycleTests(unittest.TestCase):
    def test_fixture_reuses_schema_without_reinitializing_store_data(self):
        database = postgres_for_test(self)
        connection = database.store_connection()
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO state_blobs VALUES ('fixture', '{}', 1)")
        self.assertIs(postgres_for_test(self), database)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM state_blobs").fetchone()[0], 1
        )
        self.assertIs(postgres_for_test(self, reset=True), database)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM state_blobs").fetchone()[0], 0
        )
