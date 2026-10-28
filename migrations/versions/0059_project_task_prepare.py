"""Keep project save-to-task handoffs in the existing leased lifecycle journal."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0059_project_task_prepare"
down_revision = "0058_project_task_release"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def _constraint(schema, enabled):
    actions = "'wake','stop','hibernate','archive','snapshot','restore'"
    if enabled:
        actions += ",'task_prepare'"
    op.drop_constraint("ck_project_lifecycle_action", "project_lifecycle_runs", schema=schema)
    op.create_check_constraint("ck_project_lifecycle_action", "project_lifecycle_runs",
        f"action IN ({actions}) AND generation>0 AND next_step>=0", schema=schema)


def upgrade():
    _constraint(_schema(), True)


def downgrade():
    schema = _schema()
    if op.get_bind().execute(sa.text(
        f'SELECT EXISTS(SELECT 1 FROM "{schema}".project_lifecycle_runs WHERE action=\'task_prepare\')'
    )).scalar():
        raise RuntimeError("Export project task preparation journals before downgrade")
    _constraint(schema, False)
