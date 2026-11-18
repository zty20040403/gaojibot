"""Add single-row scope_leases table and turn checkpoint columns for Stage 2.

Downgrade drops the new columns and scope_leases table cleanly.
"""
from __future__ import annotations

import os
import re

from alembic import op

revision = "0111_scope_leases_checkpoints"
down_revision = "0110_drop_remaining_dead_tables"
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
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scope_leases (
            scope_key TEXT PRIMARY KEY,
            lease_owner TEXT NOT NULL,
            lease_generation BIGINT NOT NULL DEFAULT 1,
            expires_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qq_official_turn_executions
            ADD COLUMN IF NOT EXISTS checkpoint_json TEXT,
            ADD COLUMN IF NOT EXISTS checkpoint_step INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS checkpoint_updated_at BIGINT
        """
    )
    op.execute(
        """
        ALTER TABLE agent_turns
            ADD COLUMN IF NOT EXISTS checkpoint_json TEXT,
            ADD COLUMN IF NOT EXISTS checkpoint_step INTEGER NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    _use_schema()
    op.execute(
        """
        ALTER TABLE agent_turns
            DROP COLUMN IF EXISTS checkpoint_step,
            DROP COLUMN IF EXISTS checkpoint_json
        """
    )
    op.execute(
        """
        ALTER TABLE qq_official_turn_executions
            DROP COLUMN IF EXISTS checkpoint_updated_at,
            DROP COLUMN IF EXISTS checkpoint_step,
            DROP COLUMN IF EXISTS checkpoint_json
        """
    )
    op.execute("DROP TABLE IF EXISTS scope_leases")
