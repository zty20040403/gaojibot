"""Conversation writer leases and resumable turn state."""
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
    op.execute('''CREATE TABLE scope_leases (
        scope_key TEXT PRIMARY KEY, lease_owner TEXT,
        lease_generation BIGINT NOT NULL DEFAULT 0, expires_at BIGINT NOT NULL DEFAULT 0
    )''')
    op.execute('ALTER TABLE qq_official_turn_executions ADD COLUMN checkpoint_json TEXT')
    op.execute('ALTER TABLE agent_turns ADD COLUMN checkpoint_json TEXT')


def downgrade() -> None:
    _use_schema()
    op.execute('ALTER TABLE agent_turns DROP COLUMN checkpoint_json')
    op.execute('ALTER TABLE qq_official_turn_executions DROP COLUMN checkpoint_json')
    op.execute('DROP TABLE scope_leases')
