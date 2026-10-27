"""Keep bot persistence on the same backend and schema in tests and production."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from src.bot_security.store import SecurityStore
from src.bot_storage.database import DatabaseError, open_store_connection


ROOT = Path(__file__).resolve().parents[1]


class PostgresOnlyArchitectureTests(unittest.TestCase):
    def test_stores_reject_legacy_paths_without_opening_sqlite(self):
        with patch("sqlite3.connect", side_effect=AssertionError("SQLite opened")):
            for source in (":memory:", Path("unused.sqlite3")):
                with self.subTest(source=str(source)):
                    with self.assertRaisesRegex(DatabaseError, "PostgreSQL"):
                        open_store_connection(source)
                    with self.assertRaisesRegex(DatabaseError, "PostgreSQL"):
                        SecurityStore(source, b"test-only-secret" * 3)

    def test_runtime_stores_cannot_reintroduce_sqlite_or_schema_creation(self):
        paths = list((ROOT / "src/plugins/ai_chat").rglob("*.py"))
        paths += [
            ROOT / "src/bot_security/store.py",
            ROOT / "src/bot_storage/database.py",
        ]
        for path in paths:
            source = path.read_text()
            tree = ast.parse(source)
            with self.subTest(path=str(path.relative_to(ROOT))):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertNotIn("sqlite3", {item.name for item in node.names})
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, "sqlite3")
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertIsNone(
                            re.search(r"\b(?:CREATE|ALTER)\s+TABLE\b", node.value, re.I)
                        )
                    if isinstance(node, ast.Attribute):
                        self.assertNotEqual(node.attr, "_legacy_sqlite")

    def test_only_legacy_import_tests_use_sqlite_as_bot_storage(self):
        allowed = {"test_postgres_storage.py", "test_postgres_integration.py"}
        for path in (ROOT / "tests").glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    item.name == "sqlite3" for item in node.names
                ):
                    self.assertIn(path.name, allowed)
                if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                    self.assertIn(path.name, allowed)


if __name__ == "__main__":
    unittest.main()
