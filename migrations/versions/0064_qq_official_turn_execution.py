"""Bind existing official turn jobs to one execution, result and delivery plan.

No scheduler, new queue, runtime startup, or network permission is introduced.
"""
from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0064_qq_official_turn_execution"
down_revision = "0063_qq_official_outbox"
branch_labels = None
depends_on = None


def _schema():
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("invalid_execution_schema")
    return '"' + value + '"'


PREPARE = (
    "ALTER TABLE @S@.qq_official_inbound_events ADD COLUMN turn_text TEXT CHECK (octet_length(turn_text) <= 262144)",
    "ALTER TABLE @S@.qq_official_inbound_events ADD COLUMN turn_text_version SMALLINT CHECK (turn_text_version=1)",
    "ALTER TABLE @S@.qq_official_inbound_events ADD CONSTRAINT ck_qq_turn_text_pair CHECK ((turn_text IS NULL)=(turn_text_version IS NULL))",
    "ALTER TABLE @S@.qq_official_inbound_events ADD CONSTRAINT ck_qq_turn_text_not_control CHECK (turn_text IS NULL OR reason_code IS DISTINCT FROM 'binding_control')",
    "ALTER TABLE @S@.qq_official_inbound_events ADD CONSTRAINT uq_qq_turn_inbox_source UNIQUE(inbox_id,account_key,endpoint_key,dispatch_job_id,canonical_message_id)",
    "ALTER TABLE @S@.qq_official_inbound_events ADD CONSTRAINT uq_qq_turn_inbox_native UNIQUE(inbox_id,native_message_id)",
    "ALTER TABLE @S@.agent_turns ADD CONSTRAINT uq_qq_turn_journal_source UNIQUE(turn_id,trigger_canonical_message_id,scope_key)",
)
TABLE = """
CREATE TABLE @S@.qq_official_turn_executions (
    execution_id TEXT PRIMARY KEY CHECK (execution_id ~ '^[a-f0-9]{32}$'),
    account_key TEXT NOT NULL,
    endpoint_key TEXT NOT NULL,
    requester_actor_key TEXT NOT NULL CHECK (octet_length(requester_actor_key) BETWEEN 1 AND 1280),
    inbox_id TEXT NOT NULL UNIQUE,
    job_id BIGINT NOT NULL UNIQUE REFERENCES @S@.durable_jobs(job_id),
    canonical_message_id BIGINT NOT NULL,
    source_native_message_id TEXT NOT NULL CHECK (octet_length(source_native_message_id) BETWEEN 1 AND 256),
    scope_key TEXT NOT NULL CHECK (octet_length(scope_key) BETWEEN 1 AND 1000),
    endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation >= 1),
    route_epoch BIGINT NOT NULL CHECK (route_epoch >= 1),
    account_fencing_epoch BIGINT NOT NULL CHECK (account_fencing_epoch >= 1),
    worker_owner TEXT NOT NULL CHECK (octet_length(worker_owner) BETWEEN 1 AND 160),
    job_lease_generation BIGINT NOT NULL CHECK (job_lease_generation >= 1),
    execution_revision BIGINT NOT NULL DEFAULT 1 CHECK (execution_revision >= 1),
    state TEXT NOT NULL CHECK (state IN ('prepared','executing','result_ready','handed_off','cancelled','needs_attention')),
    input_digest TEXT NOT NULL CHECK (input_digest ~ '^[a-f0-9]{64}$'),
    result_purpose TEXT NOT NULL CHECK (result_purpose IN ('final','background_result')),
    execution_kind TEXT NOT NULL CHECK (execution_kind IN ('chat','command')),
    journal_turn_id BIGINT UNIQUE,
    result_body_json TEXT CHECK (octet_length(result_body_json) BETWEEN 1 AND 262144),
    result_digest TEXT CHECK (result_digest ~ '^[a-f0-9]{64}$'),
    result_status TEXT CHECK (result_status IN ('succeeded','silence','crashed','aborted','denied','cancelled')),
    plan_id TEXT UNIQUE,
    reason_code TEXT NOT NULL DEFAULT '' CHECK (reason_code='' OR reason_code ~ '^[a-z0-9_]{1,80}$'),
    deadline_at BIGINT NOT NULL CHECK (deadline_at >= 0),
    created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
    started_at BIGINT, result_at BIGINT, handed_off_at BIGINT,
    FOREIGN KEY(endpoint_key,account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key,account_key),
    FOREIGN KEY(inbox_id,account_key,endpoint_key,job_id,canonical_message_id)
        REFERENCES @S@.qq_official_inbound_events(inbox_id,account_key,endpoint_key,dispatch_job_id,canonical_message_id),
    FOREIGN KEY(inbox_id,source_native_message_id) REFERENCES @S@.qq_official_inbound_events(inbox_id,native_message_id),
    FOREIGN KEY(journal_turn_id,canonical_message_id,scope_key)
        REFERENCES @S@.agent_turns(turn_id,trigger_canonical_message_id,scope_key),
    FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES @S@.delivery_plans(plan_id,account_key,endpoint_key),
    FOREIGN KEY(plan_id,requester_actor_key) REFERENCES @S@.delivery_plans(plan_id,requester_actor_key),
    CHECK ((result_body_json IS NULL AND result_digest IS NULL AND result_status IS NULL AND result_at IS NULL)
        OR (result_body_json IS NOT NULL AND result_digest IS NOT NULL AND result_status IS NOT NULL AND result_at IS NOT NULL)),
    CHECK (state NOT IN ('result_ready','handed_off') OR result_body_json IS NOT NULL),
    CHECK (result_body_json IS NULL OR state IN ('result_ready','handed_off','cancelled')),
    CHECK (execution_kind='chat' OR journal_turn_id IS NULL),
    CHECK (state<>'executing' OR (started_at IS NOT NULL AND (journal_turn_id IS NOT NULL OR execution_kind='command'))),
    CHECK ((plan_id IS NULL) OR state='handed_off'),
    CHECK ((state='handed_off') = (handed_off_at IS NOT NULL)),
    CHECK (state<>'handed_off' OR plan_id IS NOT NULL OR
        (result_body_json='{"v":1,"nodes":[]}' AND result_status IN ('silence','aborted','cancelled')))
)
"""


