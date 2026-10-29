"""Local console identity, administrator challenges and native upload sessions.

Preserve all receipts and console identities on downgrade, including terminal
or unmatched identity-control messages. No parallel queue is introduced.
"""
from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0068_qq_official_operations"
down_revision = "0067_qq_official_capabilities"
branch_labels = None
depends_on = None

UPGRADE = (
    """ALTER TABLE admin_accounts DROP CONSTRAINT ck_admin_identity_transport""",
    """ALTER TABLE admin_accounts ADD CONSTRAINT ck_admin_identity_transport CHECK (
        (identity_mode='legacy_qq' AND (role<>'admin' OR qq_id IS NOT NULL))
        OR (identity_mode IN ('official_c2c','local_admin') AND role='admin' AND qq_id IS NULL))""",
    """ALTER TABLE qq_official_inbound_events ADD COLUMN identity_control_actor_key text, ADD COLUMN identity_control_occurred_at bigint, ADD COLUMN identity_control_commitment text""",
    """ALTER TABLE qq_official_inbound_events ADD CONSTRAINT ck_inbox_identity_control CHECK (
 (identity_control_actor_key IS NULL AND identity_control_occurred_at IS NULL AND identity_control_commitment IS NULL) OR
 (identity_control_actor_key IS NOT NULL AND octet_length(identity_control_actor_key) BETWEEN 1 AND 1280
  AND identity_control_occurred_at IS NOT NULL AND identity_control_occurred_at>=0
  AND identity_control_commitment IS NOT NULL AND identity_control_commitment ~ '^[a-f0-9]{64}$'
  AND payload_json IS NULL AND ingest_job_id IS NULL))""",
    """ALTER TABLE qq_official_inbound_events ADD CONSTRAINT uq_inbox_identity_actor UNIQUE(inbox_id,endpoint_key,identity_control_actor_key)""",
    """CREATE TABLE qq_official_admin_identity_challenges (
 challenge_id text PRIMARY KEY CHECK(challenge_id ~ '^[a-f0-9]{32}$'),
 account_key text NOT NULL REFERENCES qq_official_accounts(account_key),
 admin_account_id text NOT NULL REFERENCES admin_accounts(account_id),admin_version bigint NOT NULL CHECK(admin_version>0),
 session_hash text NOT NULL CHECK(session_hash ~ '^[a-f0-9]{64}$'),
 code_digest text NOT NULL CHECK(code_digest ~ '^[a-f0-9]{64}$'),
 state text NOT NULL CHECK(state IN ('pending','observed','consumed','cancelled','expired')),
 created_at bigint NOT NULL,expires_at bigint NOT NULL CHECK(expires_at>created_at AND expires_at<=created_at+180),
 source_inbox_id text UNIQUE,endpoint_key text,actor_key text,endpoint_generation bigint,route_epoch bigint,
 owner_grant_id text,owner_revision bigint,observed_at bigint,consumed_at bigint,binding_id text UNIQUE REFERENCES qq_official_admin_bindings(binding_id),
 FOREIGN KEY(source_inbox_id,account_key,endpoint_key) REFERENCES qq_official_inbound_events(inbox_id,account_key,endpoint_key),
 FOREIGN KEY(source_inbox_id,endpoint_key,actor_key) REFERENCES qq_official_inbound_events(inbox_id,endpoint_key,identity_control_actor_key),
 FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
 FOREIGN KEY(owner_grant_id,account_key) REFERENCES qq_official_owner_grants(grant_id,account_key),
 CHECK((source_inbox_id IS NULL AND endpoint_key IS NULL AND actor_key IS NULL AND endpoint_generation IS NULL AND route_epoch IS NULL
  AND owner_grant_id IS NULL AND owner_revision IS NULL AND observed_at IS NULL) OR
  (source_inbox_id IS NOT NULL AND endpoint_key IS NOT NULL AND actor_key IS NOT NULL AND endpoint_generation IS NOT NULL AND endpoint_generation>0 AND route_epoch IS NOT NULL AND route_epoch>0
  AND owner_grant_id IS NOT NULL AND owner_revision IS NOT NULL AND owner_revision>0 AND observed_at IS NOT NULL AND observed_at>=created_at AND observed_at<expires_at)),
 CHECK(state NOT IN ('observed','consumed') OR source_inbox_id IS NOT NULL),
 CHECK((consumed_at IS NULL)=(binding_id IS NULL)),
 CHECK((state='consumed')=(consumed_at IS NOT NULL AND binding_id IS NOT NULL)),
 CHECK(consumed_at IS NULL OR consumed_at>=observed_at AND consumed_at<expires_at)
)""",
    """CREATE INDEX ix_admin_identity_rate ON qq_official_admin_identity_challenges(account_key,admin_account_id,created_at)""",
    """CREATE FUNCTION qq_admin_identity_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF ROW(NEW.challenge_id,NEW.account_key,NEW.admin_account_id,NEW.admin_version,NEW.session_hash,NEW.code_digest,NEW.created_at,NEW.expires_at)
  IS DISTINCT FROM ROW(OLD.challenge_id,OLD.account_key,OLD.admin_account_id,OLD.admin_version,OLD.session_hash,OLD.code_digest,OLD.created_at,OLD.expires_at)
  THEN RAISE EXCEPTION 'identity_challenge_immutable'; END IF;
 IF OLD.source_inbox_id IS NOT NULL AND ROW(NEW.source_inbox_id,NEW.endpoint_key,NEW.actor_key,NEW.endpoint_generation,NEW.route_epoch,NEW.owner_grant_id,NEW.owner_revision,NEW.observed_at)
  IS DISTINCT FROM ROW(OLD.source_inbox_id,OLD.endpoint_key,OLD.actor_key,OLD.endpoint_generation,OLD.route_epoch,OLD.owner_grant_id,OLD.owner_revision,OLD.observed_at)
  THEN RAISE EXCEPTION 'identity_source_immutable'; END IF;
 IF OLD.state IN ('consumed','cancelled','expired') AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'identity_challenge_terminal'; END IF;
 IF (OLD.state='pending' AND NEW.state NOT IN ('pending','observed','cancelled','expired')) OR
    (OLD.state='observed' AND NEW.state NOT IN ('observed','consumed','cancelled','expired')) THEN RAISE EXCEPTION 'identity_challenge_transition'; END IF;
 RETURN NEW; END $$""",
    """CREATE TRIGGER tr_admin_identity_immutable BEFORE UPDATE ON qq_official_admin_identity_challenges FOR EACH ROW EXECUTE FUNCTION qq_admin_identity_immutable()""",
    """CREATE UNIQUE INDEX uq_qq_file_chunk_stage ON qq_official_file_stage_attempts
(artifact_id,operation,COALESCE(part_index,-1))
WHERE operation<>'upload_small' AND state IN ('dispatched','confirmed','rejected','unknown')""",
    """ALTER TABLE qq_official_file_stage_attempts ADD CONSTRAINT uq_qq_file_stage_receipt
UNIQUE(stage_attempt_id,artifact_id,receipt_commitment,operation)""",
    """CREATE TABLE qq_official_file_upload_sessions (
 artifact_id TEXT PRIMARY KEY REFERENCES qq_official_file_artifacts(artifact_id),
 prepare_stage_attempt_id TEXT NOT NULL UNIQUE,
 prepare_operation TEXT NOT NULL DEFAULT 'upload_prepare' CHECK(prepare_operation='upload_prepare'),
 credential_ref TEXT NOT NULL UNIQUE CHECK(credential_ref ~ '^[a-f0-9]{32}$'),
 binding_digest TEXT NOT NULL CHECK(binding_digest ~ '^[a-f0-9]{64}$'),
 prepared_at BIGINT NOT NULL CHECK(prepared_at>=0),
 expires_at BIGINT NOT NULL CHECK(expires_at>prepared_at AND expires_at<=prepared_at+600),
 part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 128),
 contract TEXT NOT NULL CHECK(octet_length(contract) BETWEEN 1 AND 256),
 FOREIGN KEY(prepare_stage_attempt_id,artifact_id,binding_digest,prepare_operation)
  REFERENCES qq_official_file_stage_attempts(stage_attempt_id,artifact_id,receipt_commitment,operation)
)""",
    """CREATE FUNCTION qq_file_upload_session_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'file_upload_session_immutable'; END $$""",
    """CREATE TRIGGER trg_qq_file_upload_session_immutable BEFORE UPDATE OR DELETE
ON qq_official_file_upload_sessions FOR EACH ROW EXECUTE FUNCTION qq_file_upload_session_immutable()""",
)

