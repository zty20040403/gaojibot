"""Persist successful scheduled turns that intentionally have no final message."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0036_routine_quiet_completion"
down_revision = "0035_routine_children"
branch_labels = None
depends_on = None


def _schema():
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    return schema


def _constraint(schema: str, *, quiet: bool):
    states = "'queued','running','generated','enqueued','failed','cancelled','skipped'"
    if quiet:
        states += ",'quiet'"
    op.drop_constraint("ck_routine_run_status", "routine_runs", schema=schema, type_="check")
    op.create_check_constraint("ck_routine_run_status", "routine_runs", f"status IN ({states})", schema=schema)


def upgrade():
    _constraint(_schema(), quiet=True)


def downgrade():
    schema = _schema()
    if op.get_bind().execute(sa.text(f'SELECT count(*) FROM "{schema}".routine_runs WHERE status=\'quiet\'')).scalar():
        raise RuntimeError("Quiet completions must not be discarded or re-executed")
    _constraint(schema, quiet=False)
