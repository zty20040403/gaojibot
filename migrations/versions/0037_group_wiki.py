"""Versioned opt-in group wiki and once-only publication receipts."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0037_group_wiki"
down_revision = "0036_routine_quiet_completion"
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
        "group_wiki_settings",
        sa.Column("scope_key", sa.Text(), primary_key=True),
        sa.Column("bot_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("newcomer_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checked_slot", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("version>0", name="ck_wiki_settings_version"),
        schema=schema,
    )
    op.create_table(
        "group_wiki_pages",
        sa.Column("scope_key", sa.Text(), sa.ForeignKey(f"{schema}.group_wiki_settings.scope_key"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=False),
        sa.Column("items_json", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("revision>0", name="ck_wiki_page_revision"),
        schema=schema,
    )
    op.create_table(
        "group_wiki_publications",
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.durable_jobs.job_id"), primary_key=True),
        sa.Column("scope_key", sa.Text(), sa.ForeignKey(f"{schema}.group_wiki_settings.scope_key"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        schema=schema,
    )
    op.create_table(
        "group_wiki_generations",
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey(f"{schema}.durable_jobs.job_id"), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("attempt>0 AND reserved_tokens>0", name="ck_wiki_generation_budget"),
        schema=schema,
    )


def downgrade():
    for table in ("group_wiki_generations", "group_wiki_publications", "group_wiki_pages", "group_wiki_settings"):
        op.drop_table(table, schema=_schema())
