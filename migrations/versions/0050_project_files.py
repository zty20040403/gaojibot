"""Bind project files to the durable transport queue and immutable objects."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0050_project_files"
down_revision = "0049_project_io"
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
        "project_file_deliveries",
        sa.Column(
            "delivery_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.deliveries.delivery_id"),
            primary_key=True,
        ),
        sa.Column(
            "operation_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.project_io.operation_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.projects.project_id"),
            nullable=False,
        ),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("blob_sha256", sa.Text(), sa.ForeignKey(f"{s}.project_blobs.sha256")),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("upload_started_at", sa.BigInteger()),
        sa.Column(
            "next_reconcile_at", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[a-f0-9]{64}$' AND (blob_sha256 IS NULL OR blob_sha256=sha256)",
            name="ck_project_file_digest",
        ),
        sa.CheckConstraint(
            "byte_size>0 AND byte_size<=268435456", name="ck_project_file_size"
        ),
        schema=s,
    )
    op.create_index(
        "ix_project_files_project", "project_file_deliveries", ["project_id"], schema=s
    )


def downgrade():
    s = _schema()
    if (
        op.get_bind()
        .execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM "{s}".project_file_deliveries)'))
        .scalar()
    ):
        raise RuntimeError("Export project delivery receipts before downgrade")
    op.drop_table("project_file_deliveries", schema=s)
