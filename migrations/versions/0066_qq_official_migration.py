"""Scoped migration journal, project grants and existing-queue continuations.

No old conversation or personal identity is rewritten. Every downgrade first
locks all affected tables and refuses to discard any M5 authorization, routing
or delivery evidence, including revoked and incomplete records.
"""
from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0066_qq_official_migration"
down_revision = "0065_qq_official_files"
branch_labels = None
depends_on = None

UPGRADE = (
    """CREATE TABLE migration_runtime_baselines (
        account_key text PRIMARY KEY, runtime_id text NOT NULL, revision bigint NOT NULL CHECK(revision>0),
        backend_pid integer NOT NULL, advisory_key bigint NOT NULL, deployment_digest text NOT NULL,
        expires_at bigint NOT NULL, ready_at bigint NOT NULL, state text NOT NULL CHECK(state IN ('ready','revoked'))
    )
    """,
    """CREATE TABLE migration_routes (
        route_id text PRIMARY KEY, source_scope_key text NOT NULL UNIQUE,
        source_endpoint_key text NOT NULL UNIQUE,target_scope_key text NOT NULL UNIQUE,
        target_endpoint_key text NOT NULL UNIQUE REFERENCES qq_official_endpoints(endpoint_key),
        source_account_key text NOT NULL REFERENCES migration_runtime_baselines(account_key),
        target_account_key text NOT NULL REFERENCES qq_official_accounts(account_key),
        source_generation bigint,source_endpoint_epoch bigint,
        target_generation bigint NOT NULL,target_endpoint_epoch bigint NOT NULL,
        epoch bigint NOT NULL CHECK(epoch>=0),phase text NOT NULL,
        active_endpoint_key text NOT NULL,baseline_runtime_id text NOT NULL,baseline_revision bigint NOT NULL,
        CHECK(source_scope_key<>target_scope_key),CHECK(source_endpoint_key<>target_endpoint_key),
        CHECK(phase IN ('planned','frozen','draining','switching','verifying','completed','rollback_pending','rolled_back'))
    )
    """,
    """CREATE TABLE migration_scope_aliases (
        scope_key text PRIMARY KEY, route_id text NOT NULL REFERENCES migration_routes(route_id)
    )
    """,
    """CREATE TABLE migration_journals (
        migration_id text PRIMARY KEY,route_id text NOT NULL UNIQUE REFERENCES migration_routes(route_id),
        preview_digest text NOT NULL,preview_json text NOT NULL CHECK(octet_length(preview_json)<=262144),
        creator_id text NOT NULL REFERENCES admin_accounts(account_id),creator_version bigint NOT NULL,
        approved_by text REFERENCES admin_accounts(account_id),approved_version bigint,
        deadline bigint NOT NULL,created_at bigint NOT NULL,verification_nonce text,
        verification_until bigint,revision bigint NOT NULL DEFAULT 1 CHECK(revision>0)
    )
    """,
    """CREATE TABLE migration_events (
        event_id bigserial PRIMARY KEY,migration_id text NOT NULL REFERENCES migration_journals(migration_id),
        phase text NOT NULL,route_epoch bigint NOT NULL,reason text NOT NULL,occurred_at bigint NOT NULL
    )
    """,
    """CREATE TABLE migration_dispatch_activities (
        activity_id text PRIMARY KEY,source_account_key text NOT NULL REFERENCES migration_runtime_baselines(account_key),
        source_scope_key text NOT NULL,source_endpoint_key text NOT NULL,target_endpoint_key text NOT NULL,
        route_id text REFERENCES migration_routes(route_id),route_epoch bigint NOT NULL,
        runtime_id text NOT NULL,operation_ref text NOT NULL,operation text NOT NULL,
        attempt_id bigint REFERENCES delivery_attempts(attempt_id),state text NOT NULL,
        receipt_digest text,receipt_json text,dispatched_at bigint NOT NULL,observed_at bigint,
        UNIQUE(source_account_key,operation_ref),
        CHECK(state IN ('dispatched','accepted','confirmed_rejected','unknown','quarantined')),
        CHECK(operation IN ('send','execute'))
    )
    """,
    """CREATE TABLE migration_verification_receipts (
        migration_id text NOT NULL REFERENCES migration_journals(migration_id),kind text NOT NULL,
        receipt_id text NOT NULL UNIQUE REFERENCES delivery_receipts(receipt_id),
        delivery_id bigint NOT NULL REFERENCES deliveries(delivery_id),nonce text NOT NULL,
        PRIMARY KEY(migration_id,kind),CHECK(kind IN ('text','file'))
    )
    """,
    """CREATE TABLE migration_activity_observations (
        activity_id text NOT NULL REFERENCES migration_dispatch_activities(activity_id),
        receipt_digest text NOT NULL,receipt_json text NOT NULL,observed_at bigint NOT NULL,
        PRIMARY KEY(activity_id,receipt_digest)
    )
    """,
    """CREATE TABLE migration_items (
        item_id text PRIMARY KEY,migration_id text NOT NULL REFERENCES migration_journals(migration_id),
        parent_item_id text REFERENCES migration_items(item_id),occurrence_key text,
        item_revision bigint NOT NULL DEFAULT 1 CHECK(item_revision>0),kind text NOT NULL,source_ref text NOT NULL,
        source_revision bigint NOT NULL,classification text NOT NULL,export_json text NOT NULL,
        export_digest text NOT NULL,source_actor_key text,target_actor_key text,
        mapping_id text,mapping_revision bigint,mapping_expires_at bigint,
        execution_deadline bigint,result_deadline bigint,source_canonical_id bigint,source_native_id text,
        schedule_json text,state text NOT NULL DEFAULT 'reviewed',job_id bigint UNIQUE REFERENCES durable_jobs(job_id),
        expected_job_kind text,target_routine_id bigint REFERENCES agent_routines(routine_id),created_at bigint NOT NULL,
        UNIQUE(migration_id,kind,source_ref),UNIQUE(parent_item_id,occurrence_key),
        CHECK(classification IN ('unsent','unknown','confirmed','terminal','unsupported')),
        CHECK(state IN ('reviewed','prepared','active','quarantined','expired','revoked')),
        CHECK((mapping_id IS NULL AND target_actor_key IS NULL AND source_actor_key IS NULL
               AND mapping_revision IS NULL AND mapping_expires_at IS NULL)
            OR(mapping_id IS NOT NULL AND target_actor_key IS NOT NULL AND source_actor_key IS NOT NULL
               AND mapping_revision IS NOT NULL AND mapping_revision>0 AND mapping_expires_at IS NOT NULL)),
        CHECK((parent_item_id IS NULL)=(occurrence_key IS NULL))
    )
    """,
    """CREATE INDEX ix_migration_activity_drain ON migration_dispatch_activities(source_scope_key,state)
    """,
    """CREATE TABLE migration_item_plans (
        plan_id text PRIMARY KEY REFERENCES delivery_plans(plan_id),
        item_id text NOT NULL REFERENCES migration_items(item_id),
        source_job_id bigint NOT NULL REFERENCES durable_jobs(job_id),
        logical_route_epoch bigint NOT NULL,mapping_revision bigint NOT NULL,created_at bigint NOT NULL
    )
    """,
    """CREATE TABLE migration_preview_revisions (
        migration_id text NOT NULL REFERENCES migration_journals(migration_id),revision bigint NOT NULL,
        preview_digest text NOT NULL,preview_json text NOT NULL,created_at bigint NOT NULL,
        PRIMARY KEY(migration_id,revision)
    )
    """,
    """CREATE TABLE migration_verification_intents (
        migration_id text NOT NULL REFERENCES migration_journals(migration_id),kind text NOT NULL,
        request_id text NOT NULL UNIQUE,endpoint_key text NOT NULL REFERENCES qq_official_endpoints(endpoint_key),
        actor_key text NOT NULL,nonce text NOT NULL,logical_route_epoch bigint NOT NULL,
        endpoint_generation bigint NOT NULL,endpoint_route_epoch bigint NOT NULL,deadline bigint NOT NULL,
        plan_id text UNIQUE REFERENCES delivery_plans(plan_id),
        state text NOT NULL CHECK(state IN ('issued','bound','completed')),
        PRIMARY KEY(migration_id,kind),CHECK(kind IN ('text','file'))
    )
    """,
    """CREATE TABLE qq_migration_public_history_grants (
        grant_id text PRIMARY KEY CHECK (grant_id ~ '^[a-f0-9]{32}$'),
        migration_id text NOT NULL,
        route_id text NOT NULL,
        route_epoch bigint NOT NULL CHECK (route_epoch > 0),
        source_scope_key text NOT NULL,
        source_conversation_id bigint NOT NULL REFERENCES conversations(conversation_id),
        target_endpoint_key text NOT NULL REFERENCES qq_official_endpoints(endpoint_key),
        target_scope_key text NOT NULL,
        target_endpoint_generation bigint NOT NULL CHECK (target_endpoint_generation > 0),
        target_endpoint_route_epoch bigint NOT NULL CHECK (target_endpoint_route_epoch > 0),
        source_floor bigint NOT NULL CHECK (source_floor > 0),
        target_floor bigint NOT NULL CHECK (target_floor > 0),
        source_cleared_at bigint,
        target_cleared_at bigint,
        source_cutoff bigint NOT NULL CHECK (source_cutoff >= 0),
        manifest_json text NOT NULL CHECK (octet_length(manifest_json) <= 65536),
        manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[a-f0-9]{64}$'),
        state text NOT NULL CHECK (state IN ('prepared','active','revoked')),
        created_at bigint NOT NULL,
        CHECK (source_scope_key <> target_scope_key)
    )
    """,
    """CREATE TABLE qq_migration_public_history_snapshots (
        grant_id text NOT NULL REFERENCES qq_migration_public_history_grants(grant_id),
        kind text NOT NULL CHECK (kind IN ('chapter','public_note')),
        source_id bigint NOT NULL CHECK (source_id > 0),
        source_version bigint CHECK (source_version > 0),
        snapshot_json text NOT NULL CHECK (octet_length(snapshot_json) <= 65536),
        snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^[a-f0-9]{64}$'),
        PRIMARY KEY(grant_id,kind,source_id),
        CHECK ((kind='public_note') = (source_version IS NOT NULL))
    )
    """,
    """CREATE TABLE migration_actor_mappings (
 mapping_id TEXT PRIMARY KEY CHECK(mapping_id ~ '^[a-f0-9]{32}$'),
 mapping_key TEXT NOT NULL UNIQUE CHECK(mapping_key ~ '^[a-f0-9]{64}$'),
 source_endpoint_key TEXT NOT NULL CHECK(octet_length(source_endpoint_key) BETWEEN 1 AND 1000),
 source_scope_key TEXT NOT NULL CHECK(octet_length(source_scope_key) BETWEEN 1 AND 1000),
 source_actor_key TEXT NOT NULL CHECK(octet_length(source_actor_key) BETWEEN 1 AND 1280),
 target_account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
 target_endpoint_key TEXT NOT NULL,
 target_actor_key TEXT NOT NULL CHECK(octet_length(target_actor_key) BETWEEN 1 AND 1280),
 revision BIGINT NOT NULL CHECK(revision>=1),
 state TEXT NOT NULL CHECK(state IN ('active','revoked')),
 evidence_inbox_id TEXT NOT NULL REFERENCES qq_official_inbound_events(inbox_id),
 evidence_received_at BIGINT NOT NULL CHECK(evidence_received_at>=0),
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>=1),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>=1),
 expires_at BIGINT NOT NULL,
 created_by_admin TEXT NOT NULL REFERENCES admin_accounts(account_id),
 updated_by_admin TEXT NOT NULL REFERENCES admin_accounts(account_id),
 created_at BIGINT NOT NULL CHECK(created_at>=0),
 updated_at BIGINT NOT NULL CHECK(updated_at>=created_at),
 UNIQUE(mapping_id,revision),
 FOREIGN KEY(target_endpoint_key,target_account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
 CHECK(expires_at>created_at), CHECK(evidence_received_at<=created_at OR evidence_received_at<=updated_at)
)
    """,
    """CREATE TABLE migration_actor_mapping_revisions (
 mapping_id TEXT NOT NULL REFERENCES migration_actor_mappings(mapping_id),
 revision BIGINT NOT NULL CHECK(revision>=1),
 target_actor_key TEXT NOT NULL CHECK(octet_length(target_actor_key) BETWEEN 1 AND 1280),
 state TEXT NOT NULL CHECK(state IN ('active','revoked')),
 evidence_inbox_id TEXT NOT NULL REFERENCES qq_official_inbound_events(inbox_id),
 evidence_received_at BIGINT NOT NULL CHECK(evidence_received_at>=0),
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>=1),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>=1),
 expires_at BIGINT NOT NULL,
 admin_account_id TEXT NOT NULL REFERENCES admin_accounts(account_id),
 admin_version BIGINT NOT NULL CHECK(admin_version>=1),
 request_digest TEXT NOT NULL CHECK(request_digest ~ '^[a-f0-9]{64}$'),
 created_at BIGINT NOT NULL CHECK(created_at>=0), PRIMARY KEY(mapping_id,revision)
)
    """,
    """CREATE TABLE migration_control_audit (
 audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 admin_account_id TEXT NOT NULL REFERENCES admin_accounts(account_id),
 admin_version BIGINT NOT NULL CHECK(admin_version>=1),
 action TEXT NOT NULL CHECK(action ~ '^[a-z_]{1,40}$'),
 resource_id TEXT NOT NULL CHECK(resource_id='' OR resource_id ~ '^[a-f0-9]{32}$'),
 request_digest TEXT NOT NULL CHECK(request_digest ~ '^[a-f0-9]{64}$'),
 preview_digest TEXT NOT NULL CHECK(preview_digest='' OR preview_digest ~ '^[a-f0-9]{64}$'),
 created_at BIGINT NOT NULL CHECK(created_at>=0)
)
    """,
    """CREATE INDEX ix_migration_mapping_target ON migration_actor_mappings(target_endpoint_key,state,expires_at)
    """,
    """CREATE TABLE migration_work_sources (
        source_id TEXT PRIMARY KEY CHECK(source_id ~ '^[0-9a-f]{32}$'),
        account_key TEXT NOT NULL REFERENCES migration_runtime_baselines(account_key),
        endpoint_key TEXT NOT NULL CHECK(octet_length(endpoint_key) BETWEEN 1 AND 1000),
        canonical_scope_key TEXT NOT NULL CHECK(octet_length(canonical_scope_key) BETWEEN 1 AND 1000),
        actor_key TEXT CHECK(octet_length(actor_key) BETWEEN 1 AND 1280),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('handler','job','delivery','reminder','approval','maintenance')),
        source_ref TEXT NOT NULL CHECK(octet_length(source_ref) BETWEEN 1 AND 128),
        source_revision BIGINT NOT NULL CHECK(source_revision>0),
        source_digest TEXT NOT NULL CHECK(source_digest ~ '^[0-9a-f]{64}$'),
        route_epoch BIGINT NOT NULL CHECK(route_epoch>=0),
        baseline_runtime_id TEXT NOT NULL CHECK(baseline_runtime_id ~ '^[0-9a-f]{32}$'),
        active_runtime_id TEXT CHECK(active_runtime_id ~ '^[0-9a-f]{32}$'),
        job_id BIGINT UNIQUE REFERENCES durable_jobs(job_id),
        delivery_id BIGINT UNIQUE REFERENCES deliveries(delivery_id),
        state TEXT NOT NULL CHECK(state IN ('ready','executing','completed','needs_attention','revoked')),
        worker_owner TEXT,
        worker_generation BIGINT CHECK(worker_generation>0),
        effect_sequence BIGINT NOT NULL DEFAULT 0 CHECK(effect_sequence>=0),
        deadline_at BIGINT NOT NULL,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        CHECK(deadline_at>created_at),
        CHECK((source_kind='job')=(job_id IS NOT NULL)),
        CHECK((source_kind='delivery')=(delivery_id IS NOT NULL)),
        CHECK((worker_owner IS NULL)=(worker_generation IS NULL)),
        UNIQUE(account_key,source_kind,source_ref,source_revision),
        UNIQUE(source_id,account_key), UNIQUE(source_id,endpoint_key)
    )
    """,
    """ALTER TABLE migration_dispatch_activities ADD COLUMN source_id TEXT
        REFERENCES migration_work_sources(source_id)
    """,
    """CREATE INDEX ix_migration_work_source_pending ON migration_work_sources(account_key,state,source_id)
    """,
    """CREATE TABLE qq_official_project_grants (
        grant_id TEXT PRIMARY KEY CHECK (grant_id ~ '^[0-9a-f]{32}$'),
        project_id BIGINT NOT NULL REFERENCES projects(project_id),
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL,
        actor_key TEXT NOT NULL CHECK (octet_length(actor_key) BETWEEN 1 AND 1280),
        principal_id BIGINT NOT NULL REFERENCES principals(principal_id),
        project_scope_key TEXT NOT NULL CHECK (octet_length(project_scope_key) BETWEEN 1 AND 1000),
        endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation>=1),
        route_epoch BIGINT NOT NULL CHECK (route_epoch>=0),
        endpoint_route_epoch BIGINT NOT NULL CHECK (endpoint_route_epoch>=1),
        issuer_actor_key TEXT NOT NULL CHECK (octet_length(issuer_actor_key) BETWEEN 1 AND 1280),
        owner_grant_id TEXT NOT NULL,
        owner_revision BIGINT NOT NULL CHECK (owner_revision>=1),
        issuer_link_revision BIGINT CHECK (issuer_link_revision>=1),
        capabilities TEXT[] NOT NULL,
        evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('owner_command','console_migration')),
        evidence_ref TEXT NOT NULL CHECK (evidence_ref ~ '^[A-Za-z0-9_.:-]{1,128}$'),
        revision BIGINT NOT NULL CHECK (revision>=1),
        state TEXT NOT NULL CHECK (state IN ('active','revoked')),
        issued_at BIGINT NOT NULL CHECK (issued_at>=0),
        expires_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL CHECK (updated_at>=issued_at),
        revoked_at BIGINT,
        FOREIGN KEY (project_id,project_scope_key) REFERENCES projects(project_id,scope_key),
        FOREIGN KEY (endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
        FOREIGN KEY (owner_grant_id,account_key) REFERENCES qq_official_owner_grants(grant_id,account_key),
        CHECK (cardinality(capabilities) BETWEEN 1 AND 5
            AND array_position(capabilities,NULL) IS NULL
            AND capabilities <@ ARRAY['project.read','project.write','project.execute','project.manage','project.deliver']::TEXT[]),
        CHECK (expires_at>issued_at AND expires_at-issued_at<=86400),
        CHECK ((state='active' AND revoked_at IS NULL)
            OR (state='revoked' AND revoked_at IS NOT NULL AND revoked_at>=issued_at)),
        UNIQUE(project_id,endpoint_key,actor_key),
        UNIQUE(grant_id,project_id), UNIQUE(grant_id,account_key),
        UNIQUE(grant_id,endpoint_key), UNIQUE(grant_id,actor_key)
    )
    """,
    """CREATE INDEX ix_qq_project_grant_owner ON qq_official_project_grants(owner_grant_id,state)
    """,
    """ALTER TABLE project_task_bindings ADD CONSTRAINT uq_project_task_authority_source UNIQUE(task_id,project_id)
    """,
    """CREATE TABLE qq_official_project_task_authority (
        task_id BIGINT PRIMARY KEY,
        project_id BIGINT NOT NULL,
        grant_id TEXT NOT NULL,
        grant_revision BIGINT NOT NULL CHECK (grant_revision>=1),
        account_key TEXT NOT NULL,
        endpoint_key TEXT NOT NULL,
        actor_key TEXT NOT NULL,
        route_epoch BIGINT NOT NULL CHECK (route_epoch>=0),
        endpoint_route_epoch BIGINT NOT NULL CHECK (endpoint_route_epoch>=1),
        created_at BIGINT NOT NULL CHECK (created_at>=0),
        UNIQUE(task_id,project_id),
        FOREIGN KEY(task_id,project_id) REFERENCES project_task_bindings(task_id,project_id),
        FOREIGN KEY(task_id,endpoint_key,actor_key) REFERENCES subagent_tasks(task_id,endpoint_key,requester_actor_key),
        FOREIGN KEY(grant_id,project_id) REFERENCES qq_official_project_grants(grant_id,project_id),
        FOREIGN KEY(grant_id,account_key) REFERENCES qq_official_project_grants(grant_id,account_key),
        FOREIGN KEY(grant_id,endpoint_key) REFERENCES qq_official_project_grants(grant_id,endpoint_key),
        FOREIGN KEY(grant_id,actor_key) REFERENCES qq_official_project_grants(grant_id,actor_key)
    )
    """,
    """ALTER TABLE subagent_tasks DROP CONSTRAINT ck_subagent_identity_union
    """,
    """ALTER TABLE subagent_tasks ADD CONSTRAINT ck_subagent_identity_union CHECK (
        (requester_user_id IS NOT NULL AND account_key IS NULL
            AND endpoint_key IS NULL AND requester_actor_key IS NULL)
        OR (requester_user_id IS NULL AND account_key IS NOT NULL
            AND endpoint_key IS NOT NULL AND requester_actor_key IS NOT NULL
            AND trigger_message_id IS NOT NULL))
    """,
    """ALTER TABLE subagent_tasks ADD COLUMN official_project_id BIGINT
        GENERATED ALWAYS AS (CASE WHEN account_key IS NOT NULL THEN project_id ELSE NULL END) STORED
    """,
    """ALTER TABLE subagent_tasks ADD CONSTRAINT fk_subagent_official_project_authority
        FOREIGN KEY(task_id,official_project_id)
        REFERENCES qq_official_project_task_authority(task_id,project_id)
        DEFERRABLE INITIALLY DEFERRED
    """,
    """ALTER TABLE admin_accounts ADD COLUMN identity_mode TEXT NOT NULL DEFAULT 'legacy_qq'
    """,
    """ALTER TABLE admin_accounts DROP CONSTRAINT admin_accounts_check
    """,
    """ALTER TABLE admin_accounts ADD CONSTRAINT ck_admin_identity_transport CHECK (
        (identity_mode='legacy_qq' AND (role<>'admin' OR qq_id IS NOT NULL))
        OR (identity_mode='official_c2c' AND role='admin' AND qq_id IS NULL))
    """,
    """ALTER TABLE qq_official_owner_grants ADD CONSTRAINT uq_qq_owner_grant_actor_m5 UNIQUE(grant_id,c2c_actor_key)
    """,
    """CREATE TABLE qq_official_admin_bindings (
        binding_id TEXT PRIMARY KEY CHECK (binding_id ~ '^[0-9a-f]{32}$'),
        admin_account_id TEXT NOT NULL REFERENCES admin_accounts(account_id),
        admin_account_version BIGINT NOT NULL CHECK (admin_account_version>=1),
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        c2c_endpoint_key TEXT NOT NULL,
        c2c_actor_key TEXT NOT NULL CHECK (octet_length(c2c_actor_key) BETWEEN 1 AND 1280),
        owner_grant_id TEXT NOT NULL,
        owner_revision BIGINT NOT NULL CHECK (owner_revision>=1),
        revision BIGINT NOT NULL CHECK (revision>=1),
        state TEXT NOT NULL CHECK (state IN ('active','revoked')),
        created_at BIGINT NOT NULL CHECK (created_at>=0),
        updated_at BIGINT NOT NULL CHECK (updated_at>=created_at),
        revoked_at BIGINT,
        FOREIGN KEY(c2c_endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
        FOREIGN KEY(owner_grant_id,account_key) REFERENCES qq_official_owner_grants(grant_id,account_key),
        FOREIGN KEY(owner_grant_id,c2c_actor_key) REFERENCES qq_official_owner_grants(grant_id,c2c_actor_key),
        CHECK ((state='active' AND revoked_at IS NULL)
            OR (state='revoked' AND revoked_at IS NOT NULL AND revoked_at>=created_at)),
        UNIQUE(account_key,c2c_actor_key), UNIQUE(admin_account_id,account_key),
        UNIQUE(binding_id,admin_account_id), UNIQUE(binding_id,account_key),
        UNIQUE(binding_id,c2c_endpoint_key), UNIQUE(binding_id,c2c_actor_key)
    )
    """,
    """ALTER TABLE admin_approvals ADD COLUMN identity_transport TEXT NOT NULL DEFAULT 'onebot'
    """,
    """ALTER TABLE admin_approvals ALTER COLUMN qq_id DROP NOT NULL
    """,
    """ALTER TABLE admin_approvals ALTER COLUMN bot_id DROP NOT NULL
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_account_key TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_endpoint_key TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_actor_key TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_admin_binding_id TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_binding_revision BIGINT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_source_endpoint_key TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_source_actor_key TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_source_scope_key TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_execution_id TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_task_id BIGINT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_source_revision BIGINT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_route_epoch BIGINT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_endpoint_route_epoch BIGINT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_source_deadline_at BIGINT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_policy_json TEXT
    """,
    """ALTER TABLE admin_approvals ADD COLUMN official_policy_digest TEXT
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT ck_admin_approval_transport CHECK (
        (identity_transport='onebot' AND qq_id IS NOT NULL AND bot_id IS NOT NULL AND official_account_key IS NULL AND official_endpoint_key IS NULL AND official_actor_key IS NULL AND official_admin_binding_id IS NULL AND official_binding_revision IS NULL AND official_source_endpoint_key IS NULL AND official_source_actor_key IS NULL AND official_source_scope_key IS NULL AND official_execution_id IS NULL AND official_task_id IS NULL AND official_source_revision IS NULL AND official_route_epoch IS NULL AND official_endpoint_route_epoch IS NULL AND official_source_deadline_at IS NULL AND official_policy_json IS NULL AND official_policy_digest IS NULL) OR
        (identity_transport='qq_official' AND qq_id IS NULL AND bot_id IS NULL
            AND ((kind='server_task' AND quorum=2) OR (kind='quorum_vote' AND quorum=1)) AND official_account_key IS NOT NULL AND official_endpoint_key IS NOT NULL AND official_actor_key IS NOT NULL AND official_admin_binding_id IS NOT NULL AND official_binding_revision IS NOT NULL AND official_source_endpoint_key IS NOT NULL AND official_source_actor_key IS NOT NULL AND official_source_scope_key IS NOT NULL AND official_source_revision IS NOT NULL AND official_route_epoch IS NOT NULL AND official_endpoint_route_epoch IS NOT NULL AND official_source_deadline_at IS NOT NULL AND official_policy_json IS NOT NULL AND official_policy_digest IS NOT NULL
            AND ((official_execution_id IS NOT NULL AND official_task_id IS NULL)
                OR (official_execution_id IS NULL AND official_task_id IS NOT NULL))
            AND official_binding_revision>=1 AND official_source_revision>=1
            AND official_route_epoch>=0 AND official_endpoint_route_epoch>=1
            AND official_source_deadline_at>=created_at
            AND octet_length(official_source_scope_key) BETWEEN 1 AND 1000
            AND octet_length(official_policy_json) BETWEEN 1 AND 16384
            AND official_policy_digest ~ '^[a-f0-9]{64}$'))
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT fk_admin_official_binding_account
        FOREIGN KEY(official_admin_binding_id,account_id)
        REFERENCES qq_official_admin_bindings(binding_id,admin_account_id)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT fk_admin_official_binding_bot
        FOREIGN KEY(official_admin_binding_id,official_account_key)
        REFERENCES qq_official_admin_bindings(binding_id,account_key)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT fk_admin_official_binding_endpoint
        FOREIGN KEY(official_admin_binding_id,official_endpoint_key)
        REFERENCES qq_official_admin_bindings(binding_id,c2c_endpoint_key)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT fk_admin_official_binding_actor
        FOREIGN KEY(official_admin_binding_id,official_actor_key)
        REFERENCES qq_official_admin_bindings(binding_id,c2c_actor_key)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT fk_admin_official_source_endpoint
        FOREIGN KEY(official_source_endpoint_key,official_account_key)
        REFERENCES qq_official_endpoints(endpoint_key,account_key)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT fk_admin_official_source_execution
        FOREIGN KEY(official_execution_id,official_source_endpoint_key,official_source_actor_key)
        REFERENCES qq_official_turn_executions(execution_id,endpoint_key,requester_actor_key)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT fk_admin_official_source_task
        FOREIGN KEY(official_task_id,official_source_endpoint_key,official_source_actor_key)
        REFERENCES subagent_tasks(task_id,endpoint_key,requester_actor_key)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT uq_admin_official_source_recipient
        UNIQUE(approval_id,official_account_key,official_endpoint_key)
    """,
    """ALTER TABLE admin_approvals ADD CONSTRAINT uq_admin_official_source_actor
        UNIQUE(approval_id,official_actor_key)
    """,
    """CREATE TABLE qq_official_admin_otp_sources (
        source_id TEXT PRIMARY KEY CHECK(source_id ~ '^[a-f0-9]{32}$'),
        approval_id TEXT NOT NULL REFERENCES admin_approvals(approval_id),
        code_generation BIGINT NOT NULL CHECK(code_generation>=1),
        account_key TEXT NOT NULL,
        endpoint_key TEXT NOT NULL,
        actor_key TEXT NOT NULL,
        window_id TEXT NOT NULL,
        account_epoch BIGINT NOT NULL CHECK(account_epoch>=1),
        body_commitment TEXT NOT NULL CHECK(body_commitment ~ '^[a-f0-9]{64}$'),
        approval_expires_at BIGINT NOT NULL,
        created_at BIGINT NOT NULL CHECK(created_at>=0),
        expires_at BIGINT NOT NULL CHECK(expires_at>created_at AND expires_at-created_at<=30),
        plan_id TEXT UNIQUE,
        delivery_id BIGINT UNIQUE,
        attempt_id BIGINT UNIQUE,
        cancelled_at BIGINT,
        UNIQUE(approval_id,code_generation),
        UNIQUE(source_id,account_key,endpoint_key), UNIQUE(source_id,actor_key), UNIQUE(source_id,expires_at),
        FOREIGN KEY(approval_id,account_key,endpoint_key)
            REFERENCES admin_approvals(approval_id,official_account_key,official_endpoint_key),
        FOREIGN KEY(approval_id,actor_key) REFERENCES admin_approvals(approval_id,official_actor_key),
        FOREIGN KEY(window_id,account_key,endpoint_key)
            REFERENCES qq_official_reply_windows(window_id,account_key,endpoint_key),
        FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES delivery_plans(plan_id,account_key,endpoint_key),
        FOREIGN KEY(plan_id,actor_key) REFERENCES delivery_plans(plan_id,requester_actor_key),
        FOREIGN KEY(delivery_id,plan_id) REFERENCES deliveries(delivery_id,plan_id),
        FOREIGN KEY(attempt_id,delivery_id,account_key,endpoint_key)
            REFERENCES delivery_attempts(attempt_id,delivery_id,account_key,endpoint_key),
        CHECK(expires_at<=approval_expires_at),
        CHECK((plan_id IS NULL)=(delivery_id IS NULL)),
        CHECK(attempt_id IS NULL OR plan_id IS NOT NULL)
    )
    """,
    """CREATE INDEX ix_admin_official_queue ON admin_approvals(identity_transport,official_account_key,status,created_at)
    """,
    """ALTER TABLE delivery_plans ADD COLUMN approval_source_id TEXT UNIQUE
    """,
    """ALTER TABLE delivery_plans ADD COLUMN approval_source_expires_at BIGINT
    """,
    """ALTER TABLE delivery_plans ADD CONSTRAINT fk_plan_approval_source_endpoint FOREIGN KEY(approval_source_id,account_key,endpoint_key) REFERENCES qq_official_admin_otp_sources(source_id,account_key,endpoint_key)
    """,
    """ALTER TABLE delivery_plans ADD CONSTRAINT fk_plan_approval_source_actor FOREIGN KEY(approval_source_id,requester_actor_key) REFERENCES qq_official_admin_otp_sources(source_id,actor_key)
    """,
    """ALTER TABLE delivery_plans ADD CONSTRAINT fk_plan_approval_source_expiry FOREIGN KEY(approval_source_id,approval_source_expires_at) REFERENCES qq_official_admin_otp_sources(source_id,expires_at)
    """,
    """ALTER TABLE delivery_plans DROP CONSTRAINT delivery_plans_purpose_check
    """,
    """ALTER TABLE delivery_plans ADD CONSTRAINT delivery_plans_purpose_check CHECK(purpose IN ('final','requested_artifact','background_result','progress','unsolicited','binding_otp','approval_otp'))
    """,
    """ALTER TABLE deliveries DROP CONSTRAINT ck_deliveries_official_fields
    """,
    """ALTER TABLE deliveries ADD CONSTRAINT ck_deliveries_official_fields CHECK (account_key IS NULL OR (
        octet_length(requester_actor_key) BETWEEN 1 AND 1280
        AND part_index BETWEEN 0 AND 15 AND endpoint_generation>=1 AND route_epoch>=1 AND capability_revision>=1
        AND purpose IN ('final','requested_artifact','background_result','progress','unsolicited','binding_otp','approval_otp')
        AND payload_mode IN ('persistent','ephemeral','protected') AND variant IN ('primary','plain_fallback')
        AND payload_digest ~ '^[a-f0-9]{64}$' AND digest_scheme IN ('sha256','hmac_sha256_v1')
        AND ((digest_scheme='sha256' AND digest_key_id IS NULL)
            OR (digest_scheme='hmac_sha256_v1' AND digest_key_id ~ '^[A-Za-z0-9_-]{1,64}$'))
        AND expires_at>created_at AND auth_retries_used BETWEEN 0 AND 1 AND transient_retries_used>=0
        AND (wait_reason IS NULL OR wait_reason ~ '^[a-z0-9_]{1,80}$')
        AND (claim_epoch IS NULL OR claim_epoch>0)
        AND ((claim_epoch IS NULL)=(current_attempt_id IS NULL))
        AND (status NOT IN ('sending','accepted','committed','ambiguous') OR current_attempt_id IS NOT NULL)
        AND (status<>'committed' OR octet_length(native_message_id) BETWEEN 1 AND 256)
        AND ((variant='plain_fallback')=format_fallback_used)))
    """,
    """ALTER TABLE deliveries DROP CONSTRAINT ck_deliveries_ephemeral
    """,
    """ALTER TABLE deliveries ADD CONSTRAINT ck_deliveries_ephemeral CHECK (account_key IS NULL OR (
        (payload_mode IN ('persistent','protected') AND purpose NOT IN ('binding_otp','approval_otp') AND digest_scheme='sha256')
        OR (payload_mode='ephemeral' AND purpose IN ('binding_otp','approval_otp') AND part_index=0
            AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL
            AND body_json='{"v":1,"nodes":[]}' AND content_fingerprint=''
            AND body_archive_path IS NULL AND body_archive_sha256 IS NULL
            AND body_size_bytes IS NULL AND body_archived_at IS NULL
            AND auth_retries_used=0 AND transient_retries_used=0 AND NOT format_fallback_used
            AND last_error='')))
    """,
    """ALTER TABLE delivery_plans DROP CONSTRAINT delivery_plans_check2
    """,
    """ALTER TABLE delivery_plans ADD CONSTRAINT delivery_plans_check2 CHECK (
    (purpose='binding_otp' AND payload_mode='ephemeral' AND digest_scheme='hmac_sha256_v1'
      AND digest_key_id IS NOT NULL AND part_count=1 AND challenge_id IS NOT NULL AND challenge_expires_at IS NOT NULL
      AND expires_at<=challenge_expires_at AND approval_source_id IS NULL AND approval_source_expires_at IS NULL) OR
    (purpose='approval_otp' AND payload_mode='ephemeral' AND digest_scheme='hmac_sha256_v1'
      AND digest_key_id IS NOT NULL AND part_count=1 AND challenge_id IS NULL AND challenge_expires_at IS NULL
      AND approval_source_id IS NOT NULL AND approval_source_expires_at IS NOT NULL AND expires_at=approval_source_expires_at) OR
    (purpose NOT IN ('binding_otp','approval_otp') AND payload_mode IN ('persistent','protected') AND digest_scheme='sha256'
      AND digest_key_id IS NULL AND challenge_id IS NULL AND challenge_expires_at IS NULL
      AND approval_source_id IS NULL AND approval_source_expires_at IS NULL))
    """,
    """ALTER TABLE qq_official_inbound_events ADD COLUMN admin_control_actor_key TEXT CHECK(octet_length(admin_control_actor_key) BETWEEN 1 AND 1280)
    """,
    """ALTER TABLE qq_official_inbound_events ADD COLUMN admin_control_commitment TEXT CHECK(admin_control_commitment ~ '^[a-f0-9]{64}$')
    """,
    """ALTER TABLE qq_official_inbound_events ADD CONSTRAINT ck_admin_control_private_source CHECK(
   (admin_control_actor_key IS NULL AND admin_control_commitment IS NULL) OR
   (admin_control_actor_key IS NOT NULL AND admin_control_commitment IS NOT NULL AND endpoint_key IS NOT NULL
     AND native_message_id IS NOT NULL AND event_type='C2C_MESSAGE_CREATE' AND echo_state='not_echo'
     AND reason_code='admin_control' AND status='processed' AND payload_json IS NULL
     AND turn_text IS NULL AND turn_text_version IS NULL AND control_actor_key IS NULL))
    """,
    """ALTER TABLE qq_official_inbound_events ADD CONSTRAINT uq_admin_control_source_actor UNIQUE(inbox_id,endpoint_key,admin_control_actor_key)
    """,
    """CREATE TABLE qq_official_admin_control_receipts(
    inbox_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
    source_session_id TEXT NOT NULL, source_seq BIGINT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('confirm','cancel','resend','review','revoke_review','status','invalid')),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','denied','cancelled','sending','status')),
    approval_id TEXT REFERENCES admin_approvals(approval_id),
    created_at BIGINT NOT NULL CHECK(created_at>=0),
    FOREIGN KEY(inbox_id,account_key,endpoint_key) REFERENCES qq_official_inbound_events(inbox_id,account_key,endpoint_key),
    FOREIGN KEY(inbox_id,endpoint_key,actor_key) REFERENCES qq_official_inbound_events(inbox_id,endpoint_key,admin_control_actor_key),
    FOREIGN KEY(account_key,source_session_id,source_seq,inbox_id) REFERENCES qq_official_gateway_receipts(account_key,session_id,seq,inbox_id)
 )
    """,
    """ALTER TABLE reminders ADD COLUMN account_key TEXT,
 ADD COLUMN endpoint_key TEXT, ADD COLUMN creator_actor_key TEXT,
 ADD COLUMN endpoint_generation BIGINT, ADD COLUMN endpoint_route_epoch BIGINT
    """,
    """ALTER TABLE agent_routines ADD COLUMN account_key TEXT,
 ADD COLUMN endpoint_key TEXT, ADD COLUMN creator_actor_key TEXT,
 ADD COLUMN endpoint_generation BIGINT, ADD COLUMN endpoint_route_epoch BIGINT
    """,
    """ALTER TABLE agent_routines ADD COLUMN budget_origin_routine_id BIGINT REFERENCES agent_routines(routine_id)
    """,
    """ALTER TABLE agent_routines DROP CONSTRAINT ck_routine_scope
    """,
    """ALTER TABLE agent_routines ADD CONSTRAINT ck_routine_scope CHECK
 ((platform='onebot-v11' AND kind IN ('group','private') AND account_key IS NULL
   AND endpoint_key IS NULL AND creator_actor_key IS NULL AND endpoint_generation IS NULL
   AND endpoint_route_epoch IS NULL)
 OR (platform='qq-official' AND kind IN ('group','private') AND account_key IS NOT NULL
   AND endpoint_key IS NOT NULL AND creator_actor_key IS NOT NULL
   AND endpoint_generation IS NOT NULL AND endpoint_route_epoch IS NOT NULL
   AND endpoint_generation>0 AND endpoint_route_epoch>0))
    """,
    """ALTER TABLE reminders ADD CONSTRAINT ck_reminder_identity CHECK
 ((platform<>'qq-official' AND account_key IS NULL AND endpoint_key IS NULL
   AND creator_actor_key IS NULL AND endpoint_generation IS NULL AND endpoint_route_epoch IS NULL)
 OR (platform='qq-official' AND account_key IS NOT NULL AND endpoint_key IS NOT NULL
   AND creator_actor_key IS NOT NULL AND endpoint_generation IS NOT NULL AND endpoint_route_epoch IS NOT NULL
   AND endpoint_generation>0 AND endpoint_route_epoch>0))
    """,
    """ALTER TABLE reminders ADD FOREIGN KEY(endpoint_key,account_key)
 REFERENCES qq_official_endpoints(endpoint_key,account_key)
    """,
    """ALTER TABLE agent_routines ADD FOREIGN KEY(endpoint_key,account_key)
 REFERENCES qq_official_endpoints(endpoint_key,account_key)
    """,
    """CREATE TABLE qq_official_schedule_bindings (
 binding_id TEXT PRIMARY KEY CHECK(binding_id ~ '^[0-9a-f]{32}$'),
 item_id TEXT NOT NULL UNIQUE, item_revision BIGINT NOT NULL CHECK(item_revision>0),
 export_digest TEXT NOT NULL CHECK(export_digest ~ '^[0-9a-f]{64}$'),
 parent_binding_id TEXT REFERENCES qq_official_schedule_bindings(binding_id),
 source_kind TEXT NOT NULL CHECK(source_kind IN ('reminder','routine')),
 source_key TEXT NOT NULL, source_revision BIGINT NOT NULL CHECK(source_revision>0), source_scope_key TEXT NOT NULL,
 source_canonical_message_id BIGINT, source_native_message_id TEXT,
 target_reminder_id BIGINT UNIQUE REFERENCES reminders(reminder_id),
 target_routine_id BIGINT REFERENCES agent_routines(routine_id),
 target_run_id TEXT UNIQUE REFERENCES routine_runs(run_id),
 account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
 logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=0),
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>0),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>0),
 due_at BIGINT NOT NULL CHECK(due_at>=0),
 execution_deadline BIGINT NOT NULL, result_deadline BIGINT NOT NULL,
 job_id BIGINT UNIQUE REFERENCES durable_jobs(job_id),
 plan_id TEXT UNIQUE REFERENCES delivery_plans(plan_id),
 activated_at BIGINT, created_at BIGINT NOT NULL,
 CHECK(due_at<result_deadline AND execution_deadline<=result_deadline),
 CHECK((source_kind='reminder' AND target_reminder_id IS NOT NULL
        AND target_routine_id IS NULL AND target_run_id IS NULL)
    OR (source_kind='routine' AND target_reminder_id IS NULL
        AND target_routine_id IS NOT NULL)),
 FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key)
)
    """,
    """ALTER TABLE subagent_tasks ADD COLUMN migration_item_id TEXT REFERENCES migration_items(item_id)
    """,
    """ALTER TABLE subagent_tasks ADD CONSTRAINT uq_subagent_migration_source UNIQUE(task_id,migration_item_id)
    """,
    """ALTER TABLE subagent_tasks DROP CONSTRAINT ck_subagent_identity_union
    """,
    """ALTER TABLE subagent_tasks ADD CONSTRAINT ck_subagent_identity_union CHECK (
 (requester_user_id IS NOT NULL AND account_key IS NULL AND endpoint_key IS NULL
  AND requester_actor_key IS NULL AND migration_item_id IS NULL)
 OR (requester_user_id IS NULL AND account_key IS NOT NULL AND endpoint_key IS NOT NULL
  AND requester_actor_key IS NOT NULL AND project_id IS NULL
  AND ((trigger_message_id IS NOT NULL AND migration_item_id IS NULL)
    OR (trigger_message_id IS NULL AND migration_item_id IS NOT NULL))))
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD COLUMN migration_item_id TEXT UNIQUE REFERENCES migration_items(item_id)
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD COLUMN migration_result_deadline_at BIGINT
    """,
    """ALTER TABLE qq_official_subagent_dispatches ALTER COLUMN source_execution_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_subagent_dispatches ALTER COLUMN source_canonical_message_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_subagent_dispatches ALTER COLUMN source_native_message_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD CONSTRAINT ck_subagent_dispatch_source CHECK (
 (migration_item_id IS NULL AND migration_result_deadline_at IS NULL AND source_execution_id IS NOT NULL
  AND source_canonical_message_id IS NOT NULL AND source_native_message_id IS NOT NULL)
 OR (migration_item_id IS NOT NULL AND migration_result_deadline_at IS NOT NULL
  AND migration_result_deadline_at>created_at AND migration_result_deadline_at<=created_at+86400
  AND migration_result_deadline_at>=execution_deadline_at
  AND (result_expires_at IS NULL OR result_expires_at<=migration_result_deadline_at)
  AND source_execution_id IS NULL AND source_canonical_message_id IS NULL AND source_native_message_id IS NULL))
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD CONSTRAINT fk_subagent_dispatch_migration
 FOREIGN KEY(task_id,migration_item_id) REFERENCES subagent_tasks(task_id,migration_item_id)
    """,
    """CREATE TABLE migration_subagent_continuations (
 item_id TEXT PRIMARY KEY REFERENCES migration_items(item_id),
 target_task_id BIGINT NOT NULL UNIQUE,
 source_task_id BIGINT NOT NULL REFERENCES subagent_tasks(task_id),
 source_revision BIGINT NOT NULL CHECK(source_revision=1),
 source_control_version BIGINT NOT NULL CHECK(source_control_version>=1),
 source_frozen_digest TEXT NOT NULL CHECK(source_frozen_digest ~ '^[a-f0-9]{64}$'),
 export_digest TEXT NOT NULL CHECK(export_digest ~ '^[a-f0-9]{64}$'),
 account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>=1),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>=1),
 logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=1),
 mapping_id TEXT NOT NULL, mapping_revision BIGINT NOT NULL CHECK(mapping_revision>=1),
 job_id BIGINT NOT NULL UNIQUE REFERENCES durable_jobs(job_id),
 execution_deadline BIGINT NOT NULL, result_deadline BIGINT NOT NULL,
 imported_terminal BOOLEAN NOT NULL, profile TEXT NOT NULL,
 execution_job_generation BIGINT CHECK(execution_job_generation>=1),
 execution_job_owner TEXT,
 CHECK ((execution_job_generation IS NULL)=(execution_job_owner IS NULL)),
 plan_id TEXT UNIQUE REFERENCES delivery_plans(plan_id), created_at BIGINT NOT NULL,
 FOREIGN KEY(target_task_id,item_id) REFERENCES subagent_tasks(task_id,migration_item_id),
 FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
 FOREIGN KEY(target_task_id,endpoint_key,actor_key) REFERENCES subagent_tasks(task_id,endpoint_key,requester_actor_key),
 CHECK(target_task_id<>source_task_id),
 CHECK(result_deadline>created_at AND result_deadline<=created_at+86400),
 CHECK(execution_deadline<=result_deadline),
 CHECK(imported_terminal OR execution_deadline>created_at)
)
    """,
    """CREATE TABLE migration_subagent_run_provenance (
 item_id TEXT NOT NULL REFERENCES migration_subagent_continuations(item_id),
 source_run_id BIGINT NOT NULL REFERENCES subagent_runs(run_id),
 target_run_id BIGINT NOT NULL UNIQUE REFERENCES subagent_runs(run_id),
 source_checkpoint_digest TEXT NOT NULL CHECK(source_checkpoint_digest ~ '^[a-f0-9]{64}$'),
 PRIMARY KEY(item_id,source_run_id), CHECK(source_run_id<>target_run_id)
)
    """,
    """ALTER TABLE qq_official_subagent_dispatches DROP CONSTRAINT qq_official_subagent_dispatches_check1
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD CONSTRAINT ck_subagent_execution_deadline CHECK (execution_deadline_at>created_at OR migration_item_id IS NOT NULL)
    """,
    """ALTER TABLE subagent_tasks DROP CONSTRAINT ck_subagent_identity_union
    """,
    """ALTER TABLE subagent_tasks ADD CONSTRAINT ck_subagent_identity_union CHECK (
          (requester_user_id IS NOT NULL AND account_key IS NULL AND endpoint_key IS NULL
            AND requester_actor_key IS NULL AND migration_item_id IS NULL)
          OR (requester_user_id IS NULL AND account_key IS NOT NULL AND endpoint_key IS NOT NULL
            AND requester_actor_key IS NOT NULL
            AND ((trigger_message_id IS NOT NULL AND migration_item_id IS NULL)
              OR (trigger_message_id IS NULL AND migration_item_id IS NOT NULL AND project_id IS NULL))))
    """,
    """ALTER TABLE qq_official_subagent_dispatches DROP CONSTRAINT qq_official_subagent_dispatches_tool_profile_check
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD CONSTRAINT qq_official_subagent_dispatches_tool_profile_check
          CHECK (tool_profile IN ('official-text-v1','official-files-v1','official-projects-v1','official-host-v1')
            AND (migration_item_id IS NULL OR tool_profile='official-text-v1'))
    """,
    """ALTER TABLE delivery_plans ADD CONSTRAINT ck_approval_ephemeral_manifest
          CHECK (purpose NOT IN ('binding_otp','approval_otp') OR manifest_json='{"v":1,"ephemeral":true}')
    """,
    """ALTER TABLE project_lifecycle_runs ADD CONSTRAINT uq_project_lifecycle_authority_job UNIQUE(job_id,project_id)
    """,
    """CREATE TABLE qq_official_project_lifecycle_authority (
        job_id BIGINT PRIMARY KEY REFERENCES project_lifecycle_runs(job_id),
        project_id BIGINT NOT NULL REFERENCES projects(project_id),
        grant_id TEXT NOT NULL,
        grant_revision BIGINT NOT NULL CHECK(grant_revision>0),
        account_key TEXT NOT NULL,
        endpoint_key TEXT NOT NULL,
        actor_key TEXT NOT NULL,
        principal_id BIGINT NOT NULL REFERENCES principals(principal_id),
        endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>0),
        endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>0),
        route_epoch BIGINT NOT NULL CHECK(route_epoch>=0),
        source_execution_id TEXT REFERENCES qq_official_turn_executions(execution_id),
        source_task_id BIGINT REFERENCES subagent_tasks(task_id),
        source_revision BIGINT NOT NULL CHECK(source_revision>0),
        source_digest TEXT NOT NULL CHECK(source_digest ~ '^[a-f0-9]{64}$'),
        action TEXT NOT NULL,
        deadline_at BIGINT NOT NULL,
        created_at BIGINT NOT NULL,
        CHECK(num_nonnulls(source_execution_id,source_task_id)=1),
        CHECK(deadline_at>created_at AND deadline_at-created_at<=3600),
        FOREIGN KEY(job_id,project_id) REFERENCES project_lifecycle_runs(job_id,project_id),
        FOREIGN KEY(source_execution_id,endpoint_key,actor_key)
            REFERENCES qq_official_turn_executions(execution_id,endpoint_key,requester_actor_key),
        FOREIGN KEY(source_task_id,endpoint_key,actor_key)
            REFERENCES subagent_tasks(task_id,endpoint_key,requester_actor_key),
        FOREIGN KEY(grant_id,project_id) REFERENCES qq_official_project_grants(grant_id,project_id),
        FOREIGN KEY(grant_id,account_key) REFERENCES qq_official_project_grants(grant_id,account_key),
        FOREIGN KEY(grant_id,endpoint_key) REFERENCES qq_official_project_grants(grant_id,endpoint_key),
        FOREIGN KEY(grant_id,actor_key) REFERENCES qq_official_project_grants(grant_id,actor_key)
    )
    """,
    """ALTER TABLE migration_work_sources DROP CONSTRAINT migration_work_sources_source_kind_check
    """,
    """ALTER TABLE migration_work_sources ADD CONSTRAINT migration_work_source_kind CHECK (source_kind IN ('handler','job','delivery','reminder','approval','maintenance','subagent','routine'))
    """,
    """ALTER TABLE migration_work_sources ADD COLUMN parent_source_id TEXT
    """,
    """ALTER TABLE migration_work_sources ADD COLUMN task_id BIGINT REFERENCES subagent_tasks(task_id)
    """,
    """ALTER TABLE migration_work_sources ADD COLUMN routine_id BIGINT REFERENCES agent_routines(routine_id)
    """,
    """ALTER TABLE migration_work_sources ADD CONSTRAINT migration_work_source_domain CHECK ((source_kind='subagent')=(task_id IS NOT NULL) AND (source_kind='routine')=(routine_id IS NOT NULL))
    """,
    """ALTER TABLE migration_work_sources ADD CONSTRAINT migration_work_source_parent_account FOREIGN KEY(parent_source_id,account_key) REFERENCES migration_work_sources(source_id,account_key)
    """,
    """ALTER TABLE migration_work_sources ADD CONSTRAINT migration_work_source_parent_endpoint FOREIGN KEY(parent_source_id,endpoint_key) REFERENCES migration_work_sources(source_id,endpoint_key)
    """,
    """ALTER TABLE migration_work_sources ADD CONSTRAINT migration_work_source_scope_unique UNIQUE(source_id,canonical_scope_key)
    """,
    """ALTER TABLE migration_work_sources ADD CONSTRAINT migration_work_source_parent_scope FOREIGN KEY(parent_source_id,canonical_scope_key) REFERENCES migration_work_sources(source_id,canonical_scope_key)
    """,
    """CREATE UNIQUE INDEX migration_work_source_task_revision ON migration_work_sources(task_id,source_revision) WHERE task_id IS NOT NULL
    """,
    """CREATE UNIQUE INDEX migration_work_source_routine_revision ON migration_work_sources(routine_id,source_revision) WHERE routine_id IS NOT NULL
    """,
    """CREATE FUNCTION migration_work_source_parent_check() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE p migration_work_sources%ROWTYPE;
    BEGIN
      IF NEW.parent_source_id IS NOT NULL THEN
        SELECT * INTO STRICT p FROM migration_work_sources WHERE source_id=NEW.parent_source_id;
        IF p.actor_key IS DISTINCT FROM NEW.actor_key OR p.route_epoch<>NEW.route_epoch
          OR p.deadline_at<NEW.deadline_at OR p.source_id=NEW.source_id THEN
          RAISE EXCEPTION 'migration_work_source_parent_mismatch';
        END IF;
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER migration_work_source_parent_guard BEFORE INSERT OR UPDATE OF parent_source_id,actor_key,route_epoch,deadline_at ON migration_work_sources FOR EACH ROW EXECUTE FUNCTION migration_work_source_parent_check()
    """,
    """ALTER TABLE qq_official_file_artifacts ADD COLUMN source_extension_kind TEXT CHECK(source_extension_kind='project')
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT uq_project_artifact_kind UNIQUE(artifact_id,source_extension_kind)
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT uq_project_artifact_task UNIQUE(artifact_id,source_task_id)
    """,
    """ALTER TABLE project_io ADD CONSTRAINT uq_project_artifact_io UNIQUE(operation_id,project_id,actor_principal_id)
    """,
    """ALTER TABLE qq_official_project_grants ADD CONSTRAINT uq_project_artifact_principal UNIQUE(grant_id,principal_id)
    """,
    """CREATE TABLE qq_official_project_artifacts (
      artifact_id TEXT PRIMARY KEY,
      source_extension_kind TEXT NOT NULL DEFAULT 'project' CHECK(source_extension_kind='project'),
      project_id BIGINT NOT NULL REFERENCES projects(project_id),
      grant_id TEXT NOT NULL,
      grant_revision BIGINT NOT NULL CHECK(grant_revision>0),
      grant_expires_at BIGINT NOT NULL,
      account_key TEXT NOT NULL,
      endpoint_key TEXT NOT NULL,
      actor_key TEXT NOT NULL,
      principal_id BIGINT NOT NULL REFERENCES principals(principal_id),
      logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=0),
      source_kind TEXT NOT NULL CHECK(source_kind IN ('project_io','project_task')),
      source_task_id BIGINT,
      io_operation_id BIGINT UNIQUE,
      io_token_digest TEXT CHECK(io_token_digest ~ '^[a-f0-9]{64}$'),
      source_digest TEXT NOT NULL CHECK(source_digest ~ '^[a-f0-9]{64}$'),
      created_at BIGINT NOT NULL CHECK(created_at>=0),
      CHECK(grant_expires_at>created_at),
      CHECK((source_kind='project_io' AND source_task_id IS NULL AND io_operation_id IS NOT NULL AND io_token_digest IS NOT NULL)
        OR (source_kind='project_task' AND source_task_id IS NOT NULL AND io_operation_id IS NULL AND io_token_digest IS NULL)),
      FOREIGN KEY(artifact_id,source_extension_kind) REFERENCES qq_official_file_artifacts(artifact_id,source_extension_kind),
      FOREIGN KEY(artifact_id,account_key,endpoint_key) REFERENCES qq_official_file_artifacts(artifact_id,account_key,endpoint_key),
      FOREIGN KEY(artifact_id,actor_key) REFERENCES qq_official_file_artifacts(artifact_id,requester_actor_key),
      FOREIGN KEY(artifact_id,source_task_id) REFERENCES qq_official_file_artifacts(artifact_id,source_task_id),
      FOREIGN KEY(io_operation_id,project_id,principal_id) REFERENCES project_io(operation_id,project_id,actor_principal_id),
      FOREIGN KEY(source_task_id,project_id) REFERENCES qq_official_project_task_authority(task_id,project_id),
      FOREIGN KEY(grant_id,project_id) REFERENCES qq_official_project_grants(grant_id,project_id),
      FOREIGN KEY(grant_id,account_key) REFERENCES qq_official_project_grants(grant_id,account_key),
      FOREIGN KEY(grant_id,endpoint_key) REFERENCES qq_official_project_grants(grant_id,endpoint_key),
      FOREIGN KEY(grant_id,actor_key) REFERENCES qq_official_project_grants(grant_id,actor_key),
      FOREIGN KEY(grant_id,principal_id) REFERENCES qq_official_project_grants(grant_id,principal_id)
    )
    """,
    """ALTER TABLE qq_official_file_artifacts ADD COLUMN project_artifact_id TEXT
      GENERATED ALWAYS AS (CASE WHEN source_extension_kind='project' THEN artifact_id ELSE NULL END) STORED
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT fk_artifact_project_provenance
      FOREIGN KEY(project_artifact_id) REFERENCES qq_official_project_artifacts(artifact_id) DEFERRABLE INITIALLY DEFERRED
    """,
    """CREATE FUNCTION qq_project_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'project_artifact_provenance_immutable' USING ERRCODE='23514';
    END $$
    """,
    """CREATE TRIGGER qq_project_artifact_immutable BEFORE UPDATE OR DELETE ON qq_official_project_artifacts
      FOR EACH ROW EXECUTE FUNCTION qq_project_artifact_immutable()
    """,
    """CREATE FUNCTION qq_artifact_extension_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.source_extension_kind IS DISTINCT FROM OLD.source_extension_kind THEN
        RAISE EXCEPTION 'artifact_extension_immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER qq_artifact_extension_immutable BEFORE UPDATE ON qq_official_file_artifacts
      FOR EACH ROW EXECUTE FUNCTION qq_artifact_extension_immutable()
    """,
    """ALTER TABLE migration_actor_mapping_revisions ADD CONSTRAINT uq_migration_mapping_recipient
      UNIQUE(mapping_id,revision,target_actor_key,expires_at)
    """,
    """ALTER TABLE migration_actor_mappings ADD CONSTRAINT uq_migration_mapping_source_actor
      UNIQUE(mapping_id,source_actor_key)
    """,
    """ALTER TABLE migration_items ADD CONSTRAINT fk_migration_item_mapping_revision
      FOREIGN KEY(mapping_id,mapping_revision,target_actor_key,mapping_expires_at)
      REFERENCES migration_actor_mapping_revisions(mapping_id,revision,target_actor_key,expires_at)
    """,
    """ALTER TABLE migration_items ADD CONSTRAINT fk_migration_item_mapping_source
      FOREIGN KEY(mapping_id,source_actor_key) REFERENCES migration_actor_mappings(mapping_id,source_actor_key)
    """,
    """ALTER TABLE migration_subagent_continuations ADD CONSTRAINT fk_subagent_mapping_revision
      FOREIGN KEY(mapping_id,mapping_revision) REFERENCES migration_actor_mapping_revisions(mapping_id,revision)
    """,
    """CREATE FUNCTION migration_mapping_source_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF ROW(NEW.mapping_id,NEW.mapping_key,NEW.source_endpoint_key,NEW.source_scope_key,NEW.source_actor_key,
          NEW.target_account_key,NEW.target_endpoint_key,NEW.created_by_admin,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.mapping_id,OLD.mapping_key,OLD.source_endpoint_key,OLD.source_scope_key,
          OLD.source_actor_key,OLD.target_account_key,OLD.target_endpoint_key,OLD.created_by_admin,OLD.created_at)
        OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'migration_mapping_identity_immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER migration_mapping_source_immutable BEFORE UPDATE ON migration_actor_mappings
      FOR EACH ROW EXECUTE FUNCTION migration_mapping_source_immutable()
    """,
    """CREATE FUNCTION migration_mapping_revision_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'migration_mapping_revision_immutable' USING ERRCODE='23514';
    END $$
    """,
    """CREATE TRIGGER migration_mapping_revision_immutable BEFORE UPDATE OR DELETE ON migration_actor_mapping_revisions
      FOR EACH ROW EXECUTE FUNCTION migration_mapping_revision_immutable()
    """,
    """ALTER TABLE migration_actor_mappings ADD CONSTRAINT fk_migration_mapping_inbox
      FOREIGN KEY(evidence_inbox_id,target_account_key,target_endpoint_key)
      REFERENCES qq_official_inbound_events(inbox_id,account_key,endpoint_key)
    """,
    """ALTER TABLE migration_routes ADD CONSTRAINT fk_migration_target_account
      FOREIGN KEY(target_endpoint_key,target_account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key)
    """,
    """ALTER TABLE migration_routes ADD CONSTRAINT ck_migration_route_snapshot CHECK (
      active_endpoint_key IN (source_endpoint_key,target_endpoint_key)
      AND target_generation>0 AND target_endpoint_epoch>0 AND baseline_revision>0
      AND ((source_generation IS NULL AND source_endpoint_epoch IS NULL)
        OR (source_generation IS NOT NULL AND source_endpoint_epoch IS NOT NULL
          AND source_generation>0 AND source_endpoint_epoch>0)))
    """,
    """ALTER TABLE migration_routes ADD CONSTRAINT uq_migration_route_scopes UNIQUE(route_id,source_scope_key,target_scope_key,target_endpoint_key)
    """,
    """ALTER TABLE migration_journals ADD CONSTRAINT uq_migration_journal_route UNIQUE(migration_id,route_id)
    """,
    """ALTER TABLE migration_journals ADD CONSTRAINT ck_migration_journal_bound CHECK (
      preview_digest ~ '^[a-f0-9]{64}$' AND creator_version>0 AND deadline>created_at
      AND deadline-created_at<=86400 AND created_at>=0
      AND ((approved_by IS NULL AND approved_version IS NULL)
        OR (approved_by IS NOT NULL AND approved_version IS NOT NULL AND approved_version>0))
      AND ((verification_nonce IS NULL AND verification_until IS NULL)
        OR (verification_nonce IS NOT NULL AND verification_until IS NOT NULL AND verification_until<=deadline)))
    """,
    """ALTER TABLE migration_items ADD CONSTRAINT ck_migration_item_bounds CHECK (
      source_revision>0 AND kind IN ('reminder','routine','subagent','project_task','file')
      AND octet_length(source_ref) BETWEEN 1 AND 128 AND octet_length(export_json)<=262144
      AND export_digest ~ '^[a-f0-9]{64}$' AND created_at>=0
      AND (source_canonical_id IS NULL OR source_canonical_id>0)
      AND (execution_deadline IS NULL OR execution_deadline>=0)
      AND (result_deadline IS NULL OR result_deadline>=0)
      AND (execution_deadline IS NULL OR result_deadline IS NULL OR execution_deadline<=result_deadline)
      AND ((job_id IS NULL AND expected_job_kind IS NULL)
        OR (job_id IS NOT NULL AND expected_job_kind IS NOT NULL)))
    """,
    """ALTER TABLE migration_items ADD CONSTRAINT uq_migration_item_kind UNIQUE(item_id,kind)
    """,
    """ALTER TABLE migration_items ADD CONSTRAINT uq_migration_item_job UNIQUE(item_id,job_id)
    """,
    """ALTER TABLE migration_items ADD CONSTRAINT uq_migration_item_parent UNIQUE(item_id,migration_id)
    """,
    """ALTER TABLE migration_items ADD CONSTRAINT fk_migration_item_lineage
      FOREIGN KEY(parent_item_id,migration_id) REFERENCES migration_items(item_id,migration_id)
    """,
    """ALTER TABLE migration_item_plans ADD CONSTRAINT fk_migration_plan_job
      FOREIGN KEY(item_id,source_job_id) REFERENCES migration_items(item_id,job_id)
    """,
    """ALTER TABLE conversations ADD CONSTRAINT uq_migration_history_scope UNIQUE(conversation_id,scope_key)
    """,
    """ALTER TABLE qq_migration_public_history_grants ADD CONSTRAINT fk_public_history_journal
      FOREIGN KEY(migration_id,route_id) REFERENCES migration_journals(migration_id,route_id)
    """,
    """ALTER TABLE qq_migration_public_history_grants ADD CONSTRAINT fk_public_history_route
      FOREIGN KEY(route_id,source_scope_key,target_scope_key,target_endpoint_key)
      REFERENCES migration_routes(route_id,source_scope_key,target_scope_key,target_endpoint_key)
    """,
    """ALTER TABLE qq_migration_public_history_grants ADD CONSTRAINT fk_public_history_source
      FOREIGN KEY(source_conversation_id,source_scope_key) REFERENCES conversations(conversation_id,scope_key)
    """,
    """ALTER TABLE qq_official_schedule_bindings ADD CONSTRAINT fk_schedule_migration_kind
      FOREIGN KEY(item_id,source_kind) REFERENCES migration_items(item_id,kind)
    """,
    """ALTER TABLE qq_official_schedule_bindings ADD CONSTRAINT fk_schedule_migration_job
      FOREIGN KEY(item_id,job_id) REFERENCES migration_items(item_id,job_id) DEFERRABLE INITIALLY DEFERRED
    """,
    """ALTER TABLE qq_official_schedule_bindings ADD CONSTRAINT fk_schedule_plan_endpoint
      FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES delivery_plans(plan_id,account_key,endpoint_key)
    """,
    """ALTER TABLE qq_official_schedule_bindings ADD CONSTRAINT fk_schedule_plan_actor
      FOREIGN KEY(plan_id,actor_key) REFERENCES delivery_plans(plan_id,requester_actor_key)
    """,
    """ALTER TABLE reminders ADD CONSTRAINT uq_reminder_official_target UNIQUE(reminder_id,account_key,endpoint_key,creator_actor_key)
    """,
    """ALTER TABLE agent_routines ADD CONSTRAINT uq_routine_official_target UNIQUE(routine_id,account_key,endpoint_key,creator_actor_key)
    """,
    """ALTER TABLE routine_runs ADD CONSTRAINT uq_routine_run_parent UNIQUE(run_id,routine_id)
    """,
    """ALTER TABLE qq_official_schedule_bindings ADD CONSTRAINT fk_schedule_reminder_target
      FOREIGN KEY(target_reminder_id,account_key,endpoint_key,actor_key)
      REFERENCES reminders(reminder_id,account_key,endpoint_key,creator_actor_key)
    """,
    """ALTER TABLE qq_official_schedule_bindings ADD CONSTRAINT fk_schedule_routine_target
      FOREIGN KEY(target_routine_id,account_key,endpoint_key,actor_key)
      REFERENCES agent_routines(routine_id,account_key,endpoint_key,creator_actor_key)
    """,
    """ALTER TABLE qq_official_schedule_bindings ADD CONSTRAINT fk_schedule_run_parent
      FOREIGN KEY(target_run_id,target_routine_id) REFERENCES routine_runs(run_id,routine_id)
    """,
    """ALTER TABLE subagent_runs ADD CONSTRAINT uq_migration_run_task UNIQUE(run_id,task_id)
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD CONSTRAINT uq_migration_dispatch_job UNIQUE(task_id,job_id)
    """,
    """ALTER TABLE migration_subagent_continuations ADD CONSTRAINT fk_subagent_migration_job
      FOREIGN KEY(item_id,job_id) REFERENCES migration_items(item_id,job_id) DEFERRABLE INITIALLY DEFERRED
    """,
    """ALTER TABLE migration_subagent_continuations ADD CONSTRAINT fk_subagent_migration_dispatch
      FOREIGN KEY(target_task_id,job_id) REFERENCES qq_official_subagent_dispatches(task_id,job_id)
    """,
    """ALTER TABLE migration_subagent_continuations ADD CONSTRAINT fk_subagent_migration_plan
      FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES delivery_plans(plan_id,account_key,endpoint_key)
    """,
    """ALTER TABLE migration_subagent_continuations ADD CONSTRAINT fk_subagent_migration_actor
      FOREIGN KEY(plan_id,actor_key) REFERENCES delivery_plans(plan_id,requester_actor_key)
    """,
    """CREATE FUNCTION qq_official_task_profile_valid() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM qq_official_subagent_dispatches d JOIN subagent_tasks t USING(task_id)
        WHERE t.task_id=NEW.task_id AND (
          (d.tool_profile='official-projects-v1' AND (t.project_id IS NULL OR t.migration_item_id IS NOT NULL))
          OR (d.tool_profile<>'official-projects-v1' AND t.project_id IS NOT NULL))) THEN
        RAISE EXCEPTION 'official_task_profile_source_conflict' USING ERRCODE='23514';
      END IF;
      RETURN NULL;
    END $$
    """,
    """CREATE CONSTRAINT TRIGGER qq_official_dispatch_profile_source
      AFTER INSERT OR UPDATE ON qq_official_subagent_dispatches DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION qq_official_task_profile_valid()
    """,
    """CREATE CONSTRAINT TRIGGER qq_official_task_profile_source
      AFTER INSERT OR UPDATE ON subagent_tasks DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION qq_official_task_profile_valid()
    """,
    """CREATE TABLE qq_migration_native_sources (
 inbox_id TEXT PRIMARY KEY,
 account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
 source_session_id TEXT NOT NULL, source_seq BIGINT NOT NULL,
 native_message_id TEXT NOT NULL, scope_key TEXT NOT NULL,
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>=1),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>=1),
 logical_route_id TEXT REFERENCES migration_routes(route_id),
 logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=0),
 source_kind TEXT NOT NULL CHECK(source_kind IN ('message','binding_control','admin_control')),
 received_at BIGINT NOT NULL CHECK(received_at>=0),
 UNIQUE(inbox_id,account_key,endpoint_key,actor_key),
 FOREIGN KEY(inbox_id,account_key,endpoint_key) REFERENCES qq_official_inbound_events(inbox_id,account_key,endpoint_key),
 FOREIGN KEY(inbox_id,native_message_id) REFERENCES qq_official_inbound_events(inbox_id,native_message_id),
 FOREIGN KEY(account_key,source_session_id,source_seq,inbox_id) REFERENCES qq_official_gateway_receipts(account_key,session_id,seq,inbox_id),
 CHECK(logical_route_id IS NOT NULL OR logical_route_epoch=0)
)
    """,
    """CREATE TABLE qq_migration_native_plan_sources (
 plan_id TEXT PRIMARY KEY,
 inbox_id TEXT NOT NULL,account_key TEXT NOT NULL,endpoint_key TEXT NOT NULL,actor_key TEXT NOT NULL,
 FOREIGN KEY(inbox_id,account_key,endpoint_key,actor_key) REFERENCES qq_migration_native_sources(inbox_id,account_key,endpoint_key,actor_key),
 FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES delivery_plans(plan_id,account_key,endpoint_key),
 FOREIGN KEY(plan_id,actor_key) REFERENCES delivery_plans(plan_id,requester_actor_key)
)
    """,
    """CREATE FUNCTION qq_migration_native_source_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN RAISE EXCEPTION 'migration_native_source_immutable'; END $$
    """,
    """CREATE TRIGGER qq_migration_native_source_immutable BEFORE UPDATE OR DELETE ON qq_migration_native_sources
 FOR EACH ROW EXECUTE FUNCTION qq_migration_native_source_immutable()
    """,
    """CREATE TRIGGER qq_migration_native_plan_source_immutable BEFORE UPDATE OR DELETE ON qq_migration_native_plan_sources
 FOR EACH ROW EXECUTE FUNCTION qq_migration_native_source_immutable()
    """,
    """ALTER TABLE migration_subagent_continuations ADD COLUMN tool_profile TEXT NOT NULL DEFAULT 'official-text-v1' CHECK(tool_profile IN ('official-text-v1','official-files-v1'))
    """,
    """ALTER TABLE migration_subagent_continuations ADD CONSTRAINT uq_migration_task_artifact_source UNIQUE(item_id,target_task_id,job_id,account_key,endpoint_key,actor_key)
    """,
    """ALTER TABLE qq_official_subagent_dispatches DROP CONSTRAINT qq_official_subagent_dispatches_tool_profile_check
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD CONSTRAINT qq_official_subagent_dispatches_tool_profile_check
        CHECK(tool_profile IN ('official-text-v1','official-files-v1','official-projects-v1','official-host-v1')
        AND (migration_item_id IS NULL OR tool_profile IN ('official-text-v1','official-files-v1')))
    """,
    """ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT qq_official_file_artifacts_source_extension_kind_check
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_extension_kind CHECK(source_extension_kind IN ('project','migration'))
    """,
    """ALTER TABLE qq_official_file_artifacts ADD COLUMN source_migration_item_id TEXT REFERENCES migration_items(item_id)
    """,
    """ALTER TABLE qq_official_file_artifacts ALTER COLUMN source_execution_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_file_artifacts ALTER COLUMN source_canonical_message_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_file_artifacts ALTER COLUMN source_native_message_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_union CHECK(
      (source_migration_item_id IS NULL AND source_extension_kind IS DISTINCT FROM 'migration'
       AND source_execution_id IS NOT NULL AND source_canonical_message_id IS NOT NULL AND source_native_message_id IS NOT NULL)
      OR(source_migration_item_id IS NOT NULL AND source_extension_kind IS NOT DISTINCT FROM 'migration'
       AND source_execution_id IS NULL AND source_canonical_message_id IS NULL AND source_native_message_id IS NULL
       AND source_task_id IS NOT NULL AND source_task_revision IS NOT NULL AND source_task_revision=1))
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT fk_file_migration_source
        FOREIGN KEY(source_migration_item_id,source_task_id,source_job_id,account_key,endpoint_key,requester_actor_key)
        REFERENCES migration_subagent_continuations(item_id,target_task_id,job_id,account_key,endpoint_key,actor_key)
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT uq_file_migration_source UNIQUE(artifact_id,source_migration_item_id,source_task_id)
    """,
    """CREATE TABLE qq_official_migration_artifacts (
        artifact_id TEXT PRIMARY KEY, source_extension_kind TEXT NOT NULL DEFAULT 'migration' CHECK(source_extension_kind='migration'),
        item_id TEXT NOT NULL, task_id BIGINT NOT NULL, task_revision BIGINT NOT NULL CHECK(task_revision=1),
        source_job_id BIGINT NOT NULL, account_key TEXT NOT NULL,endpoint_key TEXT NOT NULL,actor_key TEXT NOT NULL,
        logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=1),
        mapping_id TEXT NOT NULL,mapping_revision BIGINT NOT NULL CHECK(mapping_revision>=1),
        source_digest TEXT NOT NULL CHECK(source_digest ~ '^[a-f0-9]{64}$'),
        result_deadline BIGINT NOT NULL,created_at BIGINT NOT NULL CHECK(created_at>=0),
        CHECK(result_deadline>created_at),UNIQUE(artifact_id,item_id),
        FOREIGN KEY(artifact_id,source_extension_kind) REFERENCES qq_official_file_artifacts(artifact_id,source_extension_kind),
        FOREIGN KEY(artifact_id,item_id,task_id) REFERENCES qq_official_file_artifacts(artifact_id,source_migration_item_id,source_task_id),
        FOREIGN KEY(item_id,task_id,source_job_id,account_key,endpoint_key,actor_key)
            REFERENCES migration_subagent_continuations(item_id,target_task_id,job_id,account_key,endpoint_key,actor_key),
        FOREIGN KEY(mapping_id,mapping_revision) REFERENCES migration_actor_mapping_revisions(mapping_id,revision)
    )
    """,
    """ALTER TABLE qq_official_file_artifacts ADD COLUMN migration_artifact_id TEXT
        GENERATED ALWAYS AS (CASE WHEN source_extension_kind='migration' THEN artifact_id ELSE NULL END) STORED
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT fk_file_migration_provenance
        FOREIGN KEY(migration_artifact_id) REFERENCES qq_official_migration_artifacts(artifact_id) DEFERRABLE INITIALLY DEFERRED
    """,
    """CREATE FUNCTION qq_migration_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_migration_artifact'; END IF;
        IF TG_TABLE_NAME='qq_official_file_artifacts' THEN
          IF NEW.source_migration_item_id IS DISTINCT FROM OLD.source_migration_item_id THEN
            RAISE EXCEPTION 'immutable_migration_artifact'; END IF;
        ELSIF NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'immutable_migration_artifact'; END IF;
        RETURN NEW; END $$
    """,
    """CREATE TRIGGER qq_migration_artifact_manifest_immutable BEFORE UPDATE ON qq_official_file_artifacts
        FOR EACH ROW EXECUTE FUNCTION qq_migration_artifact_immutable()
    """,
    """CREATE TRIGGER qq_migration_artifact_source_immutable BEFORE UPDATE OR DELETE ON qq_official_migration_artifacts
        FOR EACH ROW EXECUTE FUNCTION qq_migration_artifact_immutable()
    """,
    """ALTER TABLE qq_official_generation_capacity ALTER COLUMN source_execution_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_generation_capacity ADD COLUMN source_migration_item_id TEXT
    """,
    """ALTER TABLE qq_official_generation_capacity ADD COLUMN source_task_id BIGINT
    """,
    """ALTER TABLE qq_official_generation_capacity ADD COLUMN source_task_revision BIGINT
    """,
    """ALTER TABLE qq_official_generation_capacity ADD COLUMN source_job_id BIGINT
    """,
    """ALTER TABLE qq_official_generation_capacity ADD CONSTRAINT ck_capacity_source_union CHECK(
      (source_execution_id IS NOT NULL AND source_migration_item_id IS NULL AND source_task_id IS NULL
        AND source_task_revision IS NULL AND source_job_id IS NULL)
      OR(source_execution_id IS NULL AND source_migration_item_id IS NOT NULL AND source_task_id IS NOT NULL
        AND source_task_revision IS NOT NULL AND source_task_revision=1 AND source_job_id IS NOT NULL))
    """,
    """ALTER TABLE qq_official_generation_capacity ADD CONSTRAINT fk_capacity_migration_source
        FOREIGN KEY(source_migration_item_id,source_task_id,source_job_id,account_key,endpoint_key,requester_actor_key)
        REFERENCES migration_subagent_continuations(item_id,target_task_id,job_id,account_key,endpoint_key,actor_key)
    """,
    """CREATE FUNCTION qq_capacity_source_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF ROW(NEW.reservation_id,NEW.account_key,NEW.endpoint_key,NEW.requester_actor_key,NEW.source_execution_id,
          NEW.source_migration_item_id,NEW.source_task_id,NEW.source_task_revision,NEW.source_job_id,
          NEW.source_key,NEW.owner,NEW.created_at,NEW.expires_at)
        IS DISTINCT FROM ROW(OLD.reservation_id,OLD.account_key,OLD.endpoint_key,OLD.requester_actor_key,OLD.source_execution_id,
          OLD.source_migration_item_id,OLD.source_task_id,OLD.source_task_revision,OLD.source_job_id,
          OLD.source_key,OLD.owner,OLD.created_at,OLD.expires_at)
      THEN RAISE EXCEPTION 'immutable_capacity_source'; END IF; RETURN NEW; END $$
    """,
    """CREATE TRIGGER qq_capacity_source_immutable BEFORE UPDATE ON qq_official_generation_capacity
        FOR EACH ROW EXECUTE FUNCTION qq_capacity_source_immutable()
    """,
    """CREATE TABLE migration_artifact_plans (
      plan_id TEXT PRIMARY KEY REFERENCES delivery_plans(plan_id),artifact_id TEXT NOT NULL,
      item_id TEXT NOT NULL,source_job_id BIGINT NOT NULL REFERENCES durable_jobs(job_id),
      file_job_id BIGINT NOT NULL,file_job_generation BIGINT NOT NULL CHECK(file_job_generation>=1),
      logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=1),mapping_revision BIGINT NOT NULL CHECK(mapping_revision>=1),
      account_key TEXT NOT NULL,endpoint_key TEXT NOT NULL,actor_key TEXT NOT NULL,created_at BIGINT NOT NULL CHECK(created_at>=0),
      FOREIGN KEY(artifact_id,item_id) REFERENCES qq_official_migration_artifacts(artifact_id,item_id),
      FOREIGN KEY(artifact_id,file_job_id) REFERENCES qq_official_file_deliveries(artifact_id,job_id),
      FOREIGN KEY(plan_id,artifact_id) REFERENCES qq_official_file_message_plans(plan_id,artifact_id),
      FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES delivery_plans(plan_id,account_key,endpoint_key),
      FOREIGN KEY(plan_id,actor_key) REFERENCES delivery_plans(plan_id,requester_actor_key),
      CHECK(source_job_id<>file_job_id)
    )
    """,
    """CREATE TRIGGER qq_migration_artifact_plan_immutable BEFORE UPDATE OR DELETE ON migration_artifact_plans
        FOR EACH ROW EXECUTE FUNCTION qq_migration_artifact_immutable()
    """,
    """CREATE INDEX ix_migration_artifact_item ON qq_official_migration_artifacts(item_id,artifact_id)
    """,
    """CREATE INDEX ix_migration_artifact_plans_item ON migration_artifact_plans(item_id,plan_id)
    """,
    """ALTER TABLE migration_verification_intents ADD CONSTRAINT uq_migration_verification_request_target
 UNIQUE(request_id,migration_id,kind,endpoint_key,actor_key)
    """,
    """CREATE TABLE migration_verification_sources (
 request_id TEXT PRIMARY KEY,
 migration_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('text','file')),
 account_key TEXT NOT NULL,endpoint_key TEXT NOT NULL,actor_key TEXT NOT NULL,
 inbox_id TEXT NOT NULL,window_id TEXT NOT NULL,
 owner_grant_id TEXT NOT NULL REFERENCES qq_official_owner_grants(grant_id),
 owner_revision BIGINT NOT NULL CHECK(owner_revision>=1),
 artifact_id TEXT REFERENCES qq_official_file_artifacts(artifact_id),
 created_at BIGINT NOT NULL CHECK(created_at>=0),expires_at BIGINT NOT NULL,
 UNIQUE(migration_id,kind),
 FOREIGN KEY(request_id,migration_id,kind,endpoint_key,actor_key)
   REFERENCES migration_verification_intents(request_id,migration_id,kind,endpoint_key,actor_key),
 FOREIGN KEY(inbox_id,account_key,endpoint_key,actor_key)
   REFERENCES qq_migration_native_sources(inbox_id,account_key,endpoint_key,actor_key),
 FOREIGN KEY(window_id,account_key,endpoint_key)
   REFERENCES qq_official_reply_windows(window_id,account_key,endpoint_key),
 FOREIGN KEY(artifact_id,account_key,endpoint_key)
   REFERENCES qq_official_file_artifacts(artifact_id,account_key,endpoint_key),
 FOREIGN KEY(artifact_id,actor_key) REFERENCES qq_official_file_artifacts(artifact_id,requester_actor_key),
 CHECK(expires_at>created_at AND expires_at-created_at<=30),
 CHECK((kind='file')=(artifact_id IS NOT NULL))
)
    """,
    """CREATE TRIGGER migration_verification_source_immutable BEFORE UPDATE OR DELETE ON migration_verification_sources
 FOR EACH ROW EXECUTE FUNCTION qq_migration_native_source_immutable()
    """,
    """CREATE TABLE migration_file_continuations (
 item_id TEXT PRIMARY KEY REFERENCES migration_items(item_id),
 item_kind TEXT NOT NULL DEFAULT 'file' CHECK(item_kind='file'),
 origin TEXT NOT NULL CHECK(origin IN ('subagent','project')),source_ref TEXT NOT NULL,
 source_revision BIGINT NOT NULL CHECK(source_revision>=1),
 source_frozen_digest TEXT NOT NULL CHECK(source_frozen_digest ~ '^[a-f0-9]{64}$'),
 export_digest TEXT NOT NULL CHECK(export_digest ~ '^[a-f0-9]{64}$'),
 source_json TEXT NOT NULL CHECK(jsonb_typeof(source_json::jsonb)='object'),
 account_key TEXT NOT NULL,endpoint_key TEXT NOT NULL,actor_key TEXT NOT NULL,
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>=1),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>=1),
 logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=1),
 mapping_id TEXT NOT NULL,mapping_revision BIGINT NOT NULL CHECK(mapping_revision>=1),
 execution_deadline BIGINT NOT NULL,result_deadline BIGINT NOT NULL,
 job_id BIGINT NOT NULL UNIQUE REFERENCES durable_jobs(job_id),
 artifact_id TEXT UNIQUE REFERENCES qq_official_file_artifacts(artifact_id),
 project_id BIGINT REFERENCES projects(project_id),grant_id TEXT,grant_revision BIGINT,
 created_at BIGINT NOT NULL CHECK(created_at>=0),
 CHECK(created_at<execution_deadline AND execution_deadline<=result_deadline),
 CHECK((origin='project' AND project_id IS NOT NULL AND grant_id IS NOT NULL AND grant_revision IS NOT NULL)
    OR (origin='subagent' AND project_id IS NULL AND grant_id IS NULL AND grant_revision IS NULL)),
 FOREIGN KEY(item_id,item_kind) REFERENCES migration_items(item_id,kind),
 FOREIGN KEY(mapping_id,mapping_revision) REFERENCES migration_actor_mapping_revisions(mapping_id,revision),
 FOREIGN KEY(grant_id,project_id) REFERENCES qq_official_project_grants(grant_id,project_id),
 FOREIGN KEY(grant_id,account_key) REFERENCES qq_official_project_grants(grant_id,account_key),
 FOREIGN KEY(grant_id,endpoint_key) REFERENCES qq_official_project_grants(grant_id,endpoint_key),
 FOREIGN KEY(grant_id,actor_key) REFERENCES qq_official_project_grants(grant_id,actor_key),
 FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
 UNIQUE(item_id,job_id,account_key,endpoint_key,actor_key),UNIQUE(item_id,artifact_id)
)
    """,
    """CREATE FUNCTION migration_file_binding_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_migration_file'; END IF;
 IF (to_jsonb(NEW)-'artifact_id') IS DISTINCT FROM (to_jsonb(OLD)-'artifact_id')
   OR (OLD.artifact_id IS NOT NULL AND NEW.artifact_id IS DISTINCT FROM OLD.artifact_id)
 THEN RAISE EXCEPTION 'immutable_migration_file'; END IF; RETURN NEW; END $$
    """,
    """CREATE TRIGGER migration_file_binding_immutable BEFORE UPDATE OR DELETE ON migration_file_continuations
 FOR EACH ROW EXECUTE FUNCTION migration_file_binding_immutable()
    """,
    """ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT ck_file_source_union
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_union CHECK(
 (source_migration_item_id IS NULL AND source_extension_kind IS DISTINCT FROM 'migration'
  AND source_execution_id IS NOT NULL AND source_canonical_message_id IS NOT NULL AND source_native_message_id IS NOT NULL)
 OR(source_migration_item_id IS NOT NULL AND source_extension_kind IS NOT DISTINCT FROM 'migration'
  AND source_execution_id IS NULL AND source_canonical_message_id IS NULL AND source_native_message_id IS NULL
  AND ((source_task_id IS NOT NULL AND source_task_revision IS NOT NULL AND source_task_revision=1)
       OR(source_task_id IS NULL AND source_task_revision IS NULL))))
    """,
    """ALTER TABLE qq_official_migration_artifacts ALTER COLUMN task_id DROP NOT NULL
    """,
    """ALTER TABLE qq_official_migration_artifacts ALTER COLUMN task_revision DROP NOT NULL
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'subagent'
 CHECK(source_kind IN ('subagent','file'))
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT ck_migration_artifact_source_kind CHECK(
 (source_kind='subagent' AND task_id IS NOT NULL AND task_revision IS NOT NULL AND task_revision=1)
 OR(source_kind='file' AND task_id IS NULL AND task_revision IS NULL))
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD COLUMN import_item_id TEXT
 GENERATED ALWAYS AS (CASE WHEN source_kind='file' THEN item_id ELSE NULL END) STORED
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT uq_file_import_manifest
 UNIQUE(artifact_id,source_migration_item_id,source_job_id,account_key,endpoint_key,requester_actor_key)
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT fk_import_artifact_manifest
 FOREIGN KEY(artifact_id,item_id,source_job_id,account_key,endpoint_key,actor_key)
 REFERENCES qq_official_file_artifacts(artifact_id,source_migration_item_id,source_job_id,account_key,endpoint_key,requester_actor_key)
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT fk_import_artifact_source
 FOREIGN KEY(import_item_id,source_job_id,account_key,endpoint_key,actor_key)
 REFERENCES migration_file_continuations(item_id,job_id,account_key,endpoint_key,actor_key)
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT fk_import_artifact_binding
 FOREIGN KEY(import_item_id,artifact_id) REFERENCES migration_file_continuations(item_id,artifact_id)
 DEFERRABLE INITIALLY DEFERRED
    """,
    """CREATE UNIQUE INDEX uq_import_item_artifact ON qq_official_migration_artifacts(item_id) WHERE source_kind='file'
    """,
    """ALTER TABLE projects ADD CONSTRAINT uq_migration_project_origin UNIQUE(project_id,scope_key,bot_native_user_id)
    """,
    """CREATE TABLE migration_project_grants (
 grant_id TEXT NOT NULL REFERENCES qq_official_project_grants(grant_id),
 grant_revision BIGINT NOT NULL CHECK(grant_revision>0),
 project_id BIGINT NOT NULL,
 source_endpoint_key TEXT NOT NULL CHECK(octet_length(source_endpoint_key) BETWEEN 1 AND 1000),
 source_scope_key TEXT NOT NULL,
 source_actor_key TEXT NOT NULL CHECK(octet_length(source_actor_key) BETWEEN 1 AND 1280),
 original_bot_id TEXT NOT NULL CHECK(original_bot_id ~ '^[1-9][0-9]*$'),
 target_account_key TEXT NOT NULL,
 target_endpoint_key TEXT NOT NULL,
 target_actor_key TEXT NOT NULL,
 mapping_id TEXT NOT NULL,
 mapping_revision BIGINT NOT NULL CHECK(mapping_revision>0),
 expected_route_epoch BIGINT NOT NULL CHECK(expected_route_epoch>=2),
 admin_account_id TEXT NOT NULL REFERENCES admin_accounts(account_id),
 admin_version BIGINT NOT NULL CHECK(admin_version>0),
 admin_binding_id TEXT NOT NULL REFERENCES qq_official_admin_bindings(binding_id),
 admin_binding_revision BIGINT NOT NULL CHECK(admin_binding_revision>0),
 migration_id TEXT REFERENCES migration_journals(migration_id),
 created_at BIGINT NOT NULL CHECK(created_at>=0),
 grant_snapshot_json TEXT NOT NULL CHECK(octet_length(grant_snapshot_json) BETWEEN 2 AND 16384
   AND jsonb_typeof(grant_snapshot_json::jsonb)='object'),
 PRIMARY KEY(grant_id,grant_revision),
 UNIQUE(project_id,target_endpoint_key,target_actor_key,grant_revision),
 FOREIGN KEY(project_id,source_scope_key,original_bot_id) REFERENCES projects(project_id,scope_key,bot_native_user_id),
 FOREIGN KEY(grant_id,project_id) REFERENCES qq_official_project_grants(grant_id,project_id),
 FOREIGN KEY(grant_id,target_account_key) REFERENCES qq_official_project_grants(grant_id,account_key),
 FOREIGN KEY(grant_id,target_endpoint_key) REFERENCES qq_official_project_grants(grant_id,endpoint_key),
 FOREIGN KEY(grant_id,target_actor_key) REFERENCES qq_official_project_grants(grant_id,actor_key),
 FOREIGN KEY(mapping_id,mapping_revision) REFERENCES migration_actor_mapping_revisions(mapping_id,revision)
)
    """,
    """CREATE FUNCTION migration_project_grant_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_project_migration_grant'; END IF;
 IF (to_jsonb(NEW)-'migration_id') IS DISTINCT FROM (to_jsonb(OLD)-'migration_id')
   OR (OLD.migration_id IS NOT NULL AND NEW.migration_id IS DISTINCT FROM OLD.migration_id)
 THEN RAISE EXCEPTION 'immutable_project_migration_grant'; END IF; RETURN NEW; END $$
    """,
    """CREATE TRIGGER migration_project_grant_immutable BEFORE UPDATE OR DELETE ON migration_project_grants
 FOR EACH ROW EXECUTE FUNCTION migration_project_grant_immutable()
    """,
    """CREATE TABLE qq_official_project_focus (
        account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL CHECK(octet_length(endpoint_key)<=1000),
        actor_key TEXT NOT NULL CHECK(octet_length(actor_key)<=1280),
        principal_id BIGINT NOT NULL REFERENCES principals(principal_id),
        project_id BIGINT NOT NULL REFERENCES projects(project_id),
        updated_at BIGINT NOT NULL CHECK(updated_at>=0),
        PRIMARY KEY(endpoint_key,actor_key),
        FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key)
    )
    """,
    """ALTER TABLE project_io ADD CONSTRAINT uq_project_io_authority_source
        UNIQUE(operation_id,project_id,actor_principal_id,journal_turn_id)
    """,
    """CREATE TABLE qq_official_project_io_authority (
        operation_id BIGINT PRIMARY KEY REFERENCES project_io(operation_id),
        project_id BIGINT NOT NULL REFERENCES projects(project_id),
        turn_id BIGINT NOT NULL REFERENCES agent_turns(turn_id),
        source_execution_id TEXT NOT NULL REFERENCES qq_official_turn_executions(execution_id),
        source_job_id BIGINT NOT NULL REFERENCES durable_jobs(job_id),
        source_job_generation BIGINT NOT NULL CHECK(source_job_generation>0),
        source_revision BIGINT NOT NULL CHECK(source_revision>0),
        source_digest TEXT NOT NULL CHECK(source_digest ~ '^[a-f0-9]{64}$'),
        io_digest TEXT NOT NULL CHECK(io_digest ~ '^[a-f0-9]{64}$'),
        grant_id TEXT NOT NULL, grant_revision BIGINT NOT NULL CHECK(grant_revision>0),
        account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
        principal_id BIGINT NOT NULL REFERENCES principals(principal_id),
        resource_scope_key TEXT NOT NULL CHECK(octet_length(resource_scope_key)<=1000),
        endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>0),
        endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>0),
        route_epoch BIGINT NOT NULL CHECK(route_epoch>=0),
        created_at BIGINT NOT NULL, deadline_at BIGINT NOT NULL,
        CHECK(deadline_at>created_at AND deadline_at-created_at<=3600),
        FOREIGN KEY(operation_id,project_id,principal_id,turn_id)
            REFERENCES project_io(operation_id,project_id,actor_principal_id,journal_turn_id),
        FOREIGN KEY(source_execution_id,endpoint_key,actor_key)
            REFERENCES qq_official_turn_executions(execution_id,endpoint_key,requester_actor_key),
        FOREIGN KEY(grant_id,project_id) REFERENCES qq_official_project_grants(grant_id,project_id),
        FOREIGN KEY(grant_id,account_key) REFERENCES qq_official_project_grants(grant_id,account_key),
        FOREIGN KEY(grant_id,endpoint_key) REFERENCES qq_official_project_grants(grant_id,endpoint_key),
        FOREIGN KEY(grant_id,actor_key) REFERENCES qq_official_project_grants(grant_id,actor_key)
    )
    """,
    """ALTER TABLE qq_official_project_lifecycle_authority ADD COLUMN source_io_operation_id
        BIGINT REFERENCES qq_official_project_io_authority(operation_id)
    """,
    """ALTER TABLE qq_official_project_lifecycle_authority ADD CONSTRAINT ck_project_autosave_source
        CHECK(source_io_operation_id IS NULL OR (action='snapshot' AND source_execution_id IS NOT NULL
              AND source_task_id IS NULL))
    """,
    """ALTER TABLE qq_official_project_io_authority ADD CONSTRAINT ck_official_io_created CHECK(created_at>=0)
    """,
    """CREATE FUNCTION qq_official_project_io_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'qq_official_project_io_authority_immutable' USING ERRCODE='23514';
        END $$
    """,
    """CREATE TRIGGER qq_official_project_io_immutable BEFORE UPDATE OR DELETE
          ON qq_official_project_io_authority FOR EACH ROW
          EXECUTE FUNCTION qq_official_project_io_immutable()
    """,
    """ALTER TABLE subagent_tasks DROP CONSTRAINT ck_subagent_identity_union
    """,
    """ALTER TABLE subagent_tasks ADD CONSTRAINT ck_subagent_identity_union CHECK (
 (requester_user_id IS NOT NULL AND account_key IS NULL AND endpoint_key IS NULL
   AND requester_actor_key IS NULL AND migration_item_id IS NULL)
 OR(requester_user_id IS NULL AND account_key IS NOT NULL AND endpoint_key IS NOT NULL
   AND requester_actor_key IS NOT NULL
   AND ((trigger_message_id IS NOT NULL AND migration_item_id IS NULL)
     OR(trigger_message_id IS NULL AND migration_item_id IS NOT NULL))))
    """,
    """ALTER TABLE qq_official_subagent_dispatches DROP CONSTRAINT qq_official_subagent_dispatches_tool_profile_check
    """,
    """ALTER TABLE qq_official_subagent_dispatches ADD CONSTRAINT qq_official_subagent_dispatches_tool_profile_check
 CHECK(tool_profile IN ('official-text-v1','official-files-v1','official-projects-v1','official-host-v1')
   AND(migration_item_id IS NULL OR tool_profile IN ('official-text-v1','official-files-v1','official-projects-v1')))
    """,
    """ALTER TABLE migration_subagent_continuations DROP CONSTRAINT migration_subagent_continuations_tool_profile_check
    """,
    """ALTER TABLE migration_subagent_continuations ADD CONSTRAINT migration_subagent_continuations_tool_profile_check
 CHECK(tool_profile IN ('official-text-v1','official-files-v1','official-projects-v1'))
    """,
    """CREATE OR REPLACE FUNCTION qq_official_task_profile_valid() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
   IF EXISTS(SELECT 1 FROM qq_official_subagent_dispatches d JOIN subagent_tasks t USING(task_id)
     LEFT JOIN migration_items i ON i.item_id=t.migration_item_id WHERE t.task_id=NEW.task_id AND (
       (d.tool_profile='official-projects-v1' AND (t.project_id IS NULL
         OR(t.migration_item_id IS NOT NULL AND i.kind IS DISTINCT FROM 'project_task')))
       OR(d.tool_profile<>'official-projects-v1' AND (t.project_id IS NOT NULL
         OR(t.migration_item_id IS NOT NULL AND i.kind IS DISTINCT FROM 'subagent')))))
   THEN RAISE EXCEPTION 'official_task_profile_source_conflict' USING ERRCODE='23514'; END IF;
   RETURN NULL;
 END $$
    """,
    """ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT ck_file_source_union
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_union CHECK(
 (source_migration_item_id IS NULL AND source_extension_kind IS DISTINCT FROM 'migration'
  AND source_execution_id IS NOT NULL AND source_canonical_message_id IS NOT NULL AND source_native_message_id IS NOT NULL)
 OR(source_migration_item_id IS NOT NULL AND source_extension_kind IS NOT NULL AND source_extension_kind IN ('migration','project')
  AND source_execution_id IS NULL AND source_canonical_message_id IS NULL AND source_native_message_id IS NULL
  AND ((source_task_id IS NOT NULL AND source_task_revision IS NOT NULL AND source_task_revision=1)
       OR(source_extension_kind='migration' AND source_task_id IS NULL AND source_task_revision IS NULL))))
    """,
    """ALTER TABLE qq_official_migration_artifacts DROP CONSTRAINT qq_official_migration_artifacts_source_extension_kind_check
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT qq_official_migration_artifacts_source_extension_kind_check CHECK(source_extension_kind IN ('project','migration'))
    """,
    """ALTER TABLE qq_official_migration_artifacts DROP CONSTRAINT qq_official_migration_artifacts_source_kind_check
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT qq_official_migration_artifacts_source_kind_check CHECK(source_kind IN ('subagent','file','project_task'))
    """,
    """ALTER TABLE qq_official_migration_artifacts DROP CONSTRAINT ck_migration_artifact_source_kind
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT ck_migration_artifact_source_kind CHECK(
 (source_kind='subagent' AND source_extension_kind='migration' AND task_id IS NOT NULL AND task_revision IS NOT NULL AND task_revision=1)
 OR(source_kind='project_task' AND source_extension_kind='project' AND task_id IS NOT NULL AND task_revision IS NOT NULL AND task_revision=1)
 OR(source_kind='file' AND source_extension_kind='migration' AND task_id IS NULL AND task_revision IS NULL))
    """,
    """ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT fk_file_migration_provenance
    """,
    """ALTER TABLE qq_official_file_artifacts DROP COLUMN migration_artifact_id
    """,
    """ALTER TABLE qq_official_file_artifacts ADD COLUMN migration_artifact_id TEXT GENERATED ALWAYS AS(CASE WHEN source_migration_item_id IS NOT NULL THEN artifact_id ELSE NULL END) STORED
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT fk_file_migration_provenance FOREIGN KEY(migration_artifact_id) REFERENCES qq_official_migration_artifacts(artifact_id) DEFERRABLE INITIALLY DEFERRED
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD COLUMN project_artifact_id TEXT GENERATED ALWAYS AS(CASE WHEN source_kind='project_task' THEN artifact_id ELSE NULL END) STORED
    """,
    """ALTER TABLE qq_official_migration_artifacts ADD CONSTRAINT fk_migration_project_artifact FOREIGN KEY(project_artifact_id) REFERENCES qq_official_project_artifacts(artifact_id) DEFERRABLE INITIALLY DEFERRED
    """,
)

