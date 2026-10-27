"""Approval-bound conversation checkpoints and fenced continuation effects."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0042_mcp_continuations"
down_revision = "0041_mcp_calls"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    schema = _schema()
    op.add_column("durable_jobs", sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"), schema=schema)
    op.create_table("mcp_continuations",
        sa.Column("call_id", sa.Text(), sa.ForeignKey(f"{schema}.mcp_calls.call_id"), primary_key=True),
        sa.Column("checkpoint_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_hash", sa.Text(), nullable=False),
        sa.Column("transcript_json", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reply_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("pending_call_id", sa.Text(), sa.ForeignKey(f"{schema}.mcp_calls.call_id")),
        sa.Column("pending_checkpoint_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("final_body_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="waiting"),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.durable_jobs.job_id"), unique=True),
        sa.Column("delivery_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.deliveries.delivery_id"), unique=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("status IN ('waiting','queued','enqueued')", name="ck_mcp_continuation_status"),
        sa.CheckConstraint("revision>0 AND model_calls BETWEEN 0 AND 8", name="ck_mcp_continuation_budget"),
        sa.CheckConstraint("reserved_tokens BETWEEN 0 AND 262144", name="ck_mcp_continuation_tokens"),
        sa.CheckConstraint("(status='waiting' AND job_id IS NULL AND delivery_id IS NULL) OR "
                           "(status='queued' AND job_id IS NOT NULL AND delivery_id IS NULL) OR "
                           "(status='enqueued' AND job_id IS NOT NULL AND delivery_id IS NOT NULL)",
                           name="ck_mcp_continuation_queue"),
        schema=schema)
    op.create_index("ix_mcp_continuations_ready", "mcp_continuations", ["status", "created_at"], schema=schema)
    op.create_table("mcp_continuation_effects",
        sa.Column("call_id", sa.Text(), sa.ForeignKey(f"{schema}.mcp_continuations.call_id"), primary_key=True),
        sa.Column("tool_call_id", sa.Text(), primary_key=True),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments_hash", sa.Text(), nullable=False),
        sa.Column("idempotency", sa.Text(), nullable=False),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("status IN ('started','completed')", name="ck_mcp_continuation_effect_status"),
        sa.CheckConstraint("idempotency IN ('pure','idempotent','non-idempotent')",
                           name="ck_mcp_continuation_effect_idempotency"),
        schema=schema)


def downgrade():
    schema = _schema()
    op.drop_table("mcp_continuation_effects", schema=schema)
    op.drop_table("mcp_continuations", schema=schema)
    op.drop_column("durable_jobs", "lease_generation", schema=schema)
