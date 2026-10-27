"""Keep one execution deadline across scheduled-run retries."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0034_routine_deadlines"
down_revision = "0033_routine_effects"
branch_labels = None
depends_on = None


def _schema():
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    return schema


def upgrade():
    schema = _schema()
    op.add_column("routine_runs", sa.Column("deadline_at", sa.BigInteger()), schema=schema)
    # Legacy runs have no saved timeout; use the old minimum, never extend it.
    op.get_bind().execute(sa.text(f'''UPDATE "{schema}".routine_runs r
        SET deadline_at=(SELECT MIN(b.created_at)+30 FROM "{schema}".routine_budget_reservations b
                         WHERE b.run_id=r.run_id) WHERE r.attempt_id <> '' '''))


def downgrade():
    schema = _schema()
    if op.get_bind().execute(sa.text(f'''SELECT count(*) FROM "{schema}".routine_runs
            WHERE deadline_at IS NOT NULL AND status IN ('queued','running')''')).scalar():
        raise RuntimeError("Finish or cancel scheduled runs before removing their deadlines")
    op.drop_column("routine_runs", "deadline_at", schema=schema)
