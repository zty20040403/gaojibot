"""Keep scheduled tool receipts across worker takeover."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0033_routine_effects"
down_revision = "0032_agent_routines"
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
        "routine_effects",
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey(f"{schema}.routine_runs.run_id"),
            primary_key=True,
        ),
        sa.Column("fingerprint", sa.Text(), primary_key=True),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger()),
        sa.CheckConstraint(
            "state IN ('started','completed')", name="ck_routine_effect_state"
        ),
        schema=schema,
    )


def downgrade():
    schema = _schema()
    if (
        op.get_bind()
        .execute(sa.text(f'SELECT count(*) FROM "{schema}".routine_effects'))
        .scalar()
    ):
        raise RuntimeError(
            "Export routine effect receipts before removing their schema"
        )
    op.drop_table("routine_effects", schema=schema)
