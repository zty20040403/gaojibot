"""Retain explicit failed-task detachment receipts without deleting artifacts."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0058_project_task_release"
down_revision = "0057_project_materialize"
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
        "project_task_releases",
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey(f"{s}.project_task_bindings.task_id"),
                  primary_key=True, autoincrement=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("task_status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("revision>0 AND task_status IN ('failed','cancelled')", name="ck_project_task_release"),
        schema=s,
    )


def downgrade():
    s = _schema()
    if op.get_bind().execute(sa.text(
        f'SELECT EXISTS(SELECT 1 FROM "{s}".project_task_releases)'
    )).scalar():
        raise RuntimeError("Export project task releases before downgrade")
    op.drop_table("project_task_releases", schema=s)
