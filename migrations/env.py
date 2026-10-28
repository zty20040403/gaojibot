from __future__ import annotations

import os
import re
from logging.config import fileConfig

import psycopg
from alembic import context
from sqlalchemy import create_engine, pool, text


config = context.config
if config.config_file_name is not None:
    # Upgrades also run in a live controller/test process. Keep its worker and
    # security loggers enabled after Alembic has configured its own output.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def _schema() -> str:
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("AI_POSTGRES_SCHEMA is not a valid identifier")
    return value


def _dsn() -> str:
    dsn = os.getenv("AI_POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("AI_POSTGRES_DSN is required for Alembic")
    return dsn


def run_migrations_offline() -> None:
    schema = _schema()
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    schema = _schema()
    dsn = _dsn()
    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(dsn),
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.commit()
        connection.execute(text(f'SET search_path TO "{schema}", public'))
        # SET starts SQLAlchemy's implicit transaction. Commit it so Alembic can
        # own and commit the migration transaction that follows.
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=schema,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
