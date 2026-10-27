"""Versioned external tool catalogs and revocable group-only read capabilities."""

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0040_mcp_boundaries"
down_revision = "0039_approval_quorum"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    schema = _schema()
    op.create_table("mcp_servers",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text(), sa.ForeignKey(f"{schema}.admin_accounts.account_id"), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("version>0", name="ck_mcp_server_version"), schema=schema)
    op.create_table("mcp_tools",
        sa.Column("server_name", sa.Text(), sa.ForeignKey(f"{schema}.mcp_servers.name"), primary_key=True),
        sa.Column("remote_name", sa.Text(), primary_key=True),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("present", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trust", sa.Text(), nullable=False, server_default="approval"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("version>0 AND trust IN ('approval','safe')", name="ck_mcp_tool_policy"), schema=schema)
    op.create_table("mcp_group_tokens",
        sa.Column("token_id", sa.Text(), primary_key=True),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), sa.ForeignKey(f"{schema}.admin_accounts.account_id"), nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("rate_slot", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("rate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("version>0 AND expires_at>created_at AND rate_count>=0", name="ck_mcp_token_lifetime"),
        schema=schema)
    op.create_index("ix_mcp_group_tokens_scope", "mcp_group_tokens", ["scope_key", "expires_at"], schema=schema)


def downgrade():
    for name in ("mcp_group_tokens", "mcp_tools", "mcp_servers"):
        op.drop_table(name, schema=_schema())