DOWNGRADE = (
    """DROP TRIGGER "migration_mapping_revision_immutable" ON "migration_actor_mapping_revisions"
    """,
    """DROP TRIGGER "migration_mapping_source_immutable" ON "migration_actor_mappings"
    """,
    """DROP TRIGGER "qq_migration_artifact_plan_immutable" ON "migration_artifact_plans"
    """,
    """DROP TRIGGER "migration_file_binding_immutable" ON "migration_file_continuations"
    """,
    """DROP TRIGGER "migration_project_grant_immutable" ON "migration_project_grants"
    """,
    """DROP TRIGGER "migration_verification_source_immutable" ON "migration_verification_sources"
    """,
    """DROP TRIGGER "migration_work_source_parent_guard" ON "migration_work_sources"
    """,
    """DROP TRIGGER "qq_migration_native_plan_source_immutable" ON "qq_migration_native_plan_sources"
    """,
    """DROP TRIGGER "qq_migration_native_source_immutable" ON "qq_migration_native_sources"
    """,
    """DROP TRIGGER "qq_artifact_extension_immutable" ON "qq_official_file_artifacts"
    """,
    """DROP TRIGGER "qq_migration_artifact_manifest_immutable" ON "qq_official_file_artifacts"
    """,
    """DROP TRIGGER "qq_capacity_source_immutable" ON "qq_official_generation_capacity"
    """,
    """DROP TRIGGER "qq_migration_artifact_source_immutable" ON "qq_official_migration_artifacts"
    """,
    """DROP TRIGGER "qq_project_artifact_immutable" ON "qq_official_project_artifacts"
    """,
    """DROP TRIGGER "qq_official_project_io_immutable" ON "qq_official_project_io_authority"
    """,
    """DROP TRIGGER "qq_official_dispatch_profile_source" ON "qq_official_subagent_dispatches"
    """,
    """DROP TRIGGER "qq_official_task_profile_source" ON "subagent_tasks"
    """,
    """DROP FUNCTION "migration_file_binding_immutable"()
    """,
    """DROP FUNCTION "migration_mapping_revision_immutable"()
    """,
    """DROP FUNCTION "migration_mapping_source_immutable"()
    """,
    """DROP FUNCTION "migration_project_grant_immutable"()
    """,
    """DROP FUNCTION "migration_work_source_parent_check"()
    """,
    """DROP FUNCTION "qq_artifact_extension_immutable"()
    """,
    """DROP FUNCTION "qq_capacity_source_immutable"()
    """,
    """DROP FUNCTION "qq_migration_artifact_immutable"()
    """,
    """DROP FUNCTION "qq_migration_native_source_immutable"()
    """,
    """DROP FUNCTION "qq_official_project_io_immutable"()
    """,
    """DROP FUNCTION "qq_official_task_profile_valid"()
    """,
    """DROP FUNCTION "qq_project_artifact_immutable"()
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "fk_admin_official_binding_account"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "fk_admin_official_binding_actor"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "fk_admin_official_binding_bot"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "fk_admin_official_binding_endpoint"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "fk_admin_official_source_endpoint"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "fk_admin_official_source_execution"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "fk_admin_official_source_task"
    """,
    """ALTER TABLE "agent_routines" DROP CONSTRAINT "agent_routines_budget_origin_routine_id_fkey"
    """,
    """ALTER TABLE "agent_routines" DROP CONSTRAINT "agent_routines_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "delivery_plans" DROP CONSTRAINT "fk_plan_approval_source_actor"
    """,
    """ALTER TABLE "delivery_plans" DROP CONSTRAINT "fk_plan_approval_source_endpoint"
    """,
    """ALTER TABLE "delivery_plans" DROP CONSTRAINT "fk_plan_approval_source_expiry"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "fk_artifact_project_provenance"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "fk_file_migration_provenance"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "fk_file_migration_source"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "qq_official_file_artifacts_source_migration_item_id_fkey"
    """,
    """ALTER TABLE "qq_official_generation_capacity" DROP CONSTRAINT "fk_capacity_migration_source"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP CONSTRAINT "fk_subagent_dispatch_migration"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP CONSTRAINT "qq_official_subagent_dispatches_migration_item_id_fkey"
    """,
    """ALTER TABLE "reminders" DROP CONSTRAINT "reminders_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "subagent_tasks" DROP CONSTRAINT "fk_subagent_official_project_authority"
    """,
    """ALTER TABLE "subagent_tasks" DROP CONSTRAINT "subagent_tasks_migration_item_id_fkey"
    """,
    """ALTER TABLE "migration_activity_observations" DROP CONSTRAINT "migration_activity_observations_activity_id_fkey"
    """,
    """ALTER TABLE "migration_actor_mapping_revisions" DROP CONSTRAINT "migration_actor_mapping_revisions_admin_account_id_fkey"
    """,
    """ALTER TABLE "migration_actor_mapping_revisions" DROP CONSTRAINT "migration_actor_mapping_revisions_evidence_inbox_id_fkey"
    """,
    """ALTER TABLE "migration_actor_mapping_revisions" DROP CONSTRAINT "migration_actor_mapping_revisions_mapping_id_fkey"
    """,
    """ALTER TABLE "migration_actor_mappings" DROP CONSTRAINT "fk_migration_mapping_inbox"
    """,
    """ALTER TABLE "migration_actor_mappings" DROP CONSTRAINT "migration_actor_mappings_created_by_admin_fkey"
    """,
    """ALTER TABLE "migration_actor_mappings" DROP CONSTRAINT "migration_actor_mappings_evidence_inbox_id_fkey"
    """,
    """ALTER TABLE "migration_actor_mappings" DROP CONSTRAINT "migration_actor_mappings_target_account_key_fkey"
    """,
    """ALTER TABLE "migration_actor_mappings" DROP CONSTRAINT "migration_actor_mappings_target_endpoint_key_target_accoun_fkey"
    """,
    """ALTER TABLE "migration_actor_mappings" DROP CONSTRAINT "migration_actor_mappings_updated_by_admin_fkey"
    """,
    """ALTER TABLE "migration_artifact_plans" DROP CONSTRAINT "migration_artifact_plans_artifact_id_file_job_id_fkey"
    """,
    """ALTER TABLE "migration_artifact_plans" DROP CONSTRAINT "migration_artifact_plans_artifact_id_item_id_fkey"
    """,
    """ALTER TABLE "migration_artifact_plans" DROP CONSTRAINT "migration_artifact_plans_plan_id_account_key_endpoint_key_fkey"
    """,
    """ALTER TABLE "migration_artifact_plans" DROP CONSTRAINT "migration_artifact_plans_plan_id_actor_key_fkey"
    """,
    """ALTER TABLE "migration_artifact_plans" DROP CONSTRAINT "migration_artifact_plans_plan_id_artifact_id_fkey"
    """,
    """ALTER TABLE "migration_artifact_plans" DROP CONSTRAINT "migration_artifact_plans_plan_id_fkey"
    """,
    """ALTER TABLE "migration_artifact_plans" DROP CONSTRAINT "migration_artifact_plans_source_job_id_fkey"
    """,
    """ALTER TABLE "migration_control_audit" DROP CONSTRAINT "migration_control_audit_admin_account_id_fkey"
    """,
    """ALTER TABLE "migration_dispatch_activities" DROP CONSTRAINT "migration_dispatch_activities_attempt_id_fkey"
    """,
    """ALTER TABLE "migration_dispatch_activities" DROP CONSTRAINT "migration_dispatch_activities_route_id_fkey"
    """,
    """ALTER TABLE "migration_dispatch_activities" DROP CONSTRAINT "migration_dispatch_activities_source_account_key_fkey"
    """,
    """ALTER TABLE "migration_dispatch_activities" DROP CONSTRAINT "migration_dispatch_activities_source_id_fkey"
    """,
    """ALTER TABLE "migration_events" DROP CONSTRAINT "migration_events_migration_id_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_artifact_id_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_grant_id_account_key_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_grant_id_actor_key_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_grant_id_endpoint_key_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_grant_id_project_id_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_item_id_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_item_id_item_kind_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_job_id_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_mapping_id_mapping_revision_fkey"
    """,
    """ALTER TABLE "migration_file_continuations" DROP CONSTRAINT "migration_file_continuations_project_id_fkey"
    """,
    """ALTER TABLE "migration_item_plans" DROP CONSTRAINT "fk_migration_plan_job"
    """,
    """ALTER TABLE "migration_item_plans" DROP CONSTRAINT "migration_item_plans_item_id_fkey"
    """,
    """ALTER TABLE "migration_item_plans" DROP CONSTRAINT "migration_item_plans_plan_id_fkey"
    """,
    """ALTER TABLE "migration_item_plans" DROP CONSTRAINT "migration_item_plans_source_job_id_fkey"
    """,
    """ALTER TABLE "migration_items" DROP CONSTRAINT "fk_migration_item_lineage"
    """,
    """ALTER TABLE "migration_items" DROP CONSTRAINT "fk_migration_item_mapping_revision"
    """,
    """ALTER TABLE "migration_items" DROP CONSTRAINT "fk_migration_item_mapping_source"
    """,
    """ALTER TABLE "migration_items" DROP CONSTRAINT "migration_items_job_id_fkey"
    """,
    """ALTER TABLE "migration_items" DROP CONSTRAINT "migration_items_migration_id_fkey"
    """,
    """ALTER TABLE "migration_items" DROP CONSTRAINT "migration_items_parent_item_id_fkey"
    """,
    """ALTER TABLE "migration_items" DROP CONSTRAINT "migration_items_target_routine_id_fkey"
    """,
    """ALTER TABLE "migration_journals" DROP CONSTRAINT "migration_journals_approved_by_fkey"
    """,
    """ALTER TABLE "migration_journals" DROP CONSTRAINT "migration_journals_creator_id_fkey"
    """,
    """ALTER TABLE "migration_journals" DROP CONSTRAINT "migration_journals_route_id_fkey"
    """,
    """ALTER TABLE "migration_preview_revisions" DROP CONSTRAINT "migration_preview_revisions_migration_id_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_admin_account_id_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_admin_binding_id_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_grant_id_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_grant_id_project_id_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_grant_id_target_account_key_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_grant_id_target_actor_key_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_grant_id_target_endpoint_key_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_mapping_id_mapping_revision_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_migration_id_fkey"
    """,
    """ALTER TABLE "migration_project_grants" DROP CONSTRAINT "migration_project_grants_project_id_source_scope_key_origi_fkey"
    """,
    """ALTER TABLE "migration_routes" DROP CONSTRAINT "fk_migration_target_account"
    """,
    """ALTER TABLE "migration_routes" DROP CONSTRAINT "migration_routes_source_account_key_fkey"
    """,
    """ALTER TABLE "migration_routes" DROP CONSTRAINT "migration_routes_target_account_key_fkey"
    """,
    """ALTER TABLE "migration_routes" DROP CONSTRAINT "migration_routes_target_endpoint_key_fkey"
    """,
    """ALTER TABLE "migration_scope_aliases" DROP CONSTRAINT "migration_scope_aliases_route_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "fk_subagent_mapping_revision"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "fk_subagent_migration_actor"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "fk_subagent_migration_dispatch"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "fk_subagent_migration_job"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "fk_subagent_migration_plan"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "migration_subagent_continuati_target_task_id_endpoint_key__fkey"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "migration_subagent_continuations_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "migration_subagent_continuations_item_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "migration_subagent_continuations_job_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "migration_subagent_continuations_plan_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "migration_subagent_continuations_source_task_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_continuations" DROP CONSTRAINT "migration_subagent_continuations_target_task_id_item_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_run_provenance" DROP CONSTRAINT "migration_subagent_run_provenance_item_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_run_provenance" DROP CONSTRAINT "migration_subagent_run_provenance_source_run_id_fkey"
    """,
    """ALTER TABLE "migration_subagent_run_provenance" DROP CONSTRAINT "migration_subagent_run_provenance_target_run_id_fkey"
    """,
    """ALTER TABLE "migration_verification_intents" DROP CONSTRAINT "migration_verification_intents_endpoint_key_fkey"
    """,
    """ALTER TABLE "migration_verification_intents" DROP CONSTRAINT "migration_verification_intents_migration_id_fkey"
    """,
    """ALTER TABLE "migration_verification_intents" DROP CONSTRAINT "migration_verification_intents_plan_id_fkey"
    """,
    """ALTER TABLE "migration_verification_receipts" DROP CONSTRAINT "migration_verification_receipts_delivery_id_fkey"
    """,
    """ALTER TABLE "migration_verification_receipts" DROP CONSTRAINT "migration_verification_receipts_migration_id_fkey"
    """,
    """ALTER TABLE "migration_verification_receipts" DROP CONSTRAINT "migration_verification_receipts_receipt_id_fkey"
    """,
    """ALTER TABLE "migration_verification_sources" DROP CONSTRAINT "migration_verification_source_artifact_id_account_key_endp_fkey"
    """,
    """ALTER TABLE "migration_verification_sources" DROP CONSTRAINT "migration_verification_source_inbox_id_account_key_endpoin_fkey"
    """,
    """ALTER TABLE "migration_verification_sources" DROP CONSTRAINT "migration_verification_source_request_id_migration_id_kind_fkey"
    """,
    """ALTER TABLE "migration_verification_sources" DROP CONSTRAINT "migration_verification_source_window_id_account_key_endpoi_fkey"
    """,
    """ALTER TABLE "migration_verification_sources" DROP CONSTRAINT "migration_verification_sources_artifact_id_actor_key_fkey"
    """,
    """ALTER TABLE "migration_verification_sources" DROP CONSTRAINT "migration_verification_sources_artifact_id_fkey"
    """,
    """ALTER TABLE "migration_verification_sources" DROP CONSTRAINT "migration_verification_sources_owner_grant_id_fkey"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_source_parent_account"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_source_parent_endpoint"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_source_parent_scope"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_sources_account_key_fkey"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_sources_delivery_id_fkey"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_sources_job_id_fkey"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_sources_routine_id_fkey"
    """,
    """ALTER TABLE "migration_work_sources" DROP CONSTRAINT "migration_work_sources_task_id_fkey"
    """,
    """ALTER TABLE "qq_migration_native_plan_sources" DROP CONSTRAINT "qq_migration_native_plan_sour_inbox_id_account_key_endpoin_fkey"
    """,
    """ALTER TABLE "qq_migration_native_plan_sources" DROP CONSTRAINT "qq_migration_native_plan_sour_plan_id_account_key_endpoint_fkey"
    """,
    """ALTER TABLE "qq_migration_native_plan_sources" DROP CONSTRAINT "qq_migration_native_plan_sources_plan_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_migration_native_sources" DROP CONSTRAINT "qq_migration_native_sources_account_key_source_session_id__fkey"
    """,
    """ALTER TABLE "qq_migration_native_sources" DROP CONSTRAINT "qq_migration_native_sources_inbox_id_account_key_endpoint__fkey"
    """,
    """ALTER TABLE "qq_migration_native_sources" DROP CONSTRAINT "qq_migration_native_sources_inbox_id_native_message_id_fkey"
    """,
    """ALTER TABLE "qq_migration_native_sources" DROP CONSTRAINT "qq_migration_native_sources_logical_route_id_fkey"
    """,
    """ALTER TABLE "qq_migration_public_history_grants" DROP CONSTRAINT "fk_public_history_journal"
    """,
    """ALTER TABLE "qq_migration_public_history_grants" DROP CONSTRAINT "fk_public_history_route"
    """,
    """ALTER TABLE "qq_migration_public_history_grants" DROP CONSTRAINT "fk_public_history_source"
    """,
    """ALTER TABLE "qq_migration_public_history_grants" DROP CONSTRAINT "qq_migration_public_history_grants_source_conversation_id_fkey"
    """,
    """ALTER TABLE "qq_migration_public_history_grants" DROP CONSTRAINT "qq_migration_public_history_grants_target_endpoint_key_fkey"
    """,
    """ALTER TABLE "qq_migration_public_history_snapshots" DROP CONSTRAINT "qq_migration_public_history_snapshots_grant_id_fkey"
    """,
    """ALTER TABLE "qq_official_admin_bindings" DROP CONSTRAINT "qq_official_admin_bindings_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_admin_bindings" DROP CONSTRAINT "qq_official_admin_bindings_admin_account_id_fkey"
    """,
    """ALTER TABLE "qq_official_admin_bindings" DROP CONSTRAINT "qq_official_admin_bindings_c2c_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_admin_bindings" DROP CONSTRAINT "qq_official_admin_bindings_owner_grant_id_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_admin_bindings" DROP CONSTRAINT "qq_official_admin_bindings_owner_grant_id_c2c_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_admin_control_receipts" DROP CONSTRAINT "qq_official_admin_control_rec_account_key_source_session_i_fkey"
    """,
    """ALTER TABLE "qq_official_admin_control_receipts" DROP CONSTRAINT "qq_official_admin_control_rec_inbox_id_account_key_endpoin_fkey"
    """,
    """ALTER TABLE "qq_official_admin_control_receipts" DROP CONSTRAINT "qq_official_admin_control_rec_inbox_id_endpoint_key_actor__fkey"
    """,
    """ALTER TABLE "qq_official_admin_control_receipts" DROP CONSTRAINT "qq_official_admin_control_receipts_approval_id_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_approval_id_account_key_endp_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_approval_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_approval_id_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_attempt_id_delivery_id_accou_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_delivery_id_plan_id_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_plan_id_account_key_endpoint_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_plan_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_admin_otp_sources" DROP CONSTRAINT "qq_official_admin_otp_sources_window_id_account_key_endpoi_fkey"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "fk_import_artifact_binding"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "fk_import_artifact_manifest"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "fk_import_artifact_source"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "fk_migration_project_artifact"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "qq_official_migration_artifac_artifact_id_source_extension_fkey"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "qq_official_migration_artifac_item_id_task_id_source_job_i_fkey"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "qq_official_migration_artifact_artifact_id_item_id_task_id_fkey"
    """,
    """ALTER TABLE "qq_official_migration_artifacts" DROP CONSTRAINT "qq_official_migration_artifact_mapping_id_mapping_revision_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_artifact_id_account_key_endp_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_artifact_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_artifact_id_source_extension_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_artifact_id_source_task_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_grant_id_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_grant_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_grant_id_endpoint_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_grant_id_principal_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_grant_id_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_io_operation_id_project_id_p_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_principal_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_artifacts" DROP CONSTRAINT "qq_official_project_artifacts_source_task_id_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_focus" DROP CONSTRAINT "qq_official_project_focus_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_focus" DROP CONSTRAINT "qq_official_project_focus_principal_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_focus" DROP CONSTRAINT "qq_official_project_focus_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_grants" DROP CONSTRAINT "qq_official_project_grants_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_grants" DROP CONSTRAINT "qq_official_project_grants_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_grants" DROP CONSTRAINT "qq_official_project_grants_owner_grant_id_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_grants" DROP CONSTRAINT "qq_official_project_grants_principal_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_grants" DROP CONSTRAINT "qq_official_project_grants_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_grants" DROP CONSTRAINT "qq_official_project_grants_project_id_project_scope_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_author_operation_id_project_id_prin_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_author_source_execution_id_endpoint_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_grant_id_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_grant_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_grant_id_endpoint_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_grant_id_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_operation_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_principal_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_source_execution_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_source_job_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_io_authority" DROP CONSTRAINT "qq_official_project_io_authority_turn_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_autho_source_io_operation_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_author_grant_id_endpoint_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authori_grant_id_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authorit_grant_id_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authorit_source_execution_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authority_grant_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authority_job_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authority_job_id_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authority_principal_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authority_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_authority_source_task_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_source_execution_id_endpoint_fkey"
    """,
    """ALTER TABLE "qq_official_project_lifecycle_authority" DROP CONSTRAINT "qq_official_project_lifecycle_source_task_id_endpoint_key__fkey"
    """,
    """ALTER TABLE "qq_official_project_task_authority" DROP CONSTRAINT "qq_official_project_task_auth_task_id_endpoint_key_actor_k_fkey"
    """,
    """ALTER TABLE "qq_official_project_task_authority" DROP CONSTRAINT "qq_official_project_task_authority_grant_id_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_task_authority" DROP CONSTRAINT "qq_official_project_task_authority_grant_id_actor_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_task_authority" DROP CONSTRAINT "qq_official_project_task_authority_grant_id_endpoint_key_fkey"
    """,
    """ALTER TABLE "qq_official_project_task_authority" DROP CONSTRAINT "qq_official_project_task_authority_grant_id_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_project_task_authority" DROP CONSTRAINT "qq_official_project_task_authority_task_id_project_id_fkey"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "fk_schedule_migration_job"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "fk_schedule_migration_kind"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "fk_schedule_plan_actor"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "fk_schedule_plan_endpoint"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "fk_schedule_reminder_target"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "fk_schedule_routine_target"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "fk_schedule_run_parent"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "qq_official_schedule_bindings_endpoint_key_account_key_fkey"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "qq_official_schedule_bindings_job_id_fkey"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "qq_official_schedule_bindings_parent_binding_id_fkey"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "qq_official_schedule_bindings_plan_id_fkey"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "qq_official_schedule_bindings_target_reminder_id_fkey"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "qq_official_schedule_bindings_target_routine_id_fkey"
    """,
    """ALTER TABLE "qq_official_schedule_bindings" DROP CONSTRAINT "qq_official_schedule_bindings_target_run_id_fkey"
    """,
    """ALTER TABLE "admin_accounts" DROP CONSTRAINT "ck_admin_identity_transport"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "ck_admin_approval_transport"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "uq_admin_official_source_actor"
    """,
    """ALTER TABLE "admin_approvals" DROP CONSTRAINT "uq_admin_official_source_recipient"
    """,
    """ALTER TABLE "agent_routines" DROP CONSTRAINT "ck_routine_scope"
    """,
    """ALTER TABLE "agent_routines" DROP CONSTRAINT "uq_routine_official_target"
    """,
    """ALTER TABLE "conversations" DROP CONSTRAINT "uq_migration_history_scope"
    """,
    """ALTER TABLE "deliveries" DROP CONSTRAINT "ck_deliveries_ephemeral"
    """,
    """ALTER TABLE "deliveries" DROP CONSTRAINT "ck_deliveries_official_fields"
    """,
    """ALTER TABLE "delivery_plans" DROP CONSTRAINT "ck_approval_ephemeral_manifest"
    """,
    """ALTER TABLE "delivery_plans" DROP CONSTRAINT "delivery_plans_approval_source_id_key"
    """,
    """ALTER TABLE "delivery_plans" DROP CONSTRAINT "delivery_plans_check2"
    """,
    """ALTER TABLE "delivery_plans" DROP CONSTRAINT "delivery_plans_purpose_check"
    """,
    """ALTER TABLE "project_io" DROP CONSTRAINT "uq_project_artifact_io"
    """,
    """ALTER TABLE "project_io" DROP CONSTRAINT "uq_project_io_authority_source"
    """,
    """ALTER TABLE "project_lifecycle_runs" DROP CONSTRAINT "uq_project_lifecycle_authority_job"
    """,
    """ALTER TABLE "project_task_bindings" DROP CONSTRAINT "uq_project_task_authority_source"
    """,
    """ALTER TABLE "projects" DROP CONSTRAINT "uq_migration_project_origin"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "ck_file_source_extension_kind"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "ck_file_source_union"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "uq_file_import_manifest"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "uq_file_migration_source"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "uq_project_artifact_kind"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP CONSTRAINT "uq_project_artifact_task"
    """,
    """ALTER TABLE "qq_official_generation_capacity" DROP CONSTRAINT "ck_capacity_source_union"
    """,
    """ALTER TABLE "qq_official_inbound_events" DROP CONSTRAINT "ck_admin_control_private_source"
    """,
    """ALTER TABLE "qq_official_inbound_events" DROP CONSTRAINT "qq_official_inbound_events_admin_control_actor_key_check"
    """,
    """ALTER TABLE "qq_official_inbound_events" DROP CONSTRAINT "qq_official_inbound_events_admin_control_commitment_check"
    """,
    """ALTER TABLE "qq_official_inbound_events" DROP CONSTRAINT "uq_admin_control_source_actor"
    """,
    """ALTER TABLE "qq_official_owner_grants" DROP CONSTRAINT "uq_qq_owner_grant_actor_m5"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP CONSTRAINT "ck_subagent_dispatch_source"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP CONSTRAINT "ck_subagent_execution_deadline"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP CONSTRAINT "qq_official_subagent_dispatches_migration_item_id_key"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP CONSTRAINT "qq_official_subagent_dispatches_tool_profile_check"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP CONSTRAINT "uq_migration_dispatch_job"
    """,
    """ALTER TABLE "reminders" DROP CONSTRAINT "ck_reminder_identity"
    """,
    """ALTER TABLE "reminders" DROP CONSTRAINT "uq_reminder_official_target"
    """,
    """ALTER TABLE "routine_runs" DROP CONSTRAINT "uq_routine_run_parent"
    """,
    """ALTER TABLE "subagent_runs" DROP CONSTRAINT "uq_migration_run_task"
    """,
    """ALTER TABLE "subagent_tasks" DROP CONSTRAINT "ck_subagent_identity_union"
    """,
    """ALTER TABLE "subagent_tasks" DROP CONSTRAINT "uq_subagent_migration_source"
    """,
    """DROP TABLE "qq_official_schedule_bindings"
    """,
    """DROP TABLE "qq_official_project_task_authority"
    """,
    """DROP TABLE "qq_official_project_lifecycle_authority"
    """,
    """DROP TABLE "qq_official_project_io_authority"
    """,
    """DROP TABLE "qq_official_project_grants"
    """,
    """DROP TABLE "qq_official_project_focus"
    """,
    """DROP TABLE "qq_official_project_artifacts"
    """,
    """DROP TABLE "qq_official_migration_artifacts"
    """,
    """DROP TABLE "qq_official_admin_otp_sources"
    """,
    """DROP TABLE "qq_official_admin_control_receipts"
    """,
    """DROP TABLE "qq_official_admin_bindings"
    """,
    """DROP TABLE "qq_migration_public_history_snapshots"
    """,
    """DROP TABLE "qq_migration_public_history_grants"
    """,
    """DROP TABLE "qq_migration_native_sources"
    """,
    """DROP TABLE "qq_migration_native_plan_sources"
    """,
    """DROP TABLE "migration_work_sources"
    """,
    """DROP TABLE "migration_verification_sources"
    """,
    """DROP TABLE "migration_verification_receipts"
    """,
    """DROP TABLE "migration_verification_intents"
    """,
    """DROP TABLE "migration_subagent_run_provenance"
    """,
    """DROP TABLE "migration_subagent_continuations"
    """,
    """DROP TABLE "migration_scope_aliases"
    """,
    """DROP TABLE "migration_runtime_baselines"
    """,
    """DROP TABLE "migration_routes"
    """,
    """DROP TABLE "migration_project_grants"
    """,
    """DROP TABLE "migration_preview_revisions"
    """,
    """DROP TABLE "migration_journals"
    """,
    """DROP TABLE "migration_items"
    """,
    """DROP TABLE "migration_item_plans"
    """,
    """DROP TABLE "migration_file_continuations"
    """,
    """DROP TABLE "migration_events"
    """,
    """DROP TABLE "migration_dispatch_activities"
    """,
    """DROP TABLE "migration_control_audit"
    """,
    """DROP TABLE "migration_artifact_plans"
    """,
    """DROP TABLE "migration_actor_mappings"
    """,
    """DROP TABLE "migration_actor_mapping_revisions"
    """,
    """DROP TABLE "migration_activity_observations"
    """,
    """DROP INDEX "ix_admin_official_queue"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP COLUMN "project_artifact_id"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP COLUMN "migration_artifact_id"
    """,
    """ALTER TABLE "subagent_tasks" DROP COLUMN "official_project_id"
    """,
    """ALTER TABLE "admin_accounts" DROP COLUMN "identity_mode"
    """,
    """ALTER TABLE "admin_approvals" ALTER COLUMN "qq_id" SET NOT NULL
    """,
    """ALTER TABLE "admin_approvals" ALTER COLUMN "bot_id" SET NOT NULL
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "identity_transport"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_account_key"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_endpoint_key"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_actor_key"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_admin_binding_id"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_binding_revision"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_source_endpoint_key"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_source_actor_key"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_source_scope_key"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_execution_id"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_task_id"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_source_revision"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_route_epoch"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_endpoint_route_epoch"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_source_deadline_at"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_policy_json"
    """,
    """ALTER TABLE "admin_approvals" DROP COLUMN "official_policy_digest"
    """,
    """ALTER TABLE "agent_routines" DROP COLUMN "account_key"
    """,
    """ALTER TABLE "agent_routines" DROP COLUMN "endpoint_key"
    """,
    """ALTER TABLE "agent_routines" DROP COLUMN "creator_actor_key"
    """,
    """ALTER TABLE "agent_routines" DROP COLUMN "endpoint_generation"
    """,
    """ALTER TABLE "agent_routines" DROP COLUMN "endpoint_route_epoch"
    """,
    """ALTER TABLE "agent_routines" DROP COLUMN "budget_origin_routine_id"
    """,
    """ALTER TABLE "delivery_plans" DROP COLUMN "approval_source_id"
    """,
    """ALTER TABLE "delivery_plans" DROP COLUMN "approval_source_expires_at"
    """,
    """ALTER TABLE "qq_official_file_artifacts" ALTER COLUMN "source_execution_id" SET NOT NULL
    """,
    """ALTER TABLE "qq_official_file_artifacts" ALTER COLUMN "source_canonical_message_id" SET NOT NULL
    """,
    """ALTER TABLE "qq_official_file_artifacts" ALTER COLUMN "source_native_message_id" SET NOT NULL
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP COLUMN "source_extension_kind"
    """,
    """ALTER TABLE "qq_official_file_artifacts" DROP COLUMN "source_migration_item_id"
    """,
    """ALTER TABLE "qq_official_generation_capacity" ALTER COLUMN "source_execution_id" SET NOT NULL
    """,
    """ALTER TABLE "qq_official_generation_capacity" DROP COLUMN "source_migration_item_id"
    """,
    """ALTER TABLE "qq_official_generation_capacity" DROP COLUMN "source_task_id"
    """,
    """ALTER TABLE "qq_official_generation_capacity" DROP COLUMN "source_task_revision"
    """,
    """ALTER TABLE "qq_official_generation_capacity" DROP COLUMN "source_job_id"
    """,
    """ALTER TABLE "qq_official_inbound_events" DROP COLUMN "admin_control_actor_key"
    """,
    """ALTER TABLE "qq_official_inbound_events" DROP COLUMN "admin_control_commitment"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" ALTER COLUMN "source_execution_id" SET NOT NULL
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" ALTER COLUMN "source_canonical_message_id" SET NOT NULL
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" ALTER COLUMN "source_native_message_id" SET NOT NULL
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP COLUMN "migration_item_id"
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" DROP COLUMN "migration_result_deadline_at"
    """,
    """ALTER TABLE "reminders" DROP COLUMN "account_key"
    """,
    """ALTER TABLE "reminders" DROP COLUMN "endpoint_key"
    """,
    """ALTER TABLE "reminders" DROP COLUMN "creator_actor_key"
    """,
    """ALTER TABLE "reminders" DROP COLUMN "endpoint_generation"
    """,
    """ALTER TABLE "reminders" DROP COLUMN "endpoint_route_epoch"
    """,
    """ALTER TABLE "subagent_tasks" DROP COLUMN "migration_item_id"
    """,
    """ALTER TABLE "subagent_tasks" ADD CONSTRAINT "ck_subagent_identity_union" CHECK ((((requester_user_id IS NOT NULL) AND (account_key IS NULL) AND (endpoint_key IS NULL) AND (requester_actor_key IS NULL)) OR ((requester_user_id IS NULL) AND (account_key IS NOT NULL) AND (endpoint_key IS NOT NULL) AND (requester_actor_key IS NOT NULL) AND (trigger_message_id IS NOT NULL) AND (project_id IS NULL))))
    """,
    """ALTER TABLE "admin_accounts" ADD CONSTRAINT "admin_accounts_check" CHECK (((role <> 'admin'::text) OR (qq_id IS NOT NULL)))
    """,
    """ALTER TABLE "delivery_plans" ADD CONSTRAINT "delivery_plans_purpose_check" CHECK ((purpose = ANY (ARRAY['final'::text, 'requested_artifact'::text, 'background_result'::text, 'progress'::text, 'unsolicited'::text, 'binding_otp'::text])))
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" ADD CONSTRAINT "qq_official_subagent_dispatches_check1" CHECK ((execution_deadline_at > created_at))
    """,
    """ALTER TABLE "delivery_plans" ADD CONSTRAINT "delivery_plans_check2" CHECK ((purpose='binding_otp' AND payload_mode='ephemeral' AND challenge_id IS NOT NULL
                AND challenge_expires_at IS NOT NULL AND expires_at<=challenge_expires_at
                AND part_count=1 AND digest_scheme='hmac_sha256_v1'
                AND manifest_json='{"v":1,"ephemeral":true}')
            OR (purpose<>'binding_otp' AND payload_mode IN ('persistent','protected') AND challenge_id IS NULL AND challenge_expires_at IS NULL
                AND digest_scheme='sha256'))
    """,
    """ALTER TABLE "qq_official_subagent_dispatches" ADD CONSTRAINT "qq_official_subagent_dispatches_tool_profile_check" CHECK ((tool_profile = ANY (ARRAY['official-text-v1'::text, 'official-files-v1'::text])))
    """,
    """ALTER TABLE "deliveries" ADD CONSTRAINT "ck_deliveries_ephemeral" CHECK (account_key IS NULL OR (
        (payload_mode IN ('persistent','protected') AND purpose<>'binding_otp' AND digest_scheme='sha256')
        OR (payload_mode='ephemeral' AND purpose='binding_otp' AND part_index=0
            AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL
            AND body_json='{"v":1,"nodes":[]}' AND content_fingerprint=''
            AND body_archive_path IS NULL AND body_archive_sha256 IS NULL
            AND body_size_bytes IS NULL AND body_archived_at IS NULL
            AND auth_retries_used=0 AND transient_retries_used=0 AND NOT format_fallback_used
            AND last_error='')))
    """,
    """ALTER TABLE "deliveries" ADD CONSTRAINT "ck_deliveries_official_fields" CHECK (account_key IS NULL OR (
        octet_length(requester_actor_key) BETWEEN 1 AND 1280
        AND part_index BETWEEN 0 AND 15 AND endpoint_generation>=1 AND route_epoch>=1 AND capability_revision>=1
        AND purpose IN ('final','requested_artifact','background_result','progress','unsolicited','binding_otp')
        AND payload_mode IN ('persistent','ephemeral','protected') AND variant IN ('primary','plain_fallback')
        AND payload_digest ~ '^[a-f0-9]{64}$' AND digest_scheme IN ('sha256','hmac_sha256_v1')
        AND ((digest_scheme='sha256' AND digest_key_id IS NULL)
            OR (digest_scheme='hmac_sha256_v1' AND digest_key_id ~ '^[A-Za-z0-9_-]{1,64}$'))
        AND expires_at>created_at AND auth_retries_used BETWEEN 0 AND 1 AND transient_retries_used>=0
        AND (wait_reason IS NULL OR wait_reason ~ '^[a-z0-9_]{1,80}$')
        AND (claim_epoch IS NULL OR claim_epoch>0)
        AND ((claim_epoch IS NULL)=(current_attempt_id IS NULL))
        AND (status NOT IN ('sending','accepted','committed','ambiguous') OR current_attempt_id IS NOT NULL)
        AND (status<>'committed' OR octet_length(native_message_id) BETWEEN 1 AND 256)
        AND ((variant='plain_fallback')=format_fallback_used)))
    """,
    """ALTER TABLE "agent_routines" ADD CONSTRAINT "ck_routine_scope" CHECK (((platform = 'onebot-v11'::text) AND (kind = ANY (ARRAY['group'::text, 'private'::text]))))
    """,
)

