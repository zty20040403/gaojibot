"""Persist project task admission independently of mutable conversation focus."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0055_project_tasks"
down_revision = "0054_project_browser"
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
        "project_task_bindings",
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey(f"{s}.subagent_tasks.task_id"),
                  primary_key=True, autoincrement=False),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey(f"{s}.projects.project_id"), nullable=False),
        sa.Column("actor_principal_id", sa.BigInteger(), sa.ForeignKey(f"{s}.principals.principal_id"), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("intent_sha256", sa.Text(), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), sa.ForeignKey(f"{s}.project_blobs.sha256"), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "base_version"],
                                [f"{s}.project_versions.project_id", f"{s}.project_versions.version_no"]),
        sa.UniqueConstraint("project_id", "actor_principal_id", "request_key", name="uq_project_task_request"),
        sa.CheckConstraint("request_key ~ '^[a-f0-9]{64}$' AND intent_sha256 ~ '^[a-f0-9]{64}$' "
                           "AND manifest_sha256 ~ '^[a-f0-9]{64}$' AND base_version>0 "
                           "AND generation>=0 AND fence>=0", name="ck_project_task_binding"),
        schema=s,
    )
    op.create_index("ix_project_task_request", "project_task_bindings", ["actor_principal_id", "request_key"], schema=s)


def downgrade():
    s = _schema()
    if op.get_bind().execute(sa.text(
        f'SELECT EXISTS(SELECT 1 FROM "{s}".project_task_bindings)'
    )).scalar():
        raise RuntimeError("Export project task bindings before downgrade")
    op.drop_table("project_task_bindings", schema=s)