CONTROL_PREPARE = (
    "ALTER TABLE @S@.qq_official_inbound_events ADD COLUMN control_actor_key TEXT CHECK (octet_length(control_actor_key) BETWEEN 1 AND 1280)",
    "ALTER TABLE @S@.qq_official_inbound_events ADD COLUMN control_action TEXT CHECK (control_action IN ('whoami','create','submit','status','confirm','reject','cancel','invalid'))",
    "ALTER TABLE @S@.qq_official_inbound_events ADD COLUMN control_decision_code TEXT CHECK (control_decision_code IN ('already_confirmed','cancelled','candidate','confirmed','conflict','denied','expired','identity','invalid','issued','limited','locked','pending','rejected','sending','stale','syntax'))",
    """ALTER TABLE @S@.qq_official_inbound_events ADD CONSTRAINT ck_qq_control_source CHECK (
        (control_actor_key IS NULL AND control_action IS NULL AND control_decision_code IS NULL) OR
        (control_actor_key IS NOT NULL AND control_action IS NOT NULL AND control_decision_code IS NOT NULL
         AND echo_state='not_echo'
         AND endpoint_key IS NOT NULL AND native_message_id IS NOT NULL AND payload_json IS NULL
         AND turn_text IS NULL AND turn_text_version IS NULL))""",
    "ALTER TABLE @S@.qq_official_inbound_events ADD CONSTRAINT uq_qq_control_source_account UNIQUE(inbox_id,account_key,endpoint_key)",
    "ALTER TABLE @S@.qq_official_inbound_events ADD CONSTRAINT uq_qq_control_source_actor UNIQUE(inbox_id,endpoint_key,control_actor_key)",
    "ALTER TABLE @S@.qq_official_gateway_receipts ADD CONSTRAINT uq_qq_control_gateway_source UNIQUE(account_key,session_id,seq,inbox_id)",
    "ALTER TABLE @S@.deliveries ADD CONSTRAINT uq_qq_control_delivery_plan UNIQUE(delivery_id,plan_id)",
)

