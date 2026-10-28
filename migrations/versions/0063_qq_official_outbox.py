"""Account-fenced official parts and attempts in the existing delivery queue.

No runtime or sender is enabled by this migration. Plans are
immutable manifests, receipts are evidence, and quota rows are accounting.
The application must exclude official ownership from legacy queue operations.
"""
from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0063_qq_official_outbox"
down_revision = "0062_qq_official_inbound"
branch_labels = None
depends_on = None

LEGACY_STATUSES = "'pending','sending','ambiguous','committed','failed','cancelled'"
OFFICIAL_STATUSES = LEGACY_STATUSES + ",'accepted'"
EPHEMERAL_BODY = '{"v":1,"nodes":[]}'
EPHEMERAL_MANIFEST = '{"v":1,"ephemeral":true}'


def _schema() -> str:
    value = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("Invalid PostgreSQL schema")
    return value


TABLES = (
    ("delivery_plans", """
        plan_id TEXT PRIMARY KEY CHECK (length(plan_id) BETWEEN 1 AND 64),
        idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key ~ '^qqp1:[a-f0-9]{64}$'),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL,
        requester_actor_key TEXT NOT NULL CHECK (octet_length(requester_actor_key) BETWEEN 1 AND 1280),
        source_scope_key TEXT NOT NULL CHECK (octet_length(source_scope_key) BETWEEN 1 AND 1000),
        source_canonical_message_id BIGINT REFERENCES @S@.messages(canonical_message_id),
        turn_id BIGINT REFERENCES @S@.agent_turns(turn_id),
        endpoint_generation BIGINT NOT NULL CHECK (endpoint_generation >= 1),
        route_epoch BIGINT NOT NULL CHECK (route_epoch >= 1),
        capability_revision BIGINT NOT NULL CHECK (capability_revision >= 1),
        purpose TEXT NOT NULL CHECK (purpose IN
            ('final','requested_artifact','background_result','progress','unsolicited','binding_otp')),
        payload_mode TEXT NOT NULL CHECK (payload_mode IN ('persistent','ephemeral')),
        manifest_json TEXT NOT NULL CHECK (octet_length(manifest_json) BETWEEN 1 AND 4194304),
        manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[a-f0-9]{64}$'),
        digest_scheme TEXT NOT NULL CHECK (digest_scheme IN ('sha256','hmac_sha256_v1')),
        digest_key_id TEXT CHECK (digest_key_id ~ '^[A-Za-z0-9_-]{1,64}$'),
        part_count INTEGER NOT NULL CHECK (part_count BETWEEN 1 AND 16),
        expires_at BIGINT NOT NULL, priority INTEGER NOT NULL,
        challenge_id TEXT UNIQUE, challenge_expires_at BIGINT, created_at BIGINT NOT NULL,
        UNIQUE(plan_id,account_key,endpoint_key),
        UNIQUE(plan_id,payload_mode,purpose),
        UNIQUE(plan_id,requester_actor_key),
        FOREIGN KEY(endpoint_key,account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key,account_key),
        FOREIGN KEY(challenge_id,account_key) REFERENCES @S@.qq_official_binding_challenges(challenge_id,account_key),
        FOREIGN KEY(challenge_id,endpoint_key,challenge_expires_at)
            REFERENCES @S@.qq_official_binding_challenges(challenge_id,issuer_endpoint_key,expires_at),
        FOREIGN KEY(challenge_id,requester_actor_key)
            REFERENCES @S@.qq_official_binding_challenges(challenge_id,issuer_actor_key),
        CHECK (expires_at > created_at),
        CHECK ((digest_scheme='sha256' AND digest_key_id IS NULL)
            OR (digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL)),
        CHECK ((purpose='binding_otp' AND payload_mode='ephemeral' AND challenge_id IS NOT NULL
                AND challenge_expires_at IS NOT NULL AND expires_at<=challenge_expires_at
                AND part_count=1 AND digest_scheme='hmac_sha256_v1'
                AND manifest_json='{"v":1,"ephemeral":true}')
            OR (purpose<>'binding_otp' AND payload_mode='persistent' AND challenge_id IS NULL AND challenge_expires_at IS NULL
                AND digest_scheme='sha256'))
    """),
    ("qq_official_quota_periods", """
        quota_key TEXT PRIMARY KEY CHECK (quota_key ~ '^[a-f0-9]{64}$'),
        account_key TEXT NOT NULL REFERENCES @S@.qq_official_accounts(account_key),
        endpoint_key TEXT,
        quota_scope_key TEXT GENERATED ALWAYS AS (COALESCE(endpoint_key,'')) STORED,
        scene TEXT NOT NULL CHECK (scene IN ('group','c2c')),
        send_mode TEXT NOT NULL CHECK (send_mode IN ('passive','proactive')),
        metric TEXT NOT NULL CHECK (metric ~ '^[a-z0-9_]{1,80}$'),
        period_start_at BIGINT NOT NULL, period_end_at BIGINT,
        reset_verified BOOLEAN NOT NULL DEFAULT FALSE,
        limit_count BIGINT NOT NULL CHECK (limit_count > 0),
        used_count BIGINT NOT NULL DEFAULT 0 CHECK (used_count >= 0),
        reserved_count BIGINT NOT NULL DEFAULT 0 CHECK (reserved_count >= 0),
        blocked_until BIGINT,
        policy_revision BIGINT NOT NULL CHECK (policy_revision >= 1),
        release_policy TEXT NOT NULL DEFAULT 'not_dispatched_only'
            CHECK (release_policy IN ('not_dispatched_only','confirmed_unaccepted')),
        evidence_code TEXT NOT NULL CHECK (evidence_code ~ '^[a-z0-9_]{1,80}$'),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        UNIQUE(quota_key,account_key),
        UNIQUE(quota_key,account_key,quota_scope_key),
        FOREIGN KEY(endpoint_key,account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key,account_key),
        CHECK ((reset_verified AND period_end_at IS NOT NULL AND period_end_at>period_start_at)
            OR (NOT reset_verified AND period_end_at IS NULL))
    """),
)

