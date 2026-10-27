"""Scoped retained projects, content versions and separate member login state."""

import os
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0043_project_workspace"
down_revision = "0042_mcp_continuations"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


def upgrade():
    s = _schema()

    def principal(name, **kwargs):
        return sa.Column(
            name,
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.principals.principal_id"),
            **kwargs,
        )

    def project(name="project_id", **kwargs):
        return sa.Column(
            name, sa.BigInteger(), sa.ForeignKey(f"{s}.projects.project_id"), **kwargs
        )

    op.create_table(
        "project_scope_limits",
        sa.Column("scope_key", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_projects", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "kind IN ('group','private') AND quota_bytes>0 AND max_projects>0 AND version>0",
            name="ck_project_scope_limits",
        ),
        schema=s,
    )
    op.create_table(
        "projects",
        sa.Column("project_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "scope_key",
            sa.Text(),
            sa.ForeignKey(f"{s}.project_scope_limits.scope_key"),
            nullable=False,
        ),
        principal("creator_principal_id", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="stopped"),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("sandbox_id", sa.Text()),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "rebuildable_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("sampled_at", sa.BigInteger()),
        sa.Column("quota_mode", sa.Text(), nullable=False, server_default="soft"),
        sa.Column("rebuild_command", sa.Text(), nullable=False, server_default=""),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_active_at", sa.BigInteger(), nullable=False),
        sa.Column("state_changed_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("trashed_at", sa.BigInteger()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "locked_task_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.subagent_tasks.task_id"),
        ),
        sa.UniqueConstraint("project_id", "scope_key", name="uq_project_scope"),
        sa.CheckConstraint(
            "status IN ('active','stopped','hibernated','archived','trash','purged')",
            name="ck_project_status",
        ),
        sa.CheckConstraint(
            "backend IN ('oci','vm') AND quota_mode IN ('hard','soft')",
            name="ck_project_backend",
        ),
        sa.CheckConstraint(
            "quota_bytes>0 AND used_bytes>=0 AND rebuildable_bytes BETWEEN 0 AND used_bytes AND version>0",
            name="ck_project_bytes",
        ),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 120 AND length(rebuild_command)<=4000",
            name="ck_project_text",
        ),
        schema=s,
    )
    op.create_index(
        "ix_projects_scope_activity",
        "projects",
        ["scope_key", "last_active_at"],
        schema=s,
    )
    op.create_table(
        "project_members",
        project(primary_key=True),
        principal("principal_id", primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
        principal("added_by", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner','editor','viewer')", name="ck_project_member_role"
        ),
        schema=s,
    )
    op.create_table(
        "project_blobs",
        sa.Column("sha256", sa.Text(), primary_key=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("unreferenced_since", sa.BigInteger()),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$' AND byte_size BETWEEN 0 AND 268435456",
            name="ck_project_blob",
        ),
        schema=s,
    )
    op.create_table(
        "project_versions",
        project(primary_key=True),
        sa.Column("version_no", sa.Integer(), primary_key=True),
        sa.Column(
            "manifest_sha256",
            sa.Text(),
            sa.ForeignKey(f"{s}.project_blobs.sha256"),
            nullable=False,
        ),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "source_task_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.subagent_tasks.task_id"),
        ),
        sa.Column(
            "source_turn_id", sa.BigInteger(), sa.ForeignKey(f"{s}.agent_turns.turn_id")
        ),
        principal("created_by_principal_id", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "version_no>0 AND file_count BETWEEN 0 AND 20000 AND total_bytes>=0 AND length(label)<=120",
            name="ck_project_version",
        ),
        schema=s,
    )
    op.create_table(
        "project_version_blobs",
        sa.Column("project_id", sa.BigInteger(), primary_key=True),
        sa.Column("version_no", sa.Integer(), primary_key=True),
        sa.Column(
            "sha256",
            sa.Text(),
            sa.ForeignKey(f"{s}.project_blobs.sha256"),
            primary_key=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "version_no"],
            [f"{s}.project_versions.project_id", f"{s}.project_versions.version_no"],
            ondelete="CASCADE",
        ),
        schema=s,
    )
    op.create_index(
        "ix_project_blob_refs", "project_version_blobs", ["sha256"], schema=s
    )
    op.create_table(
        "project_focus",
        sa.Column("scope_key", sa.Text(), primary_key=True),
        principal("principal_id", primary_key=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "scope_key"],
            [f"{s}.projects.project_id", f"{s}.projects.scope_key"],
        ),
        schema=s,
    )
    op.create_table(
        "project_events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        project(nullable=False),
        principal("actor_principal_id"),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        schema=s,
    )
    op.create_index(
        "ix_project_event_time", "project_events", ["project_id", "event_id"], schema=s
    )
    op.create_table(
        "project_previews",
        sa.Column("preview_id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("root", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "version_no"],
            [f"{s}.project_versions.project_id", f"{s}.project_versions.version_no"],
        ),
        sa.CheckConstraint(
            "length(preview_id)>=22 AND expires_at>created_at",
            name="ck_project_preview",
        ),
        schema=s,
    )
    op.add_column("subagent_tasks", project(), schema=s)
    op.add_column(
        "subagent_tasks", sa.Column("project_base_version", sa.Integer()), schema=s
    )
    op.create_foreign_key(
        "fk_task_project_baseline",
        "subagent_tasks",
        "project_versions",
        ["project_id", "project_base_version"],
        ["project_id", "version_no"],
        source_schema=s,
        referent_schema=s,
    )

    # Keep the existing durable queue, reservations and takeover receipts.
    for column in (
        project(),
        principal("creator_principal_id"),
        sa.Column("routine_kind", sa.Text(), nullable=False, server_default="schedule"),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column("spec", JSONB(), nullable=False, server_default="{}"),
        sa.Column("dispatch", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
    ):
        op.add_column("agent_routines", column, schema=s)
    op.execute(
        sa.text(f"UPDATE \"{s}\".agent_routines SET state='paused' WHERE NOT enabled")
    )
    op.execute(
        sa.text(f'''UPDATE "{s}".agent_routines r SET creator_principal_id=i.principal_id
        FROM "{s}".principal_identities i WHERE i.platform=r.platform AND i.native_user_id=r.creator_id''')
    )
    op.create_check_constraint(
        "ck_routine_kind_state",
        "agent_routines",
        "routine_kind IN ('schedule','watch') AND state IN ('pending_confirmation','active','paused','deleted') "
        "AND consecutive_failures>=0 AND (NOT enabled OR state='active')",
        schema=s,
    )
    op.create_table(
        "watch_state",
        sa.Column(
            "routine_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{s}.agent_routines.routine_id"),
            primary_key=True,
        ),
        sa.Column("fingerprint", sa.Text(), nullable=False, server_default=""),
        sa.Column("etag", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_modified", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_checked_at", sa.BigInteger()),
        sa.Column("last_changed_at", sa.BigInteger()),
        sa.Column("change_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "length(change_excerpt)<=8000 AND length(etag)<=2048 AND length(last_modified)<=256 AND version>0",
            name="ck_watch_state",
        ),
        schema=s,
    )
    op.create_table(
        "workbench_login_codes",
        sa.Column("code_hash", sa.Text(), primary_key=True),
        principal("principal_id", nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("used_at", sa.BigInteger()),
        sa.Column("revoked_at", sa.BigInteger()),
        sa.CheckConstraint(
            "code_hash ~ '^[0-9a-f]{64}$' AND expires_at>created_at",
            name="ck_workbench_code",
        ),
        schema=s,
    )
    op.create_table(
        "workbench_sessions",
        sa.Column("session_hash", sa.Text(), primary_key=True),
        principal("principal_id", nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("csrf_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger()),
        sa.CheckConstraint(
            "session_hash ~ '^[0-9a-f]{64}$' AND csrf_hash ~ '^[0-9a-f]{64}$' AND expires_at>created_at",
            name="ck_workbench_session",
        ),
        schema=s,
    )
    op.create_index(
        "ix_workbench_session_owner",
        "workbench_sessions",
        ["principal_id", "expires_at"],
        schema=s,
    )


def downgrade():
    s = _schema()
    for table in (
        "project_scope_limits",
        "projects",
        "project_blobs",
        "workbench_login_codes",
        "workbench_sessions",
        "watch_state",
    ):
        if (
            op.get_bind()
            .execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM "{s}".{table})'))
            .scalar()
        ):
            raise RuntimeError(
                "Export retained project/workbench data before downgrading"
            )
    if (
        op.get_bind()
        .execute(
            sa.text(f'''SELECT EXISTS(SELECT 1 FROM "{s}".agent_routines
        WHERE state IN ('pending_confirmation','deleted') OR consecutive_failures<>0
        OR project_id IS NOT NULL OR spec<>'{{}}'::jsonb
        OR dispatch<>'{{}}'::jsonb OR routine_kind<>'schedule')''')
        )
        .scalar()
    ):
        raise RuntimeError(
            "Export project/watch routine definitions before downgrading"
        )
    op.drop_table("workbench_sessions", schema=s)
    op.drop_table("workbench_login_codes", schema=s)
    op.drop_table("watch_state", schema=s)
    op.drop_constraint("ck_routine_kind_state", "agent_routines", schema=s)
    for name in (
        "project_id",
        "creator_principal_id",
        "routine_kind",
        "state",
        "spec",
        "dispatch",
        "consecutive_failures",
    ):
        op.drop_column("agent_routines", name, schema=s)
    op.drop_constraint("fk_task_project_baseline", "subagent_tasks", schema=s)
    op.drop_column("subagent_tasks", "project_base_version", schema=s)
    op.drop_column("subagent_tasks", "project_id", schema=s)
    for table in (
        "project_previews",
        "project_events",
        "project_focus",
        "project_version_blobs",
        "project_versions",
        "project_blobs",
        "project_members",
        "projects",
        "project_scope_limits",
    ):
        op.drop_table(table, schema=s)
