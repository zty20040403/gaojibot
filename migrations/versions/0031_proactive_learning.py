"""Persist group-scoped proactive experiments and once-only feedback settlement."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0031_proactive_learning"
down_revision = "0030_turn_reaction_votes"
branch_labels = None
depends_on = None


def _schema():
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    return schema


def upgrade():
    schema = _schema()
    op.create_table(
        "proactive_trials",
        sa.Column("trial_id", sa.Text(), primary_key=True),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Text(), nullable=False),
        sa.Column("trigger_message_id", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("interest", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("native_message_id", sa.Text()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", sa.BigInteger()),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("settled_at", sa.BigInteger()),
        sa.Column("reward", sa.Float()),
        sa.Column("weight", sa.Float()),
        sa.Column("outcome", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint(
            "scope_key", "bot_id", "trigger_message_id", name="uq_proactive_trigger"
        ),
        sa.UniqueConstraint(
            "scope_key", "bot_id", "native_message_id", name="uq_proactive_sent"
        ),
        sa.CheckConstraint("threshold IN (90, 94, 98)", name="ck_proactive_threshold"),
        sa.CheckConstraint(
            "interest BETWEEN threshold AND 100", name="ck_proactive_interest"
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','unknown','failed','settled')",
            name="ck_proactive_status",
        ),
        sa.CheckConstraint(
            "reward BETWEEN 0 AND 1 AND weight BETWEEN 0 AND 1",
            name="ck_proactive_reward",
        ),
        schema=schema,
    )
    op.create_index(
        "idx_proactive_due", "proactive_trials", ["status", "expires_at"], schema=schema
    )
    op.create_index(
        "idx_proactive_scope",
        "proactive_trials",
        ["scope_key", "bot_id", "sent_at"],
        schema=schema,
    )
    op.create_table(
        "proactive_feedback",
        sa.Column(
            "trial_id",
            sa.Text(),
            sa.ForeignKey(f"{schema}.proactive_trials.trial_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("actor_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('reaction','follow_up','rejected','recalled')",
            name="ck_proactive_signal",
        ),
        sa.CheckConstraint("value IN (-1,0,1)", name="ck_proactive_vote"),
        schema=schema,
    )
    op.create_table(
        "proactive_arms",
        sa.Column("scope_key", sa.Text(), primary_key=True),
        sa.Column("bot_id", sa.Text(), primary_key=True),
        sa.Column("threshold", sa.Integer(), primary_key=True),
        sa.Column("successes", sa.Float(), nullable=False),
        sa.Column("failures", sa.Float(), nullable=False),
        sa.Column("observations", sa.Integer(), nullable=False),
        sa.CheckConstraint("threshold IN (90,94,98)", name="ck_proactive_arm"),
        sa.CheckConstraint(
            "successes >= 0 AND failures >= 0 AND observations >= 0",
            name="ck_proactive_counts",
        ),
        schema=schema,
    )


def downgrade():
    schema = _schema()
    if (
        op.get_bind()
        .execute(sa.text(f'SELECT count(*) FROM "{schema}".proactive_trials'))
        .scalar()
    ):
        raise RuntimeError(
            "Export proactive learning records before removing their schema"
        )
    for table in ("proactive_feedback", "proactive_arms", "proactive_trials"):
        op.drop_table(table, schema=schema)