CONTROL_TABLE = """CREATE TABLE @S@.qq_official_control_decisions (
    control_id TEXT PRIMARY KEY CHECK (control_id ~ '^[a-f0-9]{32}$'),
    inbox_id TEXT NOT NULL UNIQUE,
    account_key TEXT NOT NULL,
    endpoint_key TEXT NOT NULL,
    requester_actor_key TEXT NOT NULL CHECK (octet_length(requester_actor_key) BETWEEN 1 AND 1280),
    source_session_id TEXT NOT NULL CHECK (octet_length(source_session_id) BETWEEN 1 AND 512),
    source_seq BIGINT NOT NULL CHECK (source_seq >= 0),
    source_native_message_id TEXT NOT NULL CHECK (octet_length(source_native_message_id) BETWEEN 1 AND 256),
    endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation >= 1),
    route_epoch BIGINT NOT NULL CHECK (route_epoch >= 1),
    action TEXT NOT NULL CHECK (action IN ('whoami','create','submit','status','confirm','reject','cancel','invalid')),
    decision_code TEXT NOT NULL CHECK (decision_code IN ('already_confirmed','cancelled','candidate','confirmed','conflict','denied','expired','identity','invalid','issued','limited','locked','pending','rejected','sending','stale','syntax')),
    reply_kind TEXT NOT NULL CHECK (reply_kind IN ('whoami','ack','suppressed')),
    body_digest TEXT NOT NULL CHECK (body_digest ~ '^[a-f0-9]{64}$'),
    reply_window_id TEXT,
    received_at BIGINT NOT NULL CHECK (received_at BETWEEN 0 AND 9223372036854775777),
    expires_at BIGINT NOT NULL CHECK (expires_at >= 0),
    state TEXT NOT NULL DEFAULT 'recorded' CHECK (state IN ('recorded','handed_off','attempted','terminal')),
    job_id BIGINT UNIQUE REFERENCES @S@.durable_jobs(job_id),
    plan_id TEXT UNIQUE,
    delivery_id BIGINT UNIQUE,
    attempt_id BIGINT UNIQUE,
    created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
    FOREIGN KEY(inbox_id,account_key,endpoint_key) REFERENCES @S@.qq_official_inbound_events(inbox_id,account_key,endpoint_key),
    FOREIGN KEY(inbox_id,endpoint_key,requester_actor_key) REFERENCES @S@.qq_official_inbound_events(inbox_id,endpoint_key,control_actor_key),
    FOREIGN KEY(account_key,source_session_id,source_seq,inbox_id) REFERENCES @S@.qq_official_gateway_receipts(account_key,session_id,seq,inbox_id),
    FOREIGN KEY(reply_window_id,account_key,endpoint_key) REFERENCES @S@.qq_official_reply_windows(window_id,account_key,endpoint_key),
    FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES @S@.delivery_plans(plan_id,account_key,endpoint_key),
    FOREIGN KEY(plan_id,requester_actor_key) REFERENCES @S@.delivery_plans(plan_id,requester_actor_key),
    FOREIGN KEY(delivery_id,plan_id) REFERENCES @S@.deliveries(delivery_id,plan_id),
    FOREIGN KEY(attempt_id,delivery_id,account_key,endpoint_key) REFERENCES @S@.delivery_attempts(attempt_id,delivery_id,account_key,endpoint_key),
    CHECK (expires_at <= received_at + 30),
    CHECK ((reply_kind='suppressed') <= (state='terminal')),
    CHECK (reply_kind<>'suppressed' OR (job_id IS NULL AND plan_id IS NULL AND delivery_id IS NULL AND attempt_id IS NULL)),
    CHECK ((reply_kind<>'suppressed') <= (reply_window_id IS NOT NULL)),
    CHECK ((plan_id IS NULL) = (delivery_id IS NULL)),
    CHECK ((state IN ('handed_off','attempted')) <= (plan_id IS NOT NULL)),
    CHECK ((attempt_id IS NOT NULL) <= (plan_id IS NOT NULL)),
    CHECK ((state='attempted') <= (attempt_id IS NOT NULL))
)"""