DELIVERY_COLUMNS = (
    ("plan_id", "TEXT"), ("part_index", "INTEGER"),
    ("account_key", "TEXT"), ("endpoint_key", "TEXT"), ("requester_actor_key", "TEXT"),
    ("endpoint_generation", "BIGINT"), ("route_epoch", "BIGINT"), ("capability_revision", "BIGINT"),
    ("purpose", "TEXT"), ("payload_mode", "TEXT"), ("variant", "TEXT"),
    ("payload_digest", "TEXT"), ("digest_scheme", "TEXT"), ("digest_key_id", "TEXT"),
    ("priority", "INTEGER"), ("expires_at", "BIGINT"), ("wait_reason", "TEXT"),
    ("auth_retries_used", "INTEGER NOT NULL DEFAULT 0"),
    ("transient_retries_used", "INTEGER NOT NULL DEFAULT 0"),
    ("format_fallback_used", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("claim_epoch", "BIGINT"), ("current_attempt_id", "BIGINT"),
    ("official_owned", "BOOLEAN GENERATED ALWAYS AS (account_key IS NOT NULL) STORED"),
)

ATTEMPT_COLUMNS = (
    ("account_key", "TEXT"), ("endpoint_key", "TEXT"), ("fencing_epoch", "BIGINT"),
    ("endpoint_generation", "BIGINT"), ("route_epoch", "BIGINT"), ("capability_revision", "BIGINT"),
    ("bot_actor_key", "TEXT"), ("bot_identity_source", "TEXT"), ("bot_identity_revision", "BIGINT"),
    ("send_mode", "TEXT"), ("window_id", "TEXT"), ("msg_seq", "INTEGER"),
    ("window_charge_state", "TEXT"), ("payload_mode", "TEXT"), ("payload_digest", "TEXT"),
    ("digest_scheme", "TEXT"), ("digest_key_id", "TEXT"), ("wire_payload_json", "TEXT"),
    ("send_by_at", "BIGINT"), ("dispatched_at", "BIGINT"),
    ("failure_category", "TEXT"), ("failure_stage", "TEXT"), ("http_status", "INTEGER"),
    ("platform_code", "BIGINT"), ("request_id", "TEXT"), ("acceptance", "TEXT"),
    ("official_owned", "BOOLEAN GENERATED ALWAYS AS (account_key IS NOT NULL) STORED"),
)

DELIVERY_CONSTRAINTS = (
    ("uq_deliveries_plan_part", "UNIQUE(plan_id,part_index)"),
    ("uq_deliveries_owner", "UNIQUE(delivery_id,official_owned)"),
    ("uq_deliveries_account_endpoint", "UNIQUE(delivery_id,account_key,endpoint_key)"),
    ("uq_deliveries_payload_mode", "UNIQUE(delivery_id,payload_mode)"),
    ("fk_deliveries_official_plan", "FOREIGN KEY(plan_id,account_key,endpoint_key) REFERENCES @S@.delivery_plans(plan_id,account_key,endpoint_key)"),
    ("fk_deliveries_plan_payload", "FOREIGN KEY(plan_id,payload_mode,purpose) REFERENCES @S@.delivery_plans(plan_id,payload_mode,purpose)"),
    ("fk_deliveries_plan_requester", "FOREIGN KEY(plan_id,requester_actor_key) REFERENCES @S@.delivery_plans(plan_id,requester_actor_key)"),
    ("fk_deliveries_official_endpoint", "FOREIGN KEY(endpoint_key,account_key) REFERENCES @S@.qq_official_endpoints(endpoint_key,account_key)"),
    ("ck_deliveries_official_ownership", """CHECK (
        (target_platform<>'qq-official' AND account_key IS NULL AND endpoint_key IS NULL
            AND plan_id IS NULL AND part_index IS NULL AND requester_actor_key IS NULL
            AND endpoint_generation IS NULL AND route_epoch IS NULL AND capability_revision IS NULL
            AND purpose IS NULL AND payload_mode IS NULL AND variant IS NULL AND payload_digest IS NULL
            AND digest_scheme IS NULL AND digest_key_id IS NULL AND priority IS NULL AND expires_at IS NULL
            AND wait_reason IS NULL AND claim_epoch IS NULL AND current_attempt_id IS NULL
            AND auth_retries_used=0 AND transient_retries_used=0 AND NOT format_fallback_used
            AND status<>'accepted')
        OR (target_platform='qq-official' AND account_key IS NOT NULL AND endpoint_key IS NOT NULL
            AND plan_id IS NOT NULL AND part_index IS NOT NULL AND requester_actor_key IS NOT NULL
            AND endpoint_generation IS NOT NULL AND route_epoch IS NOT NULL AND capability_revision IS NOT NULL
            AND purpose IS NOT NULL AND payload_mode IS NOT NULL AND variant IS NOT NULL AND payload_digest IS NOT NULL
            AND digest_scheme IS NOT NULL AND priority IS NOT NULL AND expires_at IS NOT NULL))"""),
    ("ck_deliveries_official_fields", """CHECK (account_key IS NULL OR (
        octet_length(requester_actor_key) BETWEEN 1 AND 1280
        AND part_index BETWEEN 0 AND 15 AND endpoint_generation>=1 AND route_epoch>=1 AND capability_revision>=1
        AND purpose IN ('final','requested_artifact','background_result','progress','unsolicited','binding_otp')
        AND payload_mode IN ('persistent','ephemeral') AND variant IN ('primary','plain_fallback')
        AND payload_digest ~ '^[a-f0-9]{64}$' AND digest_scheme IN ('sha256','hmac_sha256_v1')
        AND ((digest_scheme='sha256' AND digest_key_id IS NULL)
            OR (digest_scheme='hmac_sha256_v1' AND digest_key_id ~ '^[A-Za-z0-9_-]{1,64}$'))
        AND expires_at>created_at AND auth_retries_used BETWEEN 0 AND 1 AND transient_retries_used>=0
        AND (wait_reason IS NULL OR wait_reason ~ '^[a-z0-9_]{1,80}$')
        AND (claim_epoch IS NULL OR claim_epoch>0)
        AND ((claim_epoch IS NULL)=(current_attempt_id IS NULL))
        AND (status NOT IN ('sending','accepted','committed','ambiguous') OR current_attempt_id IS NOT NULL)
        AND (status<>'committed' OR octet_length(native_message_id) BETWEEN 1 AND 256)
        AND ((variant='plain_fallback')=format_fallback_used)))"""),
    ("ck_deliveries_ephemeral", """CHECK (account_key IS NULL OR (
        (payload_mode='persistent' AND purpose<>'binding_otp' AND digest_scheme='sha256')
        OR (payload_mode='ephemeral' AND purpose='binding_otp' AND part_index=0
            AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL
            AND body_json='{"v":1,"nodes":[]}' AND content_fingerprint=''
            AND body_archive_path IS NULL AND body_archive_sha256 IS NULL
            AND body_size_bytes IS NULL AND body_archived_at IS NULL
            AND auth_retries_used=0 AND transient_retries_used=0 AND NOT format_fallback_used
            AND last_error='')))
    """),
)

ATTEMPT_CONSTRAINTS = (
    ("uq_delivery_attempts_account", "UNIQUE(attempt_id,account_key)"),
    ("uq_delivery_attempts_target", "UNIQUE(attempt_id,account_key,endpoint_key)"),
    ("uq_delivery_attempts_part", "UNIQUE(attempt_id,delivery_id,account_key,endpoint_key)"),
    ("fk_delivery_attempts_owner", "FOREIGN KEY(delivery_id,official_owned) REFERENCES @S@.deliveries(delivery_id,official_owned)"),
    ("fk_delivery_attempts_target", "FOREIGN KEY(delivery_id,account_key,endpoint_key) REFERENCES @S@.deliveries(delivery_id,account_key,endpoint_key)"),
    ("fk_delivery_attempts_payload", "FOREIGN KEY(delivery_id,payload_mode) REFERENCES @S@.deliveries(delivery_id,payload_mode)"),
    ("fk_delivery_attempts_window", "FOREIGN KEY(window_id,account_key,endpoint_key) REFERENCES @S@.qq_official_reply_windows(window_id,account_key,endpoint_key)"),
    ("ck_delivery_attempts_official_ownership", """CHECK (
        (account_key IS NULL AND endpoint_key IS NULL AND fencing_epoch IS NULL
            AND endpoint_generation IS NULL AND route_epoch IS NULL AND capability_revision IS NULL
            AND bot_actor_key IS NULL AND bot_identity_source IS NULL AND bot_identity_revision IS NULL
            AND send_mode IS NULL AND window_id IS NULL AND msg_seq IS NULL AND window_charge_state IS NULL
            AND payload_mode IS NULL AND payload_digest IS NULL AND digest_scheme IS NULL AND digest_key_id IS NULL
            AND wire_payload_json IS NULL AND send_by_at IS NULL AND dispatched_at IS NULL
            AND failure_category IS NULL AND failure_stage IS NULL AND http_status IS NULL
            AND platform_code IS NULL AND request_id IS NULL AND acceptance IS NULL)
        OR (account_key IS NOT NULL AND endpoint_key IS NOT NULL AND fencing_epoch IS NOT NULL
            AND endpoint_generation IS NOT NULL AND route_epoch IS NOT NULL AND capability_revision IS NOT NULL
            AND send_mode IS NOT NULL AND window_charge_state IS NOT NULL AND payload_mode IS NOT NULL
            AND payload_digest IS NOT NULL AND digest_scheme IS NOT NULL AND send_by_at IS NOT NULL
            AND acceptance IS NOT NULL))"""),
    ("ck_delivery_attempts_official_fields", """CHECK (account_key IS NULL OR (
        attempt>=1 AND fencing_epoch>=1 AND endpoint_generation>=1 AND route_epoch>=1 AND capability_revision>=1
        AND state IN ('reserved','dispatched','accepted','committed','confirmed_unaccepted','cancelled_unsent','ambiguous')
        AND acceptance IN ('unknown','confirmed_no','confirmed_yes')
        AND ((state IN ('accepted','committed'))=(acceptance='confirmed_yes'))
        AND ((state IN ('confirmed_unaccepted','cancelled_unsent'))=(acceptance='confirmed_no'))
        AND send_mode IN ('passive','proactive')
        AND ((send_mode='passive' AND window_id IS NOT NULL AND msg_seq IS NOT NULL
                AND msg_seq BETWEEN 1 AND 65535 AND window_charge_state IN ('reserved','spent','released'))
            OR (send_mode='proactive' AND window_id IS NULL AND msg_seq IS NULL AND window_charge_state='none'))
        AND payload_mode IN ('persistent','ephemeral') AND payload_digest ~ '^[a-f0-9]{64}$'
        AND ((payload_mode='persistent' AND digest_scheme='sha256' AND digest_key_id IS NULL
                AND wire_payload_json IS NOT NULL AND octet_length(wire_payload_json) BETWEEN 1 AND 4194304)
            OR (payload_mode='ephemeral' AND attempt=1 AND digest_scheme='hmac_sha256_v1' AND wire_payload_json IS NULL
                AND digest_key_id IS NOT NULL AND digest_key_id ~ '^[A-Za-z0-9_-]{1,64}$' AND detail=''))
        AND send_by_at>=started_at AND (dispatched_at IS NULL OR dispatched_at>=started_at)
        AND (failure_category IS NULL OR failure_category ~ '^[a-z0-9_]{1,80}$')
        AND (failure_stage IS NULL OR failure_stage ~ '^[a-z0-9_]{1,80}$')
        AND (http_status IS NULL OR http_status BETWEEN 100 AND 599)
        AND (request_id IS NULL OR request_id ~ '^[A-Za-z0-9_-]{1,128}$')
        AND ((bot_actor_key IS NULL AND bot_identity_source IS NULL AND bot_identity_revision IS NULL)
            OR (bot_actor_key IS NOT NULL AND octet_length(bot_actor_key) BETWEEN 1 AND 1280
                AND bot_identity_source IS NOT NULL AND bot_identity_source ~ '^[a-z0-9_]{1,80}$'
                AND bot_identity_revision IS NOT NULL AND bot_identity_revision>=1))))"""),
)

EVIDENCE_TABLES = (
    ("delivery_receipts", """
        receipt_id TEXT PRIMARY KEY CHECK (length(receipt_id) BETWEEN 1 AND 64),
        attempt_id BIGINT NOT NULL, account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('response','verified_echo')),
        native_message_id TEXT NOT NULL CHECK (octet_length(native_message_id) BETWEEN 1 AND 256),
        ref_idx TEXT CHECK (octet_length(ref_idx) BETWEEN 1 AND 256),
        receipt_digest TEXT NOT NULL CHECK (receipt_digest ~ '^[a-f0-9]{64}$'),
        http_status INTEGER CHECK (http_status BETWEEN 100 AND 599), platform_code BIGINT,
        request_id TEXT CHECK (request_id ~ '^[A-Za-z0-9_-]{1,128}$'),
        accepted_at BIGINT, observed_at BIGINT NOT NULL, applied_at BIGINT,
        UNIQUE(attempt_id,kind,native_message_id),
        FOREIGN KEY(attempt_id,account_key,endpoint_key) REFERENCES @S@.delivery_attempts(attempt_id,account_key,endpoint_key),
        CHECK (applied_at IS NULL OR applied_at>=observed_at)
    """),
    ("delivery_attempt_quotas", """
        attempt_id BIGINT NOT NULL, quota_key TEXT NOT NULL, account_key TEXT NOT NULL,
        endpoint_key TEXT NOT NULL, quota_endpoint_key TEXT,
        quota_scope_key TEXT GENERATED ALWAYS AS (COALESCE(quota_endpoint_key,'')) STORED,
        units BIGINT NOT NULL CHECK (units>=1),
        charge_state TEXT NOT NULL CHECK (charge_state IN ('reserved','spent','released')),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        PRIMARY KEY(attempt_id,quota_key),
        FOREIGN KEY(attempt_id,account_key,endpoint_key) REFERENCES @S@.delivery_attempts(attempt_id,account_key,endpoint_key),
        FOREIGN KEY(quota_key,account_key,quota_scope_key) REFERENCES @S@.qq_official_quota_periods(quota_key,account_key,quota_scope_key),
        CHECK (quota_endpoint_key IS NULL OR quota_endpoint_key=endpoint_key)
    """),
)

INDEXES = (
    ("ix_official_delivery_due", "deliveries", "account_key,status,next_attempt_at,priority,delivery_id", "account_key IS NOT NULL"),
    ("ix_official_delivery_plan", "deliveries", "plan_id,part_index,status", "account_key IS NOT NULL"),
    ("ix_official_attempt_recovery", "delivery_attempts", "account_key,state,delivery_id,attempt_id", "account_key IS NOT NULL"),
    ("ix_official_receipt_unapplied", "delivery_receipts", "account_key,observed_at,receipt_id", "applied_at IS NULL"),
    ("ix_official_quota_period", "qq_official_quota_periods", "account_key,scene,send_mode,period_start_at", None),
    ("ix_official_quota_charges", "delivery_attempt_quotas", "quota_key,charge_state", None),
)


def _execute(statement: str, quoted: str) -> None:
    # SQLAlchemy text() recognizes :1/:true inside JSON string literals as
    # parameters too. Escape literal colons before its bind-name parser.
    op.execute(sa.text(statement.replace("@S@", quoted).replace(":", r"\:")))


def upgrade() -> None:
    schema = _schema()
    quoted = '"' + schema + '"'
    # Split context FKs avoid one over-wide account+endpoint+actor btree entry.
    _execute('''ALTER TABLE @S@.qq_official_binding_challenges ADD CONSTRAINT uq_official_challenge_endpoint_expiry
        UNIQUE(challenge_id,issuer_endpoint_key,expires_at)''', quoted)
    _execute('''ALTER TABLE @S@.qq_official_binding_challenges ADD CONSTRAINT uq_official_challenge_actor
        UNIQUE(challenge_id,issuer_actor_key)''', quoted)
    for name, body in TABLES:
        _execute(f'CREATE TABLE @S@."{name}" ({body})', quoted)
    for table, columns in (("deliveries", DELIVERY_COLUMNS), ("delivery_attempts", ATTEMPT_COLUMNS)):
        for name, definition in columns:
            _execute(f'ALTER TABLE @S@."{table}" ADD COLUMN "{name}" {definition}', quoted)
    op.drop_constraint("ck_deliveries_status", "deliveries", schema=schema, type_="check")
    _execute(f'ALTER TABLE @S@.deliveries ADD CONSTRAINT ck_deliveries_status CHECK (status IN ({OFFICIAL_STATUSES}))', quoted)
    _execute('ALTER TABLE @S@.qq_official_reply_windows ADD CONSTRAINT uq_official_window_target UNIQUE(window_id,account_key,endpoint_key)', quoted)
    _execute('ALTER TABLE @S@.qq_official_reply_windows ADD CONSTRAINT ck_official_window_seq_ceiling CHECK (next_seq BETWEEN 1 AND 65536)', quoted)
    for table, constraints in (("deliveries", DELIVERY_CONSTRAINTS), ("delivery_attempts", ATTEMPT_CONSTRAINTS)):
        for name, clause in constraints:
            _execute(f'ALTER TABLE @S@."{table}" ADD CONSTRAINT "{name}" {clause}', quoted)
    _execute('''ALTER TABLE @S@.deliveries ADD CONSTRAINT fk_deliveries_current_attempt
        FOREIGN KEY(current_attempt_id,delivery_id,account_key,endpoint_key)
        REFERENCES @S@.delivery_attempts(attempt_id,delivery_id,account_key,endpoint_key)''', quoted)
    _execute('''CREATE UNIQUE INDEX uq_official_window_seq ON @S@.delivery_attempts(window_id,msg_seq)
        WHERE window_id IS NOT NULL''', quoted)
    _execute('''CREATE UNIQUE INDEX uq_official_attempt_unresolved ON @S@.delivery_attempts(delivery_id)
        WHERE account_key IS NOT NULL AND state IN ('reserved','dispatched','accepted','ambiguous')''', quoted)
    for name, body in EVIDENCE_TABLES:
        _execute(f'CREATE TABLE @S@."{name}" ({body})', quoted)
    for name, table, columns, where in INDEXES:
        suffix = f" WHERE {where}" if where else ""
        _execute(f'CREATE INDEX "{name}" ON @S@."{table}"({columns}){suffix}', quoted)
    _execute('ALTER TABLE @S@.qq_official_accounts ADD COLUMN bot_identity_revision BIGINT NOT NULL DEFAULT 0 CHECK (bot_identity_revision>=0)', quoted)
    _execute('ALTER TABLE @S@.qq_official_accounts ADD COLUMN bot_identity_contract TEXT CHECK (octet_length(bot_identity_contract) BETWEEN 1 AND 256)', quoted)
    _execute('UPDATE @S@.qq_official_accounts SET bot_identity_revision=1 WHERE bot_native_id IS NOT NULL', quoted)
    _execute('ALTER TABLE @S@.qq_official_endpoints ADD COLUMN proactive_probe_attempt_id BIGINT', quoted)
    _execute('ALTER TABLE @S@.qq_official_endpoints ADD COLUMN proactive_probe_at BIGINT', quoted)
    _execute('ALTER TABLE @S@.qq_official_endpoints ADD COLUMN passive_blocked_until BIGINT NOT NULL DEFAULT 0 CHECK (passive_blocked_until>=0)', quoted)
    _execute('ALTER TABLE @S@.qq_official_endpoints ADD COLUMN proactive_blocked_until BIGINT NOT NULL DEFAULT 0 CHECK (proactive_blocked_until>=0)', quoted)
    _execute('''ALTER TABLE @S@.qq_official_endpoints ADD CONSTRAINT fk_official_endpoint_probe
        FOREIGN KEY(proactive_probe_attempt_id,account_key,endpoint_key)
        REFERENCES @S@.delivery_attempts(attempt_id,account_key,endpoint_key)''', quoted)
    _execute('''ALTER TABLE @S@.qq_official_endpoints ADD CONSTRAINT ck_official_endpoint_probe
        CHECK ((proactive_probe_attempt_id IS NULL)=(proactive_probe_at IS NULL))''', quoted)


def downgrade() -> None:
    schema = _schema()
    quoted = '"' + schema + '"'
    connection = op.get_bind()
    # Downgrade cannot erase outbound evidence or an unknown-proactive probe.
    for name in ("delivery_plans", "delivery_receipts", "qq_official_quota_periods", "delivery_attempt_quotas"):
        if connection.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {quoted}."{name}")')).scalar():
            raise RuntimeError("Export official delivery evidence before downgrade")
    for table, predicate in (
        ("deliveries", "account_key IS NOT NULL OR target_platform='qq-official' OR status='accepted'"),
        ("delivery_attempts", "account_key IS NOT NULL"),
        ("qq_official_endpoints", "proactive_probe_attempt_id IS NOT NULL OR passive_blocked_until>0 OR proactive_blocked_until>0"),
        ("qq_official_accounts", "bot_identity_contract IS NOT NULL OR bot_identity_revision>1"),
    ):
        if connection.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {quoted}."{table}" WHERE {predicate})')).scalar():
            raise RuntimeError("Export official delivery evidence before downgrade")
    op.drop_constraint("fk_official_endpoint_probe", "qq_official_endpoints", schema=schema, type_="foreignkey")
    op.drop_constraint("ck_official_endpoint_probe", "qq_official_endpoints", schema=schema, type_="check")
    for name in ("proactive_blocked_until", "passive_blocked_until", "proactive_probe_at", "proactive_probe_attempt_id"):
        op.drop_column("qq_official_endpoints", name, schema=schema)
    for name in ("bot_identity_contract", "bot_identity_revision"):
        op.drop_column("qq_official_accounts", name, schema=schema)
    for name, _ in reversed(EVIDENCE_TABLES):
        op.drop_table(name, schema=schema)
    op.drop_constraint("fk_deliveries_current_attempt", "deliveries", schema=schema, type_="foreignkey")
    op.drop_index("uq_official_window_seq", table_name="delivery_attempts", schema=schema)
    op.drop_index("uq_official_attempt_unresolved", table_name="delivery_attempts", schema=schema)
    for table, constraints in (("delivery_attempts", ATTEMPT_CONSTRAINTS), ("deliveries", DELIVERY_CONSTRAINTS)):
        for name, _ in reversed(constraints):
            op.execute(sa.text(f'ALTER TABLE {quoted}."{table}" DROP CONSTRAINT "{name}"'))
    for name, table, _, _ in reversed(INDEXES):
        # Evidence tables already dropped their indexes with them.
        if table in {"deliveries", "delivery_attempts", "qq_official_quota_periods"}:
            op.drop_index(name, table_name=table, schema=schema)
    for table, columns in (("delivery_attempts", ATTEMPT_COLUMNS), ("deliveries", DELIVERY_COLUMNS)):
        for name, _ in reversed(columns):
            op.drop_column(table, name, schema=schema)
    op.drop_constraint("ck_official_window_seq_ceiling", "qq_official_reply_windows", schema=schema, type_="check")
    op.drop_constraint("uq_official_window_target", "qq_official_reply_windows", schema=schema, type_="unique")
    op.drop_constraint("ck_deliveries_status", "deliveries", schema=schema, type_="check")
    _execute(f'ALTER TABLE @S@.deliveries ADD CONSTRAINT ck_deliveries_status CHECK (status IN ({LEGACY_STATUSES}))', quoted)
    for name, _ in reversed(TABLES):
        op.drop_table(name, schema=schema)
    op.drop_constraint("uq_official_challenge_actor", "qq_official_binding_challenges", schema=schema, type_="unique")
    op.drop_constraint("uq_official_challenge_endpoint_expiry", "qq_official_binding_challenges", schema=schema, type_="unique")
