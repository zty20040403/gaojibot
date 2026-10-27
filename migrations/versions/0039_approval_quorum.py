"""Bind independent administrator votes to a frozen approval generation."""

from alembic import op
import sqlalchemy as sa
import os
import re

revision = "0039_approval_quorum"
down_revision = "0038_task_collaboration"
branch_labels = None
depends_on = None


def upgrade():
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    op.add_column("admin_approvals", sa.Column("quorum", sa.Integer(), nullable=False, server_default="1"), schema=schema)
    op.add_column("admin_approvals", sa.Column("parent_approval_id", sa.Text()), schema=schema)
    op.add_column("admin_approvals", sa.Column("parent_generation", sa.BigInteger()), schema=schema)
    op.create_foreign_key("fk_approval_parent", "admin_approvals", "admin_approvals", ["parent_approval_id"], ["approval_id"], source_schema=schema, referent_schema=schema)
    op.create_check_constraint("ck_approval_quorum", "admin_approvals", "quorum IN (1,2)", schema=schema)
    op.create_check_constraint("ck_approval_parent", "admin_approvals", "(parent_approval_id IS NULL AND parent_generation IS NULL AND kind <> 'quorum_vote') OR (parent_approval_id IS NOT NULL AND parent_generation IS NOT NULL AND kind = 'quorum_vote' AND quorum = 1)", schema=schema)
    op.create_unique_constraint("uq_approval_vote", "admin_approvals", ["parent_approval_id", "parent_generation", "account_id"], schema=schema)
    op.drop_constraint("admin_approvals_status_check", "admin_approvals", schema=schema, type_="check")
    op.create_check_constraint("admin_approvals_status_check", "admin_approvals", "status IN ('sending','pending','awaiting_quorum','queued','executing','succeeded','failed','expired','cancelled','locked','delivery_failed','needs_attention')", schema=schema)
    # An old single-admin grant must not become a two-admin grant after upgrade.
    op.execute(sa.text(f'''UPDATE "{schema}".admin_approvals SET quorum=2,
        status=CASE WHEN status='executing' THEN 'needs_attention' ELSE 'cancelled' END,
        code_hash='' WHERE kind IN ('server_task','fleet')
        AND status IN ('sending','pending','queued','executing','succeeded')'''))
    # Existing OTPs predate the quorum binding and must be reissued explicitly.
    op.execute(sa.text(f'''UPDATE "{schema}".admin_approvals SET status='cancelled', code_hash=''
        WHERE status IN ('sending','pending','queued')'''))


def downgrade():
    raise RuntimeError("Approval votes are audit evidence; migrate forward instead of discarding them")