SUBAGENT_UPGRADE = (
    "ALTER TABLE @S@.subagent_tasks ALTER COLUMN requester_user_id DROP NOT NULL",
    "ALTER TABLE @S@.subagent_tasks ADD COLUMN account_key TEXT CHECK (octet_length(account_key) BETWEEN 1 AND 512)",
    "ALTER TABLE @S@.subagent_tasks ADD COLUMN endpoint_key TEXT CHECK (octet_length(endpoint_key) BETWEEN 1 AND 1000)",
    "ALTER TABLE @S@.subagent_tasks ADD COLUMN requester_actor_key TEXT CHECK (octet_length(requester_actor_key) BETWEEN 1 AND 1280)",
    """ALTER TABLE @S@.subagent_tasks ADD CONSTRAINT ck_subagent_identity_union CHECK (
        (requester_user_id IS NOT NULL AND account_key IS NULL
            AND endpoint_key IS NULL AND requester_actor_key IS NULL)
        OR (requester_user_id IS NULL AND account_key IS NOT NULL
            AND endpoint_key IS NOT NULL AND requester_actor_key IS NOT NULL
            AND trigger_message_id IS NOT NULL AND project_id IS NULL)
    )""",
    """ALTER TABLE @S@.subagent_tasks ADD CONSTRAINT fk_subagent_actual_endpoint
        FOREIGN KEY(endpoint_key,account_key)
        REFERENCES @S@.qq_official_endpoints(endpoint_key,account_key)""",
    """ALTER TABLE @S@.subagent_tasks ADD CONSTRAINT uq_subagent_actual_requester
        UNIQUE(task_id,endpoint_key,requester_actor_key)""",
    """ALTER TABLE @S@.subagent_tasks ADD CONSTRAINT uq_subagent_canonical_source
        UNIQUE(task_id,trigger_message_id)""",
    """ALTER TABLE @S@.qq_official_turn_executions ADD CONSTRAINT uq_qq_subagent_parent_actor
        UNIQUE(execution_id,endpoint_key,requester_actor_key)""",
    # Parent source must also agree with the original native and canonical IDs.
    # This does not duplicate endpoint/actor into the same oversized B-tree key.
    """ALTER TABLE @S@.qq_official_turn_executions ADD CONSTRAINT uq_qq_subagent_parent_message
        UNIQUE(execution_id,canonical_message_id,source_native_message_id)""",
    """CREATE TABLE @S@.qq_official_subagent_dispatches (
        task_id BIGINT PRIMARY KEY,
        task_revision BIGINT NOT NULL DEFAULT 1 CHECK (task_revision=1),
        account_key TEXT NOT NULL,
        endpoint_key TEXT NOT NULL,
        requester_actor_key TEXT NOT NULL,
        source_execution_id TEXT NOT NULL,
        source_canonical_message_id BIGINT NOT NULL,
        source_native_message_id TEXT NOT NULL CHECK (octet_length(source_native_message_id) BETWEEN 1 AND 256),
        submission_key TEXT NOT NULL CHECK (submission_key ~ '^[a-f0-9]{64}$'),
        submission_digest TEXT NOT NULL CHECK (submission_digest ~ '^[a-f0-9]{64}$'),
        job_id BIGINT NOT NULL UNIQUE REFERENCES @S@.durable_jobs(job_id),
        endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation>=1),
        route_epoch BIGINT NOT NULL CHECK (route_epoch>=1),
        tool_profile TEXT NOT NULL CHECK (tool_profile='official-text-v1'),
        execution_deadline_at BIGINT NOT NULL CHECK (execution_deadline_at>=0),
        result_wait_seconds INTEGER NOT NULL CHECK (result_wait_seconds BETWEEN 1 AND 86400),
        result_body_json TEXT CHECK (octet_length(result_body_json) BETWEEN 1 AND 262144),
        result_digest TEXT CHECK (result_digest ~ '^[a-f0-9]{64}$'),
        result_task_status TEXT CHECK (result_task_status IN ('completed','partial','failed','cancelled')),
        result_at BIGINT CHECK (result_at>=0),
        result_expires_at BIGINT CHECK (result_expires_at>=0),
        plan_id TEXT UNIQUE,
        handed_off_at BIGINT CHECK (handed_off_at>=0),
        suppressed_reason TEXT CHECK (suppressed_reason ~ '^[a-z0-9_]{1,80}$'),
        created_at BIGINT NOT NULL CHECK (created_at>=0),
        updated_at BIGINT NOT NULL CHECK (updated_at>=created_at),
        UNIQUE(source_execution_id,submission_key),
        FOREIGN KEY(endpoint_key,account_key)
            REFERENCES @S@.qq_official_endpoints(endpoint_key,account_key),
        FOREIGN KEY(task_id,endpoint_key,requester_actor_key)
            REFERENCES @S@.subagent_tasks(task_id,endpoint_key,requester_actor_key),
        FOREIGN KEY(task_id,source_canonical_message_id)
            REFERENCES @S@.subagent_tasks(task_id,trigger_message_id),
        FOREIGN KEY(source_execution_id,endpoint_key,requester_actor_key)
            REFERENCES @S@.qq_official_turn_executions(execution_id,endpoint_key,requester_actor_key),
        FOREIGN KEY(source_execution_id,source_canonical_message_id,source_native_message_id)
            REFERENCES @S@.qq_official_turn_executions(execution_id,canonical_message_id,source_native_message_id),
        FOREIGN KEY(plan_id,account_key,endpoint_key)
            REFERENCES @S@.delivery_plans(plan_id,account_key,endpoint_key),
        FOREIGN KEY(plan_id,requester_actor_key)
            REFERENCES @S@.delivery_plans(plan_id,requester_actor_key),
        CHECK (execution_deadline_at>created_at),
        CHECK ((result_body_json IS NULL AND result_digest IS NULL
            AND result_task_status IS NULL AND result_at IS NULL AND result_expires_at IS NULL)
            OR (result_body_json IS NOT NULL AND result_digest IS NOT NULL
            AND result_task_status IS NOT NULL AND result_at IS NOT NULL AND result_expires_at IS NOT NULL)),
        CHECK (result_at IS NULL OR result_at>=created_at),
        CHECK (result_expires_at IS NULL OR
            (result_expires_at>result_at AND result_expires_at-result_at<=result_wait_seconds)),
        CHECK ((plan_id IS NULL)=(handed_off_at IS NULL)),
        CHECK (plan_id IS NULL OR result_body_json IS NOT NULL),
        CHECK (suppressed_reason IS NULL OR plan_id IS NULL)
    )""",
    """CREATE INDEX ix_qq_subagent_account_task
        ON @S@.qq_official_subagent_dispatches(account_key,task_id)""",
    """CREATE INDEX ix_subagent_actual_owner
        ON @S@.subagent_tasks(endpoint_key,requester_actor_key,task_id)
        WHERE account_key IS NOT NULL""",
)

