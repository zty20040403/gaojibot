"""Keep accounting failures distinct from the last measured project usage."""

import os
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0046_project_usage"
down_revision = "0045_project_file_staging"
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
        "project_usage",
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.projects.project_id"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("checked_at", sa.BigInteger(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("sample", JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint(
            "generation>=0 AND fence>=0 AND checked_at>=0",
            name="ck_project_usage_identity",
        ),
        schema=s,
    )


def downgrade():
    op.drop_table("project_usage", schema=_schema())
