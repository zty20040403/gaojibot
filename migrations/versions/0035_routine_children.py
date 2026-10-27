"""Bind scheduled occurrences to recoverable, parent-owned Sub-Agent tasks."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0035_routine_children"
down_revision = "0034_routine_deadlines"
branch_labels = None
depends_on = None


def _schema():
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    return schema


def upgrade():
    schema = _schema()
    op.create_table(
        "routine_children",
        sa.Column("run_id", sa.Text(), sa.ForeignKey(f"{schema}.routine_runs.run_id"), primary_key=True),
        sa.Column("fingerprint", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.subagent_tasks.task_id"), nullable=False),
        sa.Column("intent_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_routine_child_task"),
        schema=schema,
    )


def downgrade():
    schema = _schema()
    if op.get_bind().execute(sa.text(f'SELECT count(*) FROM "{schema}".routine_children')).scalar():
        raise RuntimeError("Routine child ownership must not be discarded")
    op.drop_table("routine_children", schema=schema)