NEW_TABLES = ('migration_activity_observations', 'migration_actor_mapping_revisions', 'migration_actor_mappings', 'migration_artifact_plans', 'migration_control_audit', 'migration_dispatch_activities', 'migration_events', 'migration_file_continuations', 'migration_item_plans', 'migration_items', 'migration_journals', 'migration_preview_revisions', 'migration_project_grants', 'migration_routes', 'migration_runtime_baselines', 'migration_scope_aliases', 'migration_subagent_continuations', 'migration_subagent_run_provenance', 'migration_verification_intents', 'migration_verification_receipts', 'migration_verification_sources', 'migration_work_sources', 'qq_migration_native_plan_sources', 'qq_migration_native_sources', 'qq_migration_public_history_grants', 'qq_migration_public_history_snapshots', 'qq_official_admin_bindings', 'qq_official_admin_control_receipts', 'qq_official_admin_otp_sources', 'qq_official_migration_artifacts', 'qq_official_project_artifacts', 'qq_official_project_focus', 'qq_official_project_grants', 'qq_official_project_io_authority', 'qq_official_project_lifecycle_authority', 'qq_official_project_task_authority', 'qq_official_schedule_bindings')

AFFECTED_TABLES = ('admin_accounts', 'admin_approvals', 'agent_routines', 'conversations', 'deliveries', 'delivery_plans', 'migration_activity_observations', 'migration_actor_mapping_revisions', 'migration_actor_mappings', 'migration_artifact_plans', 'migration_control_audit', 'migration_dispatch_activities', 'migration_events', 'migration_file_continuations', 'migration_item_plans', 'migration_items', 'migration_journals', 'migration_preview_revisions', 'migration_project_grants', 'migration_routes', 'migration_runtime_baselines', 'migration_scope_aliases', 'migration_subagent_continuations', 'migration_subagent_run_provenance', 'migration_verification_intents', 'migration_verification_receipts', 'migration_verification_sources', 'migration_work_sources', 'project_io', 'project_lifecycle_runs', 'project_task_bindings', 'projects', 'qq_migration_native_plan_sources', 'qq_migration_native_sources', 'qq_migration_public_history_grants', 'qq_migration_public_history_snapshots', 'qq_official_admin_bindings', 'qq_official_admin_control_receipts', 'qq_official_admin_otp_sources', 'qq_official_file_artifacts', 'qq_official_generation_capacity', 'qq_official_inbound_events', 'qq_official_migration_artifacts', 'qq_official_owner_grants', 'qq_official_project_artifacts', 'qq_official_project_focus', 'qq_official_project_grants', 'qq_official_project_io_authority', 'qq_official_project_lifecycle_authority', 'qq_official_project_task_authority', 'qq_official_schedule_bindings', 'qq_official_subagent_dispatches', 'reminders', 'routine_runs', 'subagent_runs', 'subagent_tasks')

