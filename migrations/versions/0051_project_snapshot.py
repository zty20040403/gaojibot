"""Retained project snapshots without changing the workspace's lifecycle state."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0051_project_snapshot"
down_revision = "0050_project_files"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def _constraint(schema, *, snapshot):
    actions = "'wake','stop','hibernate','archive'" + (",'snapshot'" if snapshot else "")
    op.drop_constraint("ck_project_lifecycle_action", "project_lifecycle_runs", schema=schema)
    op.create_check_constraint(
        "ck_project_lifecycle_action", "project_lifecycle_runs",
        f"action IN ({actions}) AND generation>0 AND next_step>=0", schema=schema,
    )


def upgrade():
    s = _schema()
    _constraint(s, snapshot=True)
    op.create_index(
        "ix_project_lifecycle_request", "project_lifecycle_runs", ["request_key"], schema=s,
    )


def downgrade():
    s = _schema()
    if op.get_bind().execute(sa.text(
        f"SELECT EXISTS(SELECT 1 FROM \"{s}\".project_lifecycle_runs WHERE action='snapshot')"
    )).scalar():
        raise RuntimeError("Export project snapshot journals before downgrade")
    op.drop_index("ix_project_lifecycle_request", table_name="project_lifecycle_runs", schema=s)
    _constraint(s, snapshot=False)