# These checks run BEFORE any destructive downgrade statements. Existing 0064
# guards for foreground/control evidence still apply independently.
SUBAGENT_DOWNGRADE_REFUSE_QUERY = """SELECT
    EXISTS(SELECT 1 FROM @S@.qq_official_subagent_dispatches)
    OR EXISTS(SELECT 1 FROM @S@.subagent_tasks WHERE account_key IS NOT NULL
        OR endpoint_key IS NOT NULL OR requester_actor_key IS NOT NULL
        OR requester_user_id IS NULL)"""

SUBAGENT_DOWNGRADE = (
    "DROP TABLE @S@.qq_official_subagent_dispatches",
    "ALTER TABLE @S@.qq_official_turn_executions DROP CONSTRAINT uq_qq_subagent_parent_message",
    "ALTER TABLE @S@.qq_official_turn_executions DROP CONSTRAINT uq_qq_subagent_parent_actor",
    "DROP INDEX @S@.ix_subagent_actual_owner",
    "ALTER TABLE @S@.subagent_tasks DROP CONSTRAINT uq_subagent_actual_requester",
    "ALTER TABLE @S@.subagent_tasks DROP CONSTRAINT uq_subagent_canonical_source",
    "ALTER TABLE @S@.subagent_tasks DROP CONSTRAINT fk_subagent_actual_endpoint",
    "ALTER TABLE @S@.subagent_tasks DROP CONSTRAINT ck_subagent_identity_union",
    "ALTER TABLE @S@.subagent_tasks DROP COLUMN requester_actor_key",
    "ALTER TABLE @S@.subagent_tasks DROP COLUMN endpoint_key",
    "ALTER TABLE @S@.subagent_tasks DROP COLUMN account_key",
    "ALTER TABLE @S@.subagent_tasks ALTER COLUMN requester_user_id SET NOT NULL",
)