HAS_EVIDENCE = """SELECT
    EXISTS(SELECT 1 FROM migration_activity_observations)
    OR EXISTS(SELECT 1 FROM migration_actor_mapping_revisions)
    OR EXISTS(SELECT 1 FROM migration_actor_mappings)
    OR EXISTS(SELECT 1 FROM migration_artifact_plans)
    OR EXISTS(SELECT 1 FROM migration_control_audit)
    OR EXISTS(SELECT 1 FROM migration_dispatch_activities)
    OR EXISTS(SELECT 1 FROM migration_events)
    OR EXISTS(SELECT 1 FROM migration_file_continuations)
    OR EXISTS(SELECT 1 FROM migration_item_plans)
    OR EXISTS(SELECT 1 FROM migration_items)
    OR EXISTS(SELECT 1 FROM migration_journals)
    OR EXISTS(SELECT 1 FROM migration_preview_revisions)
    OR EXISTS(SELECT 1 FROM migration_project_grants)
    OR EXISTS(SELECT 1 FROM migration_routes)
    OR EXISTS(SELECT 1 FROM migration_runtime_baselines)
    OR EXISTS(SELECT 1 FROM migration_scope_aliases)
    OR EXISTS(SELECT 1 FROM migration_subagent_continuations)
    OR EXISTS(SELECT 1 FROM migration_subagent_run_provenance)
    OR EXISTS(SELECT 1 FROM migration_verification_intents)
    OR EXISTS(SELECT 1 FROM migration_verification_receipts)
    OR EXISTS(SELECT 1 FROM migration_verification_sources)
    OR EXISTS(SELECT 1 FROM migration_work_sources)
    OR EXISTS(SELECT 1 FROM qq_migration_native_plan_sources)
    OR EXISTS(SELECT 1 FROM qq_migration_native_sources)
    OR EXISTS(SELECT 1 FROM qq_migration_public_history_grants)
    OR EXISTS(SELECT 1 FROM qq_migration_public_history_snapshots)
    OR EXISTS(SELECT 1 FROM qq_official_admin_bindings)
    OR EXISTS(SELECT 1 FROM qq_official_admin_control_receipts)
    OR EXISTS(SELECT 1 FROM qq_official_admin_otp_sources)
    OR EXISTS(SELECT 1 FROM qq_official_migration_artifacts)
    OR EXISTS(SELECT 1 FROM qq_official_project_artifacts)
    OR EXISTS(SELECT 1 FROM qq_official_project_focus)
    OR EXISTS(SELECT 1 FROM qq_official_project_grants)
    OR EXISTS(SELECT 1 FROM qq_official_project_io_authority)
    OR EXISTS(SELECT 1 FROM qq_official_project_lifecycle_authority)
    OR EXISTS(SELECT 1 FROM qq_official_project_task_authority)
    OR EXISTS(SELECT 1 FROM qq_official_schedule_bindings)
    OR EXISTS(SELECT 1 FROM admin_accounts WHERE identity_mode<>'legacy_qq')
    OR EXISTS(SELECT 1 FROM admin_approvals WHERE identity_transport<>'onebot')
    OR EXISTS(SELECT 1 FROM delivery_plans WHERE approval_source_id IS NOT NULL OR purpose='approval_otp')
    OR EXISTS(SELECT 1 FROM qq_official_inbound_events WHERE admin_control_actor_key IS NOT NULL OR admin_control_commitment IS NOT NULL)
    OR EXISTS(SELECT 1 FROM subagent_tasks WHERE migration_item_id IS NOT NULL OR official_project_id IS NOT NULL)
    OR EXISTS(SELECT 1 FROM qq_official_subagent_dispatches WHERE migration_item_id IS NOT NULL OR tool_profile IN ('official-projects-v1','official-host-v1'))
    OR EXISTS(SELECT 1 FROM reminders WHERE account_key IS NOT NULL)
    OR EXISTS(SELECT 1 FROM agent_routines WHERE account_key IS NOT NULL OR budget_origin_routine_id IS NOT NULL)"""

def _namespace():
    name = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise RuntimeError("invalid_migration_schema")
    op.execute(sa.text('SET LOCAL search_path TO "' + name + '",public'))


def _execute(statement):
    # Fixed DDL only. Keep literal JSON colons out of SQLAlchemy's bind parser.
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade():
    _namespace()
    for statement in UPGRADE:
        _execute(statement)


def downgrade():
    _namespace()
    _execute("SET LOCAL lock_timeout TO '5s'")
    # Close the check/drop race with concurrent admissions and receipts. These
    # names are a fixed migration manifest, never an operator-supplied selector.
    _execute("LOCK TABLE " + ",".join(AFFECTED_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().execute(sa.text(HAS_EVIDENCE)).scalar():
        raise RuntimeError("migration_evidence_requires_export")
    for statement in DOWNGRADE:
        _execute(statement)
