"""Durable project rollback with preserved history and physical verification."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0053_project_restore"
down_revision = "0052_project_autosave"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def _constraint(schema, restore):
    actions = "'wake','stop','hibernate','archive','snapshot'" + (
        ",'restore'" if restore else ""
    )
    op.drop_constraint(
        "ck_project_lifecycle_action", "project_lifecycle_runs", schema=schema
    )
    op.create_check_constraint(
        "ck_project_lifecycle_action",
        "project_lifecycle_runs",
        f"action IN ({actions}) AND generation>0 AND next_step>=0",
        schema=schema,
    )


def upgrade():
    _constraint(_schema(), True)


def downgrade():
    schema = _schema()
    if (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT EXISTS(SELECT 1 FROM \"{schema}\".project_lifecycle_runs WHERE action='restore')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Export project restore journals before downgrade")
    _constraint(schema, False)
