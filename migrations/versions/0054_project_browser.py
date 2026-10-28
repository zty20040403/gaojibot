"""Application-side browser receipts; a live session retains its project IO grant."""

import os
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0054_project_browser"
down_revision = "0053_project_restore"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    s = _schema()
    op.create_table(
        "project_browser_sessions",
        sa.Column("operation_id", sa.BigInteger(), sa.ForeignKey(f"{s}.project_io.operation_id"),
                  primary_key=True, autoincrement=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("launch_id", sa.Text(), nullable=False, unique=True),
        sa.Column("session_id", sa.Text(), unique=True),
        sa.Column("worker_id", sa.Text()),
        sa.Column("runtime", JSONB()),
        sa.Column("config_sha256", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False, server_default="opening"),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("closed_evidence", JSONB()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "instance_id ~ '^[a-f0-9]{32}$' AND launch_id ~ '^[a-f0-9]{32}$' "
            "AND (session_id IS NULL OR session_id ~ '^[a-f0-9]{32}$') "
            "AND (worker_id IS NULL OR worker_id ~ '^[a-f0-9]{32}$') "
            "AND (config_sha256 IS NULL OR config_sha256 ~ '^[a-f0-9]{64}$')",
            name="ck_project_browser_keys",
        ),
        sa.CheckConstraint("state IN ('opening','ready','needs_attention','draining','closed') "
                           "AND sequence>=0 AND sequence<9007199254740992",
                           name="ck_project_browser_state"),
        sa.CheckConstraint("state<>'ready' OR (session_id IS NOT NULL AND worker_id IS NOT NULL "
                           "AND runtime IS NOT NULL AND config_sha256 IS NOT NULL)",
                           name="ck_project_browser_ready"),
        sa.CheckConstraint("state<>'closed' OR closed_evidence IS NOT NULL",
                           name="ck_project_browser_closed"),
        schema=s,
    )
    op.create_table(
        "project_browser_calls",
        sa.Column("operation_id", sa.BigInteger(),
                  sa.ForeignKey(f"{s}.project_browser_sessions.operation_id"), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("arguments_sha256", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="reserved"),
        sa.Column("response", JSONB()),
        sa.Column("response_sha256", sa.Text()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("operation_id", "request_key", name="uq_project_browser_request"),
        sa.CheckConstraint("request_key ~ '^[a-f0-9]{64}$' AND arguments_sha256 ~ '^[a-f0-9]{64}$' "
                           "AND (response_sha256 IS NULL OR response_sha256 ~ '^[a-f0-9]{64}$') "
                           "AND sequence>0 AND sequence<9007199254740992",
                           name="ck_project_browser_call_keys"),
        sa.CheckConstraint("state IN ('reserved','dispatched','received','acknowledged','needs_attention','aborted')",
                           name="ck_project_browser_call_state"),
        sa.CheckConstraint("(response IS NULL) = (response_sha256 IS NULL) "
                           "AND (state NOT IN ('received','acknowledged') OR response IS NOT NULL)",
                           name="ck_project_browser_call_receipt"),
        schema=s,
    )
    op.create_index("uq_project_browser_pending", "project_browser_calls", ["operation_id"],
                    unique=True, postgresql_where=sa.text("state NOT IN ('acknowledged','aborted')"), schema=s)
    op.create_index("ix_project_browser_request", "project_browser_calls", ["request_key"], schema=s)


def downgrade():
    s = _schema()
    if op.get_bind().execute(sa.text(
        f'SELECT EXISTS(SELECT 1 FROM "{s}".project_browser_sessions)'
    )).scalar():
        raise RuntimeError("Export project browser sessions and receipts before downgrade")
    op.drop_table("project_browser_calls", schema=s)
    op.drop_table("project_browser_sessions", schema=s)
