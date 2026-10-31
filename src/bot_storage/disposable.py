"""Owned, local PostgreSQL workspaces for tests and offline evaluation data."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import threading

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from .database import PostgresDatabase
from .schema import HEAD_REVISION

_MIGRATION_LOCK = threading.Lock()
_ROOT = Path(__file__).resolve().parents[2]


def validate_test_database(dsn: str) -> None:
    if not dsn:
        raise RuntimeError(
            "Storage tests require TEST_POSTGRES_DSN for a local test database"
        )
    if any(os.environ.get(name) for name in ("PGHOSTADDR", "PGSERVICE")):
        raise RuntimeError(
            "Storage tests reject inherited PostgreSQL routing overrides"
        )
    values = conninfo_to_dict(dsn)
    if (
        values.get("host") not in {"127.0.0.1", "::1"}
        or values.get("hostaddr")
        or values.get("service")
        or not values.get("dbname", "").endswith(("_test", "_acceptance"))
    ):
        raise RuntimeError(
            "Storage tests require an explicit loopback _test/_acceptance database"
        )
    configured = os.environ.get("AI_POSTGRES_DSN", "").strip()
    if configured:

        def identity(item):
            host = item.get("host")
            if host in {"127.0.0.1", "::1", "localhost"}:
                host = "loopback"
            return host, item.get("port", "5432"), item.get("dbname")

        if identity(conninfo_to_dict(configured)) == identity(values):
            raise RuntimeError("The test database must differ from AI_POSTGRES_DSN")


@contextmanager
def _migration_environment(dsn, schema):
    with _MIGRATION_LOCK:
        changes = {"AI_POSTGRES_DSN": dsn, "AI_POSTGRES_SCHEMA": schema}
        previous = {key: os.environ.get(key) for key in changes}
        try:
            os.environ.update(changes)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class IsolatedPostgres:
    def __init__(self, dsn: str, *, revision: str = HEAD_REVISION) -> None:
        validate_test_database(dsn)
        with psycopg.connect(dsn) as connection:
            extension = connection.execute(
                "SELECT extnamespace::regnamespace::text FROM pg_extension "
                "WHERE extname='vector'"
            ).fetchone()
        if extension != ("public",):
            raise RuntimeError(
                "The dedicated test database requires pgvector in schema public "
                "before creating isolated workspaces; follow the CI database setup"
            )
        self.dsn = dsn
        self.schema = f"gaoji_test_{os.getpid()}_{secrets.token_hex(6)}"
        self.database: PostgresDatabase | None = None
        self._closed = False
        try:
            with _migration_environment(dsn, self.schema):
                config = Config()
                config.set_main_option("script_location", str(_ROOT / "migrations"))
                command.upgrade(config, revision)
            self.database = PostgresDatabase(
                dsn,
                schema=self.schema,
                min_size=1,
                max_size=8,
                application_name="gaoji-isolated-workspace",
            )
            self.database.require_revision(revision)
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup_error:
                error.add_note(
                    f"Workspace cleanup also failed: {type(cleanup_error).__name__}"
                )
            raise

    def _validate_owned(self):
        validate_test_database(self.dsn)
        if not re.fullmatch(r"gaoji_test_\d+_[0-9a-f]{12}", self.schema):
            raise RuntimeError("Refusing to modify a schema not owned by the workspace")

    def reset(self) -> None:
        if self._closed:
            raise RuntimeError("Test workspace is closed")
        self._validate_owned()
        with psycopg.connect(self.dsn) as connection:
            tables = connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s "
                "AND tablename != 'alembic_version'",
                (self.schema,),
            ).fetchall()
            if tables:
                connection.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                        sql.SQL(", ").join(
                            sql.Identifier(self.schema, row[0]) for row in tables
                        )
                    )
                )

    def close(self) -> None:
        if self._closed:
            return
        self._validate_owned()
        if self.database is not None:
            self.database.close()
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )
        self._closed = True
