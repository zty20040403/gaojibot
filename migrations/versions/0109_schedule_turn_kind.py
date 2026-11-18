"""Allow schedules.kind = 'turn' for scheduled full main-model turns (group daily digest / periodic checks)."""
from __future__ import annotations

import os
import re

from alembic import op

revision = "0109_schedule_turn_kind"
down_revision = "0108_proactive_wake_state"
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
    op.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_kind_check")
    op.execute("ALTER TABLE schedules ADD CONSTRAINT schedules_kind_check CHECK (kind IN ('reminder', 'routine', 'turn'))")


def downgrade() -> None:
    _use_schema()
    if op.get_bind().exec_driver_sql("SELECT 1 FROM schedules WHERE kind = 'turn' LIMIT 1").first():
        raise RuntimeError("schedule_turn_rows_require_export_before_downgrade")
    op.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_kind_check")
    op.execute("ALTER TABLE schedules ADD CONSTRAINT schedules_kind_check CHECK (kind IN ('reminder', 'routine'))")
