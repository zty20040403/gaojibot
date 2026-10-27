"""Persist project tool admission across process loss and lifecycle races."""

import os
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0049_project_io"
down_revision = "0048_project_creation_key"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    s = _schema()
    op.create_table(
        "project_io",
        sa.Column("operation_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "actor_principal_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.principals.principal_id"),
            nullable=False,
        ),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments_sha256", sa.Text(), nullable=False),
        sa.Column("sandbox_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("mutating", sa.Boolean(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "usage_accounted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default="reserved"),
        sa.Column("result", JSONB()),
        sa.Column("evidence", JSONB()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("project_id", "request_key", name="uq_project_io_request"),
        sa.CheckConstraint(
            "request_key ~ '^[a-f0-9]{64}$' AND arguments_sha256 ~ '^[a-f0-9]{64}$' AND token ~ '^[a-f0-9]{32}$'",
            name="ck_project_io_keys",
        ),
        sa.CheckConstraint(
            "generation>0 AND fence>=0 AND reserved_bytes>=0",
            name="ck_project_io_numbers",
        ),
        sa.CheckConstraint(
            "state IN ('reserved','dispatched','needs_attention','completed','aborted')",
            name="ck_project_io_state",
        ),
        sa.CheckConstraint(
            "state<>'completed' OR (result IS NOT NULL AND evidence IS NOT NULL)",
            name="ck_project_io_receipt",
        ),
        schema=s,
    )
    op.create_index(
        "uq_project_io_active",
        "project_io",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('reserved','dispatched','needs_attention')"
        ),
        schema=s,
    )


def downgrade():
    s = _schema()
    if (
        op.get_bind()
        .execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM "{s}".project_io)'))
        .scalar()
    ):
        raise RuntimeError("Export project operation receipts before downgrade")
    op.drop_table("project_io", schema=s)
