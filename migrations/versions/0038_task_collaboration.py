"""Scoped task collaborators and revision-bound feedback."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0038_task_collaboration"
down_revision = "0037_group_wiki"
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
        "subagent_collaborators",
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.subagent_tasks.task_id"), primary_key=True),
        sa.Column("principal_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.principals.principal_id"), primary_key=True),
        sa.Column("added_by", sa.BigInteger(), sa.ForeignKey(f"{schema}.principals.principal_id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("grant_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        schema=schema,
    )
    op.create_table(
        "subagent_revision_feedback",
        sa.Column("feedback_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.subagent_tasks.task_id"), nullable=False),
        sa.Column("principal_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.principals.principal_id"), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("consumed_revision", sa.Integer()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("task_id", "principal_id", "request_key", name="uq_subagent_feedback_request"),
        sa.CheckConstraint("status IN ('pending','consumed','dismissed')", name="ck_subagent_feedback_status"),
        sa.CheckConstraint("base_revision>0 AND length(instruction) BETWEEN 1 AND 1000", name="ck_subagent_feedback_input"),
        sa.CheckConstraint("(status='consumed' AND consumed_revision IS NOT NULL AND consumed_revision>base_revision) OR "
                           "(status<>'consumed' AND consumed_revision IS NULL)", name="ck_subagent_feedback_revision"),
        schema=schema,
    )
    op.create_index("ix_subagent_feedback_pending", "subagent_revision_feedback",
                    ["task_id", "status", "feedback_id"], schema=schema)


def downgrade():
    op.drop_table("subagent_revision_feedback", schema=_schema())
    op.drop_table("subagent_collaborators", schema=_schema())