DOWNGRADE = (
    """DROP TABLE qq_official_file_upload_sessions""",
    """DROP FUNCTION qq_file_upload_session_immutable()""",
    """ALTER TABLE qq_official_file_stage_attempts DROP CONSTRAINT uq_qq_file_stage_receipt""",
    """DROP INDEX uq_qq_file_chunk_stage""",
    """DROP TABLE qq_official_admin_identity_challenges""",
    """DROP FUNCTION qq_admin_identity_immutable()""",
    """ALTER TABLE qq_official_inbound_events DROP CONSTRAINT uq_inbox_identity_actor,DROP CONSTRAINT ck_inbox_identity_control,DROP COLUMN identity_control_actor_key,DROP COLUMN identity_control_occurred_at,DROP COLUMN identity_control_commitment""",
    """ALTER TABLE admin_accounts DROP CONSTRAINT ck_admin_identity_transport""",
    """ALTER TABLE admin_accounts ADD CONSTRAINT ck_admin_identity_transport CHECK (
        (identity_mode='legacy_qq' AND (role<>'admin' OR qq_id IS NOT NULL))
        OR (identity_mode='official_c2c' AND role='admin' AND qq_id IS NULL))""",
)

AFFECTED_TABLES = ('admin_accounts', 'qq_official_admin_identity_challenges', 'qq_official_inbound_events', 'qq_official_file_stage_attempts', 'qq_official_file_upload_sessions')
HAS_EVIDENCE = """SELECT EXISTS(SELECT 1 FROM admin_accounts WHERE identity_mode='local_admin')
    OR EXISTS(SELECT 1 FROM qq_official_admin_identity_challenges)
    OR EXISTS(SELECT 1 FROM qq_official_inbound_events WHERE identity_control_actor_key IS NOT NULL
        OR identity_control_occurred_at IS NOT NULL OR identity_control_commitment IS NOT NULL)
    OR EXISTS(SELECT 1 FROM qq_official_file_upload_sessions)
    OR EXISTS(SELECT 1 FROM qq_official_file_stage_attempts WHERE operation<>'upload_small')"""

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
        raise RuntimeError("official_operation_evidence_requires_export")
    for statement in DOWNGRADE:
        _execute(statement)
