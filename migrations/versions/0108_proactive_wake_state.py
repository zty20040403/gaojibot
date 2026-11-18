"""Allow proactive observations to enter queued state when waking a main-model turn without a direct delivery_plan."""
from __future__ import annotations

import os
import re

from alembic import op

revision = "0108_proactive_wake_state"
down_revision = "0107_drop_dead_tables"
branch_labels = None
depends_on = None


def _use_schema() -> None:
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f"SET LOCAL search_path TO {quoted}, public")


def upgrade() -> None:
    _use_schema()
    op.execute("ALTER TABLE qq_official_proactive_observations DROP CONSTRAINT qq_official_proactive_observations_check1")
    op.execute(
        "ALTER TABLE qq_official_proactive_observations "
        "ADD CONSTRAINT qq_official_proactive_observations_check1 CHECK (plan_id IS NULL OR state = 'queued')"
    )


def downgrade() -> None:
    _use_schema()
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM qq_official_proactive_observations WHERE state = 'queued' AND plan_id IS NULL LIMIT 1"
    ).first():
        raise RuntimeError("proactive_wake_observations_require_export_before_downgrade")
    op.execute("ALTER TABLE qq_official_proactive_observations DROP CONSTRAINT qq_official_proactive_observations_check1")
    op.execute(
        "ALTER TABLE qq_official_proactive_observations "
        "ADD CONSTRAINT qq_official_proactive_observations_check1 CHECK ((state = 'queued') = (plan_id IS NOT NULL))"
    )
