"""Durable cron definitions, fenced executions and per-routine budget reservations."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0032_agent_routines"
down_revision = "0031_proactive_learning"
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
        "agent_routines",
        sa.Column("routine_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Text(), nullable=False),
        sa.Column("creator_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("daily_tokens", sa.Integer(), nullable=False),
        sa.Column("daily_runs", sa.Integer(), nullable=False),
        sa.Column("next_due_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "scope_key", "bot_id", "creator_id", "name", name="uq_routine_owner_name"
        ),
        sa.CheckConstraint(
            "platform='onebot-v11' AND kind IN ('group','private')",
            name="ck_routine_scope",
        ),
        sa.CheckConstraint(
            "version>0 AND max_tokens BETWEEN 1024 AND 200000 AND daily_tokens BETWEEN max_tokens AND 2000000 AND daily_runs BETWEEN 1 AND 24",
            name="ck_routine_budget",
        ),
        schema=schema,
    )
    op.create_index(
        "idx_routine_due",
        "agent_routines",
        ["next_due_at"],
        schema=schema,
        postgresql_where=sa.text("enabled"),
    )
    op.create_table(
        "routine_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column(
            "routine_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{schema}.agent_routines.routine_id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.BigInteger(), nullable=False),
        sa.Column("missed_from", sa.BigInteger(), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.durable_jobs.job_id")
        ),
        sa.Column(
            "turn_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.agent_turns.turn_id")
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_json", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "delivery_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{schema}.deliveries.delivery_id"),
        ),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "routine_id", "version", "scheduled_at", name="uq_routine_slot"
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','generated','enqueued','failed','cancelled','skipped')",
            name="ck_routine_run_status",
        ),
        schema=schema,
    )
    op.create_index(
        "idx_routine_active_run",
        "routine_runs",
        ["routine_id"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("status IN ('queued','running','generated')"),
    )
    op.create_table(
        "routine_budget_reservations",
        sa.Column("attempt_id", sa.Text(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey(f"{schema}.routine_runs.run_id"),
            nullable=False,
        ),
        sa.Column(
            "routine_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{schema}.agent_routines.routine_id"),
            nullable=False,
        ),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("day", sa.Text(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("charged_tokens", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "usage_event_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{schema}.usage_events.usage_id"),
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "reserved_tokens>0 AND charged_tokens>=0",
            name="ck_routine_reservation_tokens",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','settled','unknown')",
            name="ck_routine_reservation_status",
        ),
        schema=schema,
    )
    op.create_index(
        "idx_routine_budget_day",
        "routine_budget_reservations",
        ["routine_id", "day"],
        schema=schema,
    )


def downgrade():
    schema = _schema()
    if (
        op.get_bind()
        .execute(sa.text(f'SELECT count(*) FROM "{schema}".agent_routines'))
        .scalar()
    ):
        raise RuntimeError(
            "Export routine definitions and runs before removing their schema"
        )
    for table in ("routine_budget_reservations", "routine_runs", "agent_routines"):
        op.drop_table(table, schema=schema)
