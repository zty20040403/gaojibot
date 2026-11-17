"""Drop unused cluster, alert, diagnostic, ops, host API, and reaction tables.

降级只重建空表结构和外键约束，不恢复已删除的历史数据。
Downgrade recreates empty table schemas and constraints only; dropped data is not restored.
"""
from __future__ import annotations

import importlib
import os
import re

from alembic import op

revision = "0107_drop_dead_tables"
down_revision = "0106_question_notices"
branch_labels = None
depends_on = None

# Ordered children-first so every DROP TABLE succeeds without CASCADE.
DEAD_TABLES = (
    "ops_actions",
    "ops_replies",
    "host_api_calls",
    "host_api_quota",
    "admin_fleet_watches",
    "turn_reaction_votes",
    "alert_notifications",
    "alert_events",
    "diagnostic_evidence",
    "diagnostic_runs",
    "fleet_backend_states",
    "fleet_observations",
    "fleet_approvals",
    "fleet_operation_events",
    "fleet_operations",
    "fleet_previews",
    "fleet_artifacts",
    "fleet_reservations",
    "fleet_job_events",
    "fleet_job_checkpoints",
    "fleet_worker_jobs",
    "fleet_borrow_grants",
    "fleet_worker_policies",
    "fleet_workers",
    "fleet_incident_events",
    "fleet_incidents",
    "fleet_runbook_cases",
    "fleet_guardian_checks",
    "fleet_guardians",
    "fleet_deployment_approvals",
    "fleet_deployment_events",
    "fleet_deployment_targets",
    "fleet_deployments",
    "fleet_maintenance_locks",
)

_STANDALONE_REVISIONS = (
    "0012_alert_event_history",
    "0019_cluster_control",
    "0020_incident_diagnostics",
    "0021_cluster_execution",
    "0022_cluster_scheduling_guardians",
    "0023_cluster_deployments",
    "0024_cluster_runtime_constraints",
    "0025_ops_management",
    "0029_native_ssh_operations",
    "0030_turn_reaction_votes",
    "0081_host_api_calls",
    "0089_ops_actions",
)


def _use_schema() -> str:
    schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("Invalid PostgreSQL schema")
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f"SET LOCAL search_path TO {quoted}, public")
    return schema


def upgrade() -> None:
    schema = _use_schema()
    op.execute("ALTER TABLE qq_official_inbound_events DROP CONSTRAINT ops_reply_source")
    op.execute("ALTER TABLE delivery_plans DROP CONSTRAINT delivery_plans_ops_action_id_fkey")
    for table in DEAD_TABLES:
        op.drop_table(table, schema=schema)


def downgrade() -> None:
    """降级只重建空表结构，不恢复数据 (recreates empty schemas only; data is not restored)."""
    _use_schema()
    for module_name in _STANDALONE_REVISIONS:
        importlib.import_module(f"migrations.versions.{module_name}").upgrade()
    _use_schema()
    op.execute(
        """CREATE TABLE admin_fleet_watches (
            path TEXT PRIMARY KEY, account_json TEXT NOT NULL, bot_id TEXT NOT NULL,
            actor TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'watching',
            updated_at BIGINT NOT NULL, lease_until BIGINT NOT NULL DEFAULT 0)"""
    )
    op.execute(
        "ALTER TABLE delivery_plans ADD CONSTRAINT delivery_plans_ops_action_id_fkey "
        "FOREIGN KEY(ops_action_id) REFERENCES ops_actions(action_id)"
    )
    op.execute(
        "ALTER TABLE ops_actions ADD CONSTRAINT ops_notice_source "
        "FOREIGN KEY(notice_plan_id, action_id) REFERENCES delivery_plans(plan_id, ops_action_id)"
    )
    op.execute(
        "ALTER TABLE ops_actions ADD CONSTRAINT ops_reply_identity "
        "UNIQUE(action_id, endpoint_key, actor_key)"
    )
    op.execute(
        "ALTER TABLE qq_official_inbound_events ADD CONSTRAINT ops_reply_source "
        "FOREIGN KEY(ops_reply_action_id, endpoint_key, ops_reply_actor_key) "
        "REFERENCES ops_actions(action_id, endpoint_key, actor_key)"
    )
    op.execute(
        """CREATE TABLE ops_replies (
            inbox_id TEXT PRIMARY KEY, action_id TEXT NOT NULL, account_key TEXT NOT NULL,
            endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
            source_session_id TEXT NOT NULL, source_seq BIGINT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('approved','denied','expired','cancelled')),
            created_at BIGINT NOT NULL,
            UNIQUE(inbox_id, action_id, actor_key),
            FOREIGN KEY(inbox_id, account_key, endpoint_key)
                REFERENCES qq_official_inbound_events(inbox_id, account_key, endpoint_key),
            FOREIGN KEY(inbox_id, action_id, actor_key)
                REFERENCES qq_official_inbound_events(inbox_id, ops_reply_action_id, ops_reply_actor_key),
            FOREIGN KEY(account_key, source_session_id, source_seq, inbox_id)
                REFERENCES qq_official_gateway_receipts(account_key, session_id, seq, inbox_id))"""
    )
    op.execute(
        "ALTER TABLE ops_actions ADD CONSTRAINT ops_approved_reply "
        "FOREIGN KEY(approved_inbox_id, action_id, approved_actor_key) "
        "REFERENCES ops_replies(inbox_id, action_id, actor_key)"
    )
