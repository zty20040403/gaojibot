"""At-most-once MCP dispatch and exact approval-bound receipts."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0041_mcp_calls"
down_revision = "0040_mcp_boundaries"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    schema = _schema()
    op.create_table("mcp_calls",
        sa.Column("call_id", sa.Text(), primary_key=True),
        sa.Column("invocation_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("server_name", sa.Text(), nullable=False),
        sa.Column("remote_name", sa.Text(), nullable=False),
        sa.Column("binding_json", sa.Text(), nullable=False),
        sa.Column("binding_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text(), sa.ForeignKey(f"{schema}.admin_approvals.approval_id")),
        sa.Column("owner", sa.Text(), nullable=False, server_default=""),
        sa.Column("lease_until", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("dispatched_at", sa.BigInteger()),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["server_name", "remote_name"],
            [f"{schema}.mcp_tools.server_name", f"{schema}.mcp_tools.remote_name"]),
        sa.CheckConstraint("status IN ('waiting','ready','connecting','dispatched','succeeded','failed','needs_attention','cancelled')",
            name="ck_mcp_call_status"),
        sa.CheckConstraint("expires_at>created_at AND lease_until>=0", name="ck_mcp_call_lifetime"),
        schema=schema)
    op.create_index("ix_mcp_calls_recovery", "mcp_calls", ["status", "lease_until"], schema=schema)
    op.create_index("ix_mcp_calls_scope", "mcp_calls", ["scope_key", "created_at"], schema=schema)


def downgrade():
    op.drop_table("mcp_calls", schema=_schema())
