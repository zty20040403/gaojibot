"""Keep accepted project versions and publication receipts independent of tasks."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0056_project_publish"
down_revision = "0055_project_tasks"
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
        "project_task_publications",
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey(f"{s}.project_task_bindings.task_id"),
                  primary_key=True, autoincrement=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("acceptance_run_id", sa.BigInteger(), sa.ForeignKey(f"{s}.subagent_runs.run_id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), sa.ForeignKey(f"{s}.project_blobs.sha256"), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "version_no"],
                                [f"{s}.project_versions.project_id", f"{s}.project_versions.version_no"]),
        sa.CheckConstraint("revision>0 AND version_no>0 AND artifact_sha256 ~ '^[a-f0-9]{64}$' "
                           "AND manifest_sha256 ~ '^[a-f0-9]{64}$'", name="ck_project_task_publication"),
        schema=s,
    )


def downgrade():
    s = _schema()
    if op.get_bind().execute(sa.text(
        f'SELECT EXISTS(SELECT 1 FROM "{s}".project_task_publications)'
    )).scalar():
        raise RuntimeError("Export project task publications before downgrade")
    op.drop_table("project_task_publications", schema=s)
