"""Isolated PostgreSQL fixtures with class and application lifetimes."""

from __future__ import annotations

import os
import unittest

from src.bot_storage import PostgresDatabase
from src.bot_storage.disposable import IsolatedPostgres as _SchemaFixture
from src.bot_storage.disposable import validate_test_database as validate_test_database


def isolated_postgres_for_test(case: unittest.TestCase) -> PostgresDatabase:
    """Own a separate schema for one app, including multiple apps in one test."""
    fixture = _SchemaFixture(os.environ.get("TEST_POSTGRES_DSN", "").strip())
    case.addCleanup(fixture.close)
    assert fixture.database is not None
    return fixture.database


def postgres_for_test(
    case: unittest.TestCase, *, reset: bool = False
) -> PostgresDatabase:
    """Reuse the class schema; reset once per test, or explicitly per subcase.

    Multiple stores/reopened stores in one test share real PostgreSQL constraints
    and data. Independent test classes own distinct schemas and connection pools.
    """
    cls = type(case)
    fixture = cls.__dict__.get("_owned_postgres_fixture")
    if fixture is None:
        fixture = _SchemaFixture(os.environ.get("TEST_POSTGRES_DSN", "").strip())
        setattr(cls, "_owned_postgres_fixture", fixture)

        def close() -> None:
            try:
                fixture.close()
            finally:
                delattr(cls, "_owned_postgres_fixture")

        cls.addClassCleanup(close)
    if reset or not getattr(case, "_postgres_fixture_ready", False):
        fixture.reset()
        case._postgres_fixture_ready = True
    assert fixture.database is not None
    return fixture.database
