"""Associate project mutations and recoverable saves with their durable turn."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0052_project_autosave"
down_revision = "0051_project_snapshot"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    s = _schema()
    op.add_column("project_io", sa.Column("journal_turn_id", sa.BigInteger()), schema=s)
    op.create_foreign_key(
        "fk_project_io_turn",
        "project_io",
        "agent_turns",
        ["journal_turn_id"],
        ["turn_id"],
        source_schema=s,
        referent_schema=s,
    )
    op.create_index(
        "ix_project_io_turn", "project_io", ["project_id", "journal_turn_id"], schema=s
    )
    op.create_table(
        "project_turn_saves",
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.projects.project_id"),
            primary_key=True,
        ),
        sa.Column(
            "turn_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.agent_turns.turn_id"),
            primary_key=True,
        ),
        sa.Column(
            "actor_principal_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.principals.principal_id"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.project_lifecycle_runs.job_id"),
            unique=True,
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "next_attempt_at", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("error_code", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','queued','skipped')", name="ck_project_turn_save_state"
        ),
        sa.CheckConstraint(
            "(state='queued')=(job_id IS NOT NULL)", name="ck_project_turn_save_job"
        ),
        schema=s,
    )
    op.create_index(
        "ix_project_turn_save_due",
        "project_turn_saves",
        ["next_attempt_at"],
        postgresql_where=sa.text("state='pending'"),
        schema=s,
    )


def downgrade():
    s = _schema()
    if (
        op.get_bind()
        .execute(
            sa.text(
                f'SELECT EXISTS(SELECT 1 FROM "{s}".project_turn_saves) OR '
                f'EXISTS(SELECT 1 FROM "{s}".project_io WHERE journal_turn_id IS NOT NULL)'
            )
        )
        .scalar()
    ):
        raise RuntimeError("Export project turn associations before downgrade")
    op.drop_table("project_turn_saves", schema=s)
    op.drop_index("ix_project_io_turn", table_name="project_io", schema=s)
    op.drop_constraint("fk_project_io_turn", "project_io", schema=s, type_="foreignkey")
    op.drop_column("project_io", "journal_turn_id", schema=s)