def upgrade():
    schema = _schema()
    for statement in (*PREPARE, TABLE, *CONTROL_PREPARE, CONTROL_TABLE, *SUBAGENT_UPGRADE):
        op.execute(sa.text(statement.replace("@S@", schema).replace(":", r"\:")))
    op.execute(sa.text(f"CREATE INDEX ix_qq_turn_execution_state ON {schema}.qq_official_turn_executions(account_key,state)"))


def downgrade():
    schema = _schema()
    if op.get_bind().execute(sa.text(SUBAGENT_DOWNGRADE_REFUSE_QUERY.replace("@S@", schema))).scalar():
        raise RuntimeError("subagent_evidence_requires_export")
    if op.get_bind().execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {schema}.qq_official_turn_executions) OR EXISTS(SELECT 1 FROM {schema}.qq_official_control_decisions) OR EXISTS(SELECT 1 FROM {schema}.qq_official_inbound_events WHERE control_actor_key IS NOT NULL OR turn_text IS NOT NULL OR turn_text_version IS NOT NULL)")).scalar():
        raise RuntimeError("execution_evidence_requires_export")
    for statement in SUBAGENT_DOWNGRADE:
        op.execute(sa.text(statement.replace("@S@", schema).replace(":", r"\:")))
    op.execute(sa.text(f"DROP TABLE {schema}.qq_official_control_decisions"))
    op.execute(sa.text(f"ALTER TABLE {schema}.deliveries DROP CONSTRAINT uq_qq_control_delivery_plan"))
    op.execute(sa.text(f"ALTER TABLE {schema}.qq_official_gateway_receipts DROP CONSTRAINT uq_qq_control_gateway_source"))
    for name in ("uq_qq_control_source_actor", "uq_qq_control_source_account", "ck_qq_control_source"):
        op.execute(sa.text(f"ALTER TABLE {schema}.qq_official_inbound_events DROP CONSTRAINT {name}"))
    for name in ("control_actor_key", "control_action", "control_decision_code"):
        op.execute(sa.text(f"ALTER TABLE {schema}.qq_official_inbound_events DROP COLUMN {name}"))
    op.execute(sa.text(f"DROP TABLE {schema}.qq_official_turn_executions"))
    op.execute(sa.text(f"ALTER TABLE {schema}.agent_turns DROP CONSTRAINT uq_qq_turn_journal_source"))
    op.execute(sa.text(f"ALTER TABLE {schema}.qq_official_inbound_events DROP CONSTRAINT uq_qq_turn_inbox_source"))
    op.execute(sa.text(f"ALTER TABLE {schema}.qq_official_inbound_events DROP CONSTRAINT uq_qq_turn_inbox_native"))
    for name in ("ck_qq_turn_text_pair", "ck_qq_turn_text_not_control"):
        op.execute(sa.text(f"ALTER TABLE {schema}.qq_official_inbound_events DROP CONSTRAINT {name}"))
    for name in ("turn_text", "turn_text_version"):
        op.execute(sa.text(f"ALTER TABLE {schema}.qq_official_inbound_events DROP COLUMN {name}"))
