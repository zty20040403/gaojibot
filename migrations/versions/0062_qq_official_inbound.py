"""Durable official ingress, endpoint fences, and explicit owner binding.

No changes to legacy ledger rows, queue records, or security identities.
"""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0062_qq_official_inbound"
down_revision = "0061_platform_actor_identity"
branch_labels = None
depends_on = None


def _schema() -> str:
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


# @S@ is replaced only with a validated, quoted identifier. All persisted key
# bounds are bytes, never truncation; composite btree entries remain bounded.
TABLES = (
    ("qq_official_accounts", """
        account_key TEXT PRIMARY KEY CHECK (octet_length(account_key) BETWEEN 1 AND 512),
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        fencing_epoch BIGINT NOT NULL DEFAULT 0 CHECK (fencing_epoch >= 0),
        holder_id TEXT, lock_backend_pid INTEGER,
        session_id TEXT CHECK (octet_length(session_id) BETWEEN 1 AND 512),
        durable_seq BIGINT CHECK (durable_seq >= 0),
        gateway_state TEXT NOT NULL DEFAULT 'disabled'
            CHECK (gateway_state IN ('disabled','idle','connecting','active','paused','offline')),
        pause_reason TEXT CHECK (pause_reason ~ '^[a-z0-9_]{1,80}$'),
        config_revision BIGINT NOT NULL DEFAULT 1 CHECK (config_revision >= 1),
        bot_native_id TEXT CHECK (octet_length(bot_native_id) BETWEEN 1 AND 256),
        bot_identity_source TEXT CHECK (bot_identity_source ~ '^[a-z0-9_]{1,80}$'),
        bot_identity_verified_at BIGINT,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        CHECK ((session_id IS NULL) = (durable_seq IS NULL)),
        CHECK ((holder_id IS NULL) = (lock_backend_pid IS NULL)),
        CHECK (holder_id IS NULL OR (length(holder_id)=32 AND lock_backend_pid > 0)),
        CHECK ((bot_native_id IS NULL AND bot_identity_source IS NULL AND bot_identity_verified_at IS NULL)
            OR (bot_native_id IS NOT NULL AND bot_identity_source IS NOT NULL AND bot_identity_verified_at IS NOT NULL))
    """),
    ("qq_official_endpoints", """
        endpoint_key TEXT PRIMARY KEY CHECK (octet_length(endpoint_key) BETWEEN 1 AND 1000),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        kind TEXT NOT NULL CHECK (kind IN ('group','private')),
        native_conversation_id TEXT NOT NULL CHECK (octet_length(native_conversation_id) BETWEEN 1 AND 256),
        canonical_scope_key TEXT NOT NULL CHECK (octet_length(canonical_scope_key) BETWEEN 1 AND 1000),
        binding_state TEXT NOT NULL DEFAULT 'unbound' CHECK (binding_state IN ('unbound','active','revoked')),
        membership_state TEXT NOT NULL DEFAULT 'unknown' CHECK (membership_state IN ('unknown','present','absent')),
        generation BIGINT NOT NULL DEFAULT 1 CHECK (generation >= 1),
        route_epoch BIGINT NOT NULL DEFAULT 1 CHECK (route_epoch >= 1),
        capability_revision BIGINT NOT NULL DEFAULT 1 CHECK (capability_revision >= 1),
        proactive_state TEXT NOT NULL DEFAULT 'unknown' CHECK (proactive_state IN ('unknown','allowed','denied')),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        UNIQUE(account_key, kind, native_conversation_id),
        UNIQUE(endpoint_key, account_key)
    """),
    ("qq_official_inbound_events", """
        inbox_id TEXT PRIMARY KEY CHECK (length(inbox_id) BETWEEN 1 AND 64),
        accepted_order BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        endpoint_key TEXT, endpoint_generation BIGINT, route_epoch BIGINT,
        event_type TEXT NOT NULL CHECK (event_type ~ '^[A-Z0-9_]{1,128}$'),
        native_message_id TEXT CHECK (octet_length(native_message_id) BETWEEN 1 AND 256),
        event_id TEXT CHECK (octet_length(event_id) BETWEEN 1 AND 256),
        payload_json TEXT CHECK (octet_length(payload_json) <= 262144),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processed','quarantined','revoked','skipped')),
        reason_code TEXT CHECK (reason_code ~ '^[a-z0-9_]{1,80}$'),
        trigger_requested BOOLEAN NOT NULL DEFAULT FALSE,
        echo_state TEXT NOT NULL DEFAULT 'unknown' CHECK (echo_state IN ('unknown','not_echo','possible_self','confirmed_self')),
        ingest_job_id BIGINT REFERENCES @S@.durable_jobs(job_id),
        dispatch_job_id BIGINT REFERENCES @S@.durable_jobs(job_id),
        canonical_message_id BIGINT REFERENCES @S@.messages(canonical_message_id),
        received_at BIGINT NOT NULL, payload_expires_at BIGINT,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, processed_at BIGINT,
        UNIQUE(inbox_id, account_key),
        FOREIGN KEY(endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key),
        CHECK ((endpoint_key IS NULL AND endpoint_generation IS NULL AND route_epoch IS NULL)
            OR (endpoint_key IS NOT NULL AND endpoint_generation IS NOT NULL AND route_epoch IS NOT NULL
                AND endpoint_generation >= 1 AND route_epoch >= 1)),
        CHECK (payload_json IS NULL OR payload_expires_at IS NOT NULL)
    """),
    ("qq_official_gateway_receipts", """
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        session_id TEXT NOT NULL CHECK (octet_length(session_id) BETWEEN 1 AND 512),
        seq BIGINT NOT NULL CHECK (seq >= 0),
        event_type TEXT NOT NULL CHECK (event_type ~ '^[A-Z0-9_]{1,128}$'),
        disposition TEXT NOT NULL CHECK (disposition IN ('inbox','duplicate','control','skipped','quarantined')),
        inbox_id TEXT, received_at BIGINT NOT NULL,
        PRIMARY KEY(account_key, session_id, seq),
        FOREIGN KEY(inbox_id, account_key) REFERENCES @S@.qq_official_inbound_events(inbox_id, account_key)
    """),
    ("qq_official_inbound_dedup", """
        dedup_key TEXT PRIMARY KEY CHECK (dedup_key ~ '^[a-f0-9]{64}$'),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        key_kind TEXT NOT NULL CHECK (key_kind IN ('event','message')),
        endpoint_key TEXT,
        native_id TEXT NOT NULL CHECK (octet_length(native_id) BETWEEN 1 AND 256),
        inbox_id TEXT NOT NULL, created_at BIGINT NOT NULL,
        FOREIGN KEY(endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key),
        FOREIGN KEY(inbox_id, account_key) REFERENCES @S@.qq_official_inbound_events(inbox_id, account_key),
        CHECK ((key_kind='message' AND endpoint_key IS NOT NULL) OR (key_kind='event' AND endpoint_key IS NULL))
    """),
    ("qq_official_messages", """
        message_id TEXT PRIMARY KEY CHECK (length(message_id) BETWEEN 1 AND 64),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL,
        native_message_id TEXT NOT NULL CHECK (octet_length(native_message_id) BETWEEN 1 AND 256),
        canonical_message_id BIGINT REFERENCES @S@.messages(canonical_message_id),
        direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
        msg_idx TEXT CHECK (octet_length(msg_idx) BETWEEN 1 AND 256),
        ref_msg_idx TEXT CHECK (octet_length(ref_msg_idx) BETWEEN 1 AND 256),
        inbox_id TEXT, created_at BIGINT NOT NULL,
        UNIQUE(endpoint_key, native_message_id),
        FOREIGN KEY(endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key),
        FOREIGN KEY(inbox_id, account_key) REFERENCES @S@.qq_official_inbound_events(inbox_id, account_key)
    """),
    ("qq_official_message_indexes", """
        endpoint_key TEXT NOT NULL,
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        index_kind TEXT NOT NULL CHECK (index_kind IN ('msg_idx','ref_idx')),
        index_value TEXT NOT NULL CHECK (octet_length(index_value) BETWEEN 1 AND 256),
        native_message_id TEXT NOT NULL CHECK (octet_length(native_message_id) BETWEEN 1 AND 256),
        canonical_message_id BIGINT REFERENCES @S@.messages(canonical_message_id),
        ambiguous BOOLEAN NOT NULL DEFAULT FALSE,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        PRIMARY KEY(endpoint_key, index_kind, index_value),
        FOREIGN KEY(endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key),
        FOREIGN KEY(endpoint_key, native_message_id) REFERENCES @S@.qq_official_messages(endpoint_key, native_message_id)
    """),
    ("qq_official_reply_windows", """
        window_id TEXT PRIMARY KEY CHECK (length(window_id) BETWEEN 1 AND 64),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL,
        endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation >= 1),
        route_epoch BIGINT NOT NULL CHECK (route_epoch >= 1),
        credential_kind TEXT NOT NULL CHECK (credential_kind IN ('msg_id','event_id')),
        credential_value TEXT NOT NULL CHECK (octet_length(credential_value) BETWEEN 1 AND 256),
        issued_at BIGINT, received_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
        max_replies INTEGER NOT NULL CHECK (max_replies >= 1),
        used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
        reserved_count INTEGER NOT NULL DEFAULT 0 CHECK (reserved_count >= 0),
        next_seq BIGINT NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
        state TEXT NOT NULL DEFAULT 'expired' CHECK (state IN ('active','expired','revoked')),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        UNIQUE(endpoint_key, credential_kind, credential_value),
        FOREIGN KEY(endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key),
        CHECK (used_count + reserved_count <= max_replies),
        CHECK (state<>'active' OR (issued_at IS NOT NULL AND expires_at>issued_at))
    """),
    ("qq_official_owner_grants", """
        grant_id TEXT PRIMARY KEY CHECK (length(grant_id) BETWEEN 1 AND 64),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        c2c_actor_key TEXT NOT NULL CHECK (octet_length(c2c_actor_key) BETWEEN 1 AND 1280),
        source TEXT NOT NULL CHECK (source IN ('config','console')),
        source_ref TEXT NOT NULL CHECK (octet_length(source_ref) <= 256),
        revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
        state TEXT NOT NULL CHECK (state IN ('active','revoked')),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, revoked_at BIGINT,
        UNIQUE(account_key, c2c_actor_key), UNIQUE(grant_id, account_key)
    """),
    ("qq_official_binding_challenges", """
        challenge_id TEXT PRIMARY KEY CHECK (length(challenge_id) BETWEEN 1 AND 64),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        issuer_actor_key TEXT NOT NULL CHECK (octet_length(issuer_actor_key) BETWEEN 1 AND 1280),
        issuer_endpoint_key TEXT NOT NULL,
        owner_grant_id TEXT NOT NULL, owner_revision BIGINT NOT NULL CHECK (owner_revision >= 1),
        code_hmac TEXT NOT NULL CHECK (code_hmac='' OR code_hmac ~ '^[a-f0-9]{64}$'),
        expires_at BIGINT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('sending','issued','candidate','confirmed','rejected','cancelled','expired','locked')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, consumed_at BIGINT,
        delivery_epoch BIGINT NOT NULL CHECK (delivery_epoch >= 1),
        UNIQUE(challenge_id, account_key),
        FOREIGN KEY(owner_grant_id, account_key) REFERENCES @S@.qq_official_owner_grants(grant_id, account_key),
        FOREIGN KEY(issuer_endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key)
    """),
    ("qq_official_binding_candidates", """
        candidate_id TEXT PRIMARY KEY CHECK (length(candidate_id) BETWEEN 1 AND 64),
        challenge_id TEXT NOT NULL UNIQUE,
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL,
        actor_key TEXT NOT NULL CHECK (octet_length(actor_key) BETWEEN 1 AND 1280),
        endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation >= 1),
        version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
        state TEXT NOT NULL CHECK (state IN ('pending','confirmed','rejected','expired','cancelled')),
        expires_at BIGINT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        confirmed_by TEXT CHECK (octet_length(confirmed_by) <= 1280),
        grant_submitter_owner BOOLEAN, consumed_at BIGINT,
        UNIQUE(candidate_id, account_key),
        FOREIGN KEY(challenge_id, account_key) REFERENCES @S@.qq_official_binding_challenges(challenge_id, account_key),
        FOREIGN KEY(endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key)
    """),
    ("qq_official_owner_actor_links", """
        endpoint_key TEXT NOT NULL,
        actor_key TEXT NOT NULL CHECK (octet_length(actor_key) BETWEEN 1 AND 1280),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        owner_grant_id TEXT NOT NULL, owner_revision BIGINT NOT NULL CHECK (owner_revision >= 1),
        endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation >= 1),
        revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
        state TEXT NOT NULL CHECK (state IN ('active','revoked')),
        candidate_id TEXT NOT NULL,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        PRIMARY KEY(endpoint_key, actor_key),
        FOREIGN KEY(endpoint_key, account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key, account_key),
        FOREIGN KEY(owner_grant_id, account_key) REFERENCES @S@.qq_official_owner_grants(grant_id, account_key),
        FOREIGN KEY(candidate_id, account_key) REFERENCES @S@.qq_official_binding_candidates(candidate_id, account_key)
    """),
    ("qq_official_binding_rate_limits", """
        bucket TEXT PRIMARY KEY CHECK (bucket ~ '^[a-f0-9]{64}$'),
        window_start BIGINT NOT NULL, hits INTEGER NOT NULL CHECK (hits >= 0)
    """),
)

