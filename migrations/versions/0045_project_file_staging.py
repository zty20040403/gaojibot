"""Lease-independent file streaming and scoped pending snapshot references."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0045_project_file_staging"
down_revision = "0044_project_lifecycle"
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
        "project_snapshot_staging",
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.project_lifecycle_runs.job_id"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "sha256",
            sa.Text(),
            sa.ForeignKey(f"{s}.project_blobs.sha256"),
            nullable=False,
        ),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "byte_size>=0 AND byte_size<=268435456 AND mode IN (420,493) "
            "AND manifest_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_project_snapshot_staging",
        ),
        schema=s,
    )
    op.create_index(
        "ix_project_snapshot_staging_blob",
        "project_snapshot_staging",
        ["sha256"],
        schema=s,
    )


def downgrade():
    s = _schema()
    if (
        op.get_bind()
        .execute(
            sa.text(f'SELECT EXISTS(SELECT 1 FROM "{s}".project_snapshot_staging)')
        )
        .scalar()
    ):
        raise RuntimeError("Reconcile staged project snapshots before downgrade")
    op.drop_table("project_snapshot_staging", schema=s)
