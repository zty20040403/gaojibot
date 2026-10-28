"""Bind accepted task versions to one recoverable physical restore."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0057_project_materialize"
down_revision = "0056_project_publish"
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
        "project_task_materializations",
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey(f"{s}.project_task_publications.task_id"),
                  primary_key=True, autoincrement=False),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey(f"{s}.project_lifecycle_runs.job_id"), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.BigInteger()),
        sa.CheckConstraint("(status='pending' AND completed_at IS NULL) OR "
                           "(status='completed' AND completed_at IS NOT NULL)", name="ck_project_materialization_state"),
        schema=s,
    )


def downgrade():
    s = _schema()
    if op.get_bind().execute(sa.text(
        f'SELECT EXISTS(SELECT 1 FROM "{s}".project_task_materializations)'
    )).scalar():
        raise RuntimeError("Export project task materializations before downgrade")
    op.drop_table("project_task_materializations", schema=s)