INDEXES = (
    "CREATE UNIQUE INDEX uq_qq_official_active_route ON @S@.qq_official_endpoints(canonical_scope_key) WHERE binding_state='active'",
    "CREATE INDEX ix_qq_official_inbox_order ON @S@.qq_official_inbound_events(endpoint_key,accepted_order) WHERE status='pending'",
    "CREATE INDEX ix_qq_official_inbox_retention ON @S@.qq_official_inbound_events(payload_expires_at) WHERE payload_json IS NOT NULL",
    "CREATE INDEX ix_qq_official_receipts_inbox ON @S@.qq_official_gateway_receipts(inbox_id)",
    "CREATE INDEX ix_qq_official_dedup_inbox ON @S@.qq_official_inbound_dedup(inbox_id)",
    "CREATE INDEX ix_qq_official_msg_idx ON @S@.qq_official_messages(endpoint_key,msg_idx) WHERE msg_idx IS NOT NULL",
    "CREATE INDEX ix_qq_official_window_active ON @S@.qq_official_reply_windows(endpoint_key,expires_at) WHERE state='active'",
    "CREATE UNIQUE INDEX uq_qq_official_challenge_hmac ON @S@.qq_official_binding_challenges(account_key,code_hmac) WHERE code_hmac<>''",
    "CREATE UNIQUE INDEX uq_qq_official_challenge_live_owner ON @S@.qq_official_binding_challenges(owner_grant_id) WHERE state IN ('sending','issued','candidate')",
    "CREATE INDEX ix_qq_official_candidate_endpoint ON @S@.qq_official_binding_candidates(endpoint_key,state)",
    "CREATE INDEX ix_qq_official_owner_links_grant ON @S@.qq_official_owner_actor_links(owner_grant_id,state)",
)


def upgrade() -> None:
    quoted = '"' + _schema() + '"'
    for name, body in TABLES:
        op.execute(sa.text(f'CREATE TABLE {quoted}."{name}" ({body.replace("@S@", quoted)})'))
    for statement in INDEXES:
        op.execute(sa.text(statement.replace("@S@", quoted)))


def downgrade() -> None:
    schema = _schema()
    quoted = '"' + schema + '"'
    connection = op.get_bind()
    # Preserve evidence even when a deployment is disabled. Downgrade is not the
    # rollback mechanism for a populated account; stop the runtime instead.
    for name, _ in TABLES:
        if connection.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {quoted}."{name}")')).scalar():
            raise RuntimeError("Export official ingress and authorization evidence before downgrade")
    for name, _ in reversed(TABLES):
        op.drop_table(name, schema=schema)
