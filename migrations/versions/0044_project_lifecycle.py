"""Durable project transitions and per-project physical-operation fences."""

import os
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0044_project_lifecycle"
down_revision = "0043_project_workspace"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    s = _schema()
    for name in ("workspace_generation", "lifecycle_fence"):
        op.add_column(
            "projects",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
            schema=s,
        )
    op.create_check_constraint(
        "ck_project_lifecycle_counters",
        "projects",
        "workspace_generation>=0 AND lifecycle_fence>=0",
        schema=s,
    )
    op.create_table(
        "project_lifecycle_runs",
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.durable_jobs.job_id"),
            primary_key=True,
        ),
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
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("source_state", sa.Text(), nullable=False),
        sa.Column("target_state", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("plan", JSONB(), nullable=False),
        sa.Column("next_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "project_id", "request_key", name="uq_project_lifecycle_request"
        ),
        sa.CheckConstraint(
            "action IN ('wake','stop','hibernate','archive') AND generation>0 AND next_step>=0",
            name="ck_project_lifecycle_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','needs_attention')",
            name="ck_project_lifecycle_status",
        ),
        schema=s,
    )
    op.create_index(
        "uq_project_lifecycle_active",
        "project_lifecycle_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status<>'completed'"),
        schema=s,
    )
    op.add_column(
        "projects",
        sa.Column(
            "lifecycle_job_id",
            sa.BigInteger(),
            sa.ForeignKey(
                f"{s}.project_lifecycle_runs.job_id", name="fk_project_lifecycle_job"
            ),
        ),
        schema=s,
    )
    op.create_table(
        "project_lifecycle_steps",
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.project_lifecycle_runs.job_id"),
            primary_key=True,
        ),
        sa.Column("step_index", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.projects.project_id"),
            nullable=False,
        ),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("dispatch_lease", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("arguments", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("project_id", "fence", name="uq_project_operation_fence"),
        sa.CheckConstraint(
            "fence>0 AND dispatch_lease>0 AND step_index>=0 AND request_id ~ '^[a-f0-9]{32}$'",
            name="ck_project_step_identity",
        ),
        sa.CheckConstraint(
            "status IN ('pending','succeeded')", name="ck_project_step_status"
        ),
        schema=s,
    )


def downgrade():
    s = _schema()
    connection = op.get_bind()
    if connection.execute(
        sa.text(f'SELECT EXISTS(SELECT 1 FROM "{s}".project_lifecycle_runs)')
    ).scalar():
        raise RuntimeError(
            "Export retained project lifecycle journals before downgrade"
        )
    op.drop_column("projects", "lifecycle_job_id", schema=s)
    op.drop_table("project_lifecycle_steps", schema=s)
    op.drop_table("project_lifecycle_runs", schema=s)
    op.drop_constraint("ck_project_lifecycle_counters", "projects", schema=s)
    for name in ("workspace_generation", "lifecycle_fence"):
        op.drop_column("projects", name, schema=s)
