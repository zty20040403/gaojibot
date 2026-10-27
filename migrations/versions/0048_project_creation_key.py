"""Keep project creation idempotent across repeated chat delivery and recovery."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0048_project_creation_key"
down_revision = "0047_project_runtime_owner"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    s = _schema()
    op.add_column("projects", sa.Column("creation_key", sa.Text()), schema=s)
    op.create_index(
        "project_creation_identity",
        "projects",
        ["scope_key", "bot_native_user_id", "creator_principal_id", "creation_key"],
        unique=True,
        schema=s,
        postgresql_where=sa.text("creation_key IS NOT NULL"),
    )


def downgrade():
    s = _schema()
    if (
        op.get_bind()
        .execute(
            sa.text(
                f'SELECT EXISTS(SELECT 1 FROM "{s}".projects WHERE creation_key IS NOT NULL)'
            )
        )
        .scalar()
    ):
        raise RuntimeError("Export project creation identities before downgrade")
    op.drop_index("project_creation_identity", table_name="projects", schema=s)
    op.drop_column("projects", "creation_key", schema=s)
