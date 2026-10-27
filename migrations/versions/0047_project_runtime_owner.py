"""Bind retained projects to their original bot account across process restarts."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0047_project_runtime_owner"
down_revision = "0046_project_usage"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    # Existing unbound projects require an explicit operator migration. Choosing
    # whichever bot happens to be online would silently change their identity.
    op.add_column(
        "projects",
        sa.Column("bot_native_user_id", sa.Text(), nullable=False, server_default=""),
        schema=_schema(),
    )


def downgrade():
    s = _schema()
    if (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT EXISTS(SELECT 1 FROM \"{s}\".projects WHERE bot_native_user_id<>'')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Export project account bindings before downgrade")
    op.drop_column("projects", "bot_native_user_id", schema=s)
