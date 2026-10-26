"""Store scoped per-principal answer reactions independently of admin labels."""
from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0030_turn_reaction_votes"
down_revision = "0029_native_ssh_operations"
branch_labels = None
depends_on = None


def schema_name():
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    return schema


def upgrade():
    schema = schema_name()
    op.create_table("turn_reaction_votes",
        sa.Column("turn_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.agent_turns.turn_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("reactor_principal_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.principals.principal_id"), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.messages.canonical_message_id"), nullable=False),
        sa.Column("is_asker", sa.Integer(), nullable=False),
        sa.Column("vote", sa.Integer(), nullable=False),
        sa.Column("emoji_id", sa.Text(), nullable=False),
        sa.Column("source_time", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("vote IN (-1, 0, 1)", name="ck_reaction_vote"),
        sa.CheckConstraint("is_asker IN (0, 1)", name="ck_reaction_asker"), schema=schema)
    op.create_index("idx_turn_reaction_votes_updated", "turn_reaction_votes", ["updated_at", "turn_id"], schema=schema)


def downgrade():
    schema = schema_name()
    if op.get_bind().execute(sa.text(f'SELECT count(*) FROM "{schema}".turn_reaction_votes')).scalar():
        raise RuntimeError("Export reaction feedback before removing its schema")
    op.drop_table("turn_reaction_votes", schema=schema)
