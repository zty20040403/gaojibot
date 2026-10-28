"""Bind host-admin quota changes to leased lifecycle jobs, not chat members."""

from alembic import op
import sqlalchemy as sa

import os
import re

revision = "0060_project_quota_jobs"
down_revision = "0059_project_task_prepare"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def _actions(schema, resize):
    actions = "'wake','stop','hibernate','archive','snapshot','restore','task_prepare'"
    if resize:
        actions += ",'resize'"
    op.drop_constraint("ck_project_lifecycle_action", "project_lifecycle_runs", schema=schema)
    op.create_check_constraint("ck_project_lifecycle_action", "project_lifecycle_runs",
        f"action IN ({actions}) AND generation>0 AND next_step>=0", schema=schema)


def upgrade():
    s = _schema()
    _actions(s, True)
    op.alter_column("project_lifecycle_runs", "actor_principal_id", nullable=True, schema=s)
    op.create_check_constraint("ck_project_lifecycle_authority", "project_lifecycle_runs",
        "(action='resize') = (actor_principal_id IS NULL)", schema=s)
    op.create_table("project_quota_authorizations",
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey(f"{s}.project_lifecycle_runs.job_id"), primary_key=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey(f"{s}.projects.project_id"), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("bot_native_user_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("from_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("resource_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger()),
        sa.CheckConstraint("generation>0 AND resource_version>0 AND length(actor) BETWEEN 1 AND 160 "
            "AND from_quota_bytes>=67108864 AND quota_bytes BETWEEN 67108864 AND 107374182400 "
            "AND quota_bytes % 1048576=0 AND quota_bytes<>from_quota_bytes", name="ck_project_quota_authorization"),
        schema=s)


def downgrade():
    s = _schema()
    if op.get_bind().execute(sa.text(
        f'SELECT EXISTS(SELECT 1 FROM "{s}".project_lifecycle_runs WHERE action=\'resize\')'
    )).scalar():
        raise RuntimeError("Export quota lifecycle journals before downgrade")
    op.drop_table("project_quota_authorizations", schema=s)
    op.drop_constraint("ck_project_lifecycle_authority", "project_lifecycle_runs", schema=s)
    op.alter_column("project_lifecycle_runs", "actor_principal_id", nullable=False, schema=s)
    _actions(s, False)
