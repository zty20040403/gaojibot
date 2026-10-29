"""Native schedules, proactive observations, wiki provenance and image artifacts.

Existing queues, identities and PDF/ZIP artifacts are preserved. Downgrade
refuses to discard any capability provenance, including terminal records.
"""
from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa

revision = "0067_qq_official_capabilities"
down_revision = "0066_qq_official_migration"
branch_labels = None
depends_on = None

UPGRADE = (
    """ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT qq_official_file_artifacts_mime_check
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT qq_official_file_artifacts_mime_check
       CHECK (mime IN ('application/pdf','application/zip','image/png','image/jpeg'))
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_qq_file_image_contract CHECK (
       mime NOT IN ('image/png','image/jpeg') OR
       (byte_size BETWEEN 1 AND 3500000 AND
         ((mime='image/png' AND right(lower(filename),4)='.png') OR
          (mime='image/jpeg' AND (right(lower(filename),4)='.jpg' OR right(lower(filename),5)='.jpeg')))))
    """,
    """CREATE TABLE qq_official_project_resource_origins (
        origin_id text PRIMARY KEY CHECK(origin_id ~ '^[0-9a-f]{64}$'),
        project_id bigint NOT NULL, project_scope_key text NOT NULL,
        original_bot_id text NOT NULL, source_account_key text NOT NULL,
        source_endpoint_key text NOT NULL, target_account_key text NOT NULL,
        target_endpoint_key text NOT NULL, endpoint_generation bigint NOT NULL CHECK(endpoint_generation>0),
        endpoint_route_epoch bigint NOT NULL CHECK(endpoint_route_epoch>0),
        logical_route_epoch bigint NOT NULL CHECK(logical_route_epoch>0),
        migration_id text NOT NULL REFERENCES migration_journals(migration_id),
        route_id text NOT NULL REFERENCES migration_routes(route_id),
        item_id text NOT NULL REFERENCES migration_items(item_id),
        preview_digest text NOT NULL CHECK(preview_digest ~ '^[0-9a-f]{64}$'),
        export_digest text NOT NULL CHECK(export_digest ~ '^[0-9a-f]{64}$'),
        staged_grant_id text NOT NULL, staged_grant_revision bigint NOT NULL,
        staged_snapshot_digest text NOT NULL CHECK(staged_snapshot_digest ~ '^[0-9a-f]{64}$'),
        created_at bigint NOT NULL CHECK(created_at>=0),
        UNIQUE(project_id,target_endpoint_key,logical_route_epoch),
        FOREIGN KEY(project_id,project_scope_key,original_bot_id)
            REFERENCES projects(project_id,scope_key,bot_native_user_id),
        FOREIGN KEY(target_endpoint_key,target_account_key)
            REFERENCES qq_official_endpoints(endpoint_key,account_key),
        FOREIGN KEY(staged_grant_id,staged_grant_revision)
            REFERENCES migration_project_grants(grant_id,grant_revision)
    )
    """,
    """CREATE FUNCTION qq_project_resource_origin_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'project resource origin immutable'; END $$
    """,
    """CREATE TRIGGER qq_project_resource_origin_immutable BEFORE UPDATE OR DELETE
        ON qq_official_project_resource_origins FOR EACH ROW EXECUTE FUNCTION qq_project_resource_origin_immutable()
    """,
    """CREATE TABLE qq_official_project_resource_grants (
        grant_id text NOT NULL REFERENCES qq_official_project_grants(grant_id),
        grant_revision bigint NOT NULL CHECK(grant_revision>0),
        origin_id text NOT NULL REFERENCES qq_official_project_resource_origins(origin_id),
        grant_digest text NOT NULL CHECK(grant_digest ~ '^[a-f0-9]{64}$'),
        created_at bigint NOT NULL CHECK(created_at>=0),
        PRIMARY KEY(grant_id,grant_revision)
    )
    """,
    """CREATE FUNCTION qq_project_resource_grant_validate() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM qq_official_project_grants g
            JOIN qq_official_project_resource_origins r ON r.origin_id=NEW.origin_id
            JOIN migration_routes route ON route.route_id=r.route_id
            WHERE g.grant_id=NEW.grant_id AND g.revision=NEW.grant_revision
            AND g.state='active' AND g.evidence_kind='owner_command'
            AND g.issued_at<=NEW.created_at AND NEW.created_at<g.expires_at
            AND g.project_id=r.project_id AND g.project_scope_key=r.project_scope_key
            AND g.account_key=r.target_account_key AND g.endpoint_key=r.target_endpoint_key
            AND g.endpoint_generation=r.endpoint_generation AND g.endpoint_route_epoch=r.endpoint_route_epoch
            AND g.route_epoch=r.logical_route_epoch AND route.epoch=r.logical_route_epoch
            AND route.phase='completed' AND route.active_endpoint_key=r.target_endpoint_key)
        THEN RAISE EXCEPTION 'project resource grant binding mismatch'; END IF;
        RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER qq_project_resource_grant_validate BEFORE INSERT ON qq_official_project_resource_grants
        FOR EACH ROW EXECUTE FUNCTION qq_project_resource_grant_validate()
    """,
    """CREATE TRIGGER qq_project_resource_grant_immutable BEFORE UPDATE OR DELETE ON qq_official_project_resource_grants
        FOR EACH ROW EXECUTE FUNCTION qq_project_resource_origin_immutable()
    """,
    """CREATE TABLE qq_official_wiki_configurations (
 scope_key TEXT NOT NULL REFERENCES group_wiki_settings(scope_key),
 config_version INTEGER NOT NULL CHECK(config_version>0),
 account_key TEXT NOT NULL,
 endpoint_key TEXT NOT NULL,
 actor_key TEXT NOT NULL,
 source_execution_id TEXT NOT NULL REFERENCES qq_official_turn_executions(execution_id),
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>0),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>0),
 logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=0),
 owner_grant_id TEXT NOT NULL,
 owner_revision BIGINT NOT NULL CHECK(owner_revision>0),
 owner_link_revision BIGINT NOT NULL CHECK(owner_link_revision>0),
 created_at BIGINT NOT NULL,
 PRIMARY KEY(scope_key,config_version),
 UNIQUE(endpoint_key,config_version),
 FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
 FOREIGN KEY(owner_grant_id,account_key) REFERENCES qq_official_owner_grants(grant_id,account_key)
)
    """,
    """CREATE FUNCTION qq_wiki_source_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN RAISE EXCEPTION 'official wiki source is immutable'; END $$
    """,
    """CREATE TRIGGER qq_wiki_source_immutable BEFORE UPDATE OR DELETE ON qq_official_wiki_configurations
 FOR EACH ROW EXECUTE FUNCTION qq_wiki_source_immutable()
    """,
    """CREATE TABLE qq_official_native_schedule_sources (
 source_id TEXT PRIMARY KEY CHECK(source_id ~ '^[0-9a-f]{32}$'),
 execution_id TEXT NOT NULL REFERENCES qq_official_turn_executions(execution_id),
 request_key TEXT NOT NULL CHECK(request_key ~ '^[0-9a-f]{64}$'),
 account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
 scope_key TEXT NOT NULL, inbox_id TEXT NOT NULL REFERENCES qq_official_inbound_events(inbox_id),
 endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>0),
 endpoint_route_epoch BIGINT NOT NULL CHECK(endpoint_route_epoch>0),
 logical_route_epoch BIGINT NOT NULL CHECK(logical_route_epoch>=0),
 kind TEXT NOT NULL CHECK(kind IN ('reminder','routine')),
 reminder_id BIGINT REFERENCES reminders(reminder_id), routine_id BIGINT REFERENCES agent_routines(routine_id),
 revision BIGINT NOT NULL CHECK(revision>0), first_due_at BIGINT NOT NULL, spec_json TEXT NOT NULL, spec_digest TEXT NOT NULL CHECK(length(spec_digest)=64),
 policy_id TEXT NOT NULL, policy_revision BIGINT NOT NULL CHECK(policy_revision>0),
 authorization_until BIGINT NOT NULL, execution_seconds BIGINT NOT NULL CHECK(execution_seconds>0),
 delivery_seconds BIGINT NOT NULL CHECK(delivery_seconds>=execution_seconds),
 state TEXT NOT NULL CHECK(state IN ('active','cancelled')),
 created_at BIGINT NOT NULL, UNIQUE(execution_id,request_key), UNIQUE(source_id,account_key,endpoint_key),
 UNIQUE(routine_id,revision), UNIQUE(reminder_id),
 CHECK(authorization_until>created_at),
 CHECK((kind='reminder' AND reminder_id IS NOT NULL AND routine_id IS NULL)
    OR (kind='routine' AND routine_id IS NOT NULL AND reminder_id IS NULL)),
 FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key)
)
    """,
    """CREATE TABLE qq_official_native_schedule_occurrences (
 occurrence_id TEXT PRIMARY KEY CHECK(occurrence_id ~ '^[0-9a-f]{32}$'),
 source_id TEXT NOT NULL, account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL,
 scheduled_at BIGINT NOT NULL, execution_deadline BIGINT NOT NULL, result_deadline BIGINT NOT NULL,
 run_id TEXT UNIQUE REFERENCES routine_runs(run_id), job_id BIGINT UNIQUE NOT NULL REFERENCES durable_jobs(job_id),
 plan_id TEXT UNIQUE REFERENCES delivery_plans(plan_id),
 state TEXT NOT NULL CHECK(state IN ('ready','executing','result_ready','handed_off','needs_attention','cancelled')),
 result_json TEXT, result_digest TEXT, attempt_id TEXT, worker_owner TEXT, worker_generation BIGINT, account_epoch BIGINT,
 created_at BIGINT NOT NULL, UNIQUE(source_id,scheduled_at),
 CHECK(scheduled_at<execution_deadline AND execution_deadline<=result_deadline),
 CHECK((result_json IS NULL)=(result_digest IS NULL)),
 CHECK((attempt_id IS NULL)=(worker_owner IS NULL)),
 CHECK((attempt_id IS NULL)=(worker_generation IS NULL)),
 CHECK((attempt_id IS NULL)=(account_epoch IS NULL)),
 CHECK(state NOT IN ('result_ready','handed_off') OR result_json IS NOT NULL),
 CHECK(state<>'handed_off' OR plan_id IS NOT NULL),
 FOREIGN KEY(source_id,account_key,endpoint_key) REFERENCES qq_official_native_schedule_sources(source_id,account_key,endpoint_key)
)
    """,
    """CREATE TABLE qq_official_proactive_observations (
 inbox_id TEXT PRIMARY KEY REFERENCES qq_official_inbound_events(inbox_id),
 account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
 canonical_message_id BIGINT NOT NULL REFERENCES messages(canonical_message_id),
 source_native_message_id TEXT NOT NULL, source_received_at BIGINT NOT NULL,
 deadline_at BIGINT NOT NULL, ingest_job_id BIGINT NOT NULL REFERENCES durable_jobs(job_id),
 worker_owner TEXT NOT NULL, worker_generation BIGINT NOT NULL, account_epoch BIGINT NOT NULL,
 endpoint_generation BIGINT NOT NULL, endpoint_route_epoch BIGINT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('scoring','discarded','queued')),
 plan_id TEXT UNIQUE REFERENCES delivery_plans(plan_id),
 created_at BIGINT NOT NULL, CHECK(deadline_at=source_received_at+30),
 CHECK((state='queued')=(plan_id IS NOT NULL)),
 FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key)
)
    """,
    """ALTER TABLE qq_official_native_schedule_sources ADD FOREIGN KEY(execution_id,endpoint_key,actor_key)
 REFERENCES qq_official_turn_executions(execution_id,endpoint_key,requester_actor_key)
    """,
    """ALTER TABLE qq_official_native_schedule_sources ADD FOREIGN KEY(reminder_id,account_key,endpoint_key,actor_key)
 REFERENCES reminders(reminder_id,account_key,endpoint_key,creator_actor_key)
    """,
    """ALTER TABLE qq_official_native_schedule_sources ADD FOREIGN KEY(routine_id,account_key,endpoint_key,actor_key)
 REFERENCES agent_routines(routine_id,account_key,endpoint_key,creator_actor_key)
    """,
    """CREATE FUNCTION guard_native_schedule_source() RETURNS trigger LANGUAGE plpgsql AS $$
 DECLARE e qq_official_turn_executions%ROWTYPE;
 BEGIN
 IF TG_OP='UPDATE' THEN
   IF (to_jsonb(NEW)-'state') IS DISTINCT FROM (to_jsonb(OLD)-'state')
      OR OLD.state='cancelled' AND NEW.state<>'cancelled' THEN
     RAISE EXCEPTION 'immutable_native_schedule_source';
   END IF;
 ELSE
   SELECT * INTO e FROM qq_official_turn_executions WHERE execution_id=NEW.execution_id;
   IF NOT FOUND OR e.account_key IS DISTINCT FROM NEW.account_key OR e.endpoint_key IS DISTINCT FROM NEW.endpoint_key
      OR e.requester_actor_key IS DISTINCT FROM NEW.actor_key OR e.inbox_id IS DISTINCT FROM NEW.inbox_id
      OR e.endpoint_generation IS DISTINCT FROM NEW.endpoint_generation OR e.route_epoch IS DISTINCT FROM NEW.endpoint_route_epoch
      OR e.state<>'executing' OR e.deadline_at<=NEW.created_at THEN
     RAISE EXCEPTION 'native_schedule_source_mismatch';
   END IF;
 END IF;
 RETURN NEW;
 END $$
    """,
    """CREATE TRIGGER native_schedule_source_immutable BEFORE INSERT OR UPDATE ON qq_official_native_schedule_sources
 FOR EACH ROW EXECUTE FUNCTION guard_native_schedule_source()
    """,
    """CREATE FUNCTION guard_native_schedule_occurrence() RETURNS trigger LANGUAGE plpgsql AS $$
 DECLARE s qq_official_native_schedule_sources%ROWTYPE; r routine_runs%ROWTYPE; j durable_jobs%ROWTYPE; p delivery_plans%ROWTYPE;
 BEGIN
 IF TG_OP='UPDATE' THEN
   IF (to_jsonb(NEW)-ARRAY['state','result_json','result_digest','plan_id','attempt_id','worker_owner','worker_generation','account_epoch'])
       IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','result_json','result_digest','plan_id','attempt_id','worker_owner','worker_generation','account_epoch'])
      OR OLD.plan_id IS NOT NULL AND NEW.plan_id IS DISTINCT FROM OLD.plan_id
      OR OLD.result_json IS NOT NULL AND (NEW.result_json IS DISTINCT FROM OLD.result_json OR NEW.result_digest IS DISTINCT FROM OLD.result_digest)
      OR OLD.attempt_id IS NOT NULL AND (NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
        OR NEW.worker_owner IS DISTINCT FROM OLD.worker_owner OR NEW.worker_generation IS DISTINCT FROM OLD.worker_generation
        OR NEW.account_epoch IS DISTINCT FROM OLD.account_epoch)
      OR OLD.state IN ('handed_off','needs_attention','cancelled') AND NEW.state IS DISTINCT FROM OLD.state THEN
     RAISE EXCEPTION 'immutable_native_schedule_occurrence';
   END IF;
   IF NEW.plan_id IS NOT NULL AND OLD.plan_id IS NULL THEN
     SELECT * INTO s FROM qq_official_native_schedule_sources WHERE source_id=NEW.source_id;
     SELECT * INTO p FROM delivery_plans WHERE plan_id=NEW.plan_id;
     IF p.plan_id IS NULL OR p.account_key IS DISTINCT FROM s.account_key OR p.endpoint_key IS DISTINCT FROM s.endpoint_key
        OR p.requester_actor_key IS DISTINCT FROM s.actor_key OR p.purpose<>'background_result'
        OR p.expires_at IS DISTINCT FROM NEW.result_deadline OR p.endpoint_generation IS DISTINCT FROM s.endpoint_generation
        OR p.route_epoch IS DISTINCT FROM s.endpoint_route_epoch THEN
       RAISE EXCEPTION 'native_occurrence_plan_mismatch';
     END IF;
   END IF;
 ELSE
   SELECT * INTO s FROM qq_official_native_schedule_sources WHERE source_id=NEW.source_id;
   SELECT * INTO j FROM durable_jobs WHERE job_id=NEW.job_id;
   IF s.source_id IS NULL OR j.job_id IS NULL OR s.account_key IS DISTINCT FROM NEW.account_key OR s.endpoint_key IS DISTINCT FROM NEW.endpoint_key
      OR NEW.execution_deadline<>LEAST(NEW.scheduled_at+s.execution_seconds,s.authorization_until)
      OR NEW.result_deadline<>LEAST(NEW.scheduled_at+s.delivery_seconds,s.authorization_until)
      OR j.payload_json::jsonb<>jsonb_build_object('occurrence_id',NEW.occurrence_id)
      OR s.kind='reminder' AND NEW.run_id IS NOT NULL OR s.kind='routine' AND NEW.run_id IS NULL THEN
     RAISE EXCEPTION 'native_occurrence_source_mismatch';
   END IF;
   IF NEW.run_id IS NOT NULL THEN
     SELECT * INTO r FROM routine_runs WHERE run_id=NEW.run_id;
     IF r.run_id IS NULL OR r.routine_id<>s.routine_id OR r.version<>s.revision OR r.scheduled_at<>NEW.scheduled_at THEN
       RAISE EXCEPTION 'native_occurrence_domain_mismatch';
     END IF;
   END IF;
 END IF;
 RETURN NEW;
 END $$
    """,
    """CREATE TRIGGER native_schedule_occurrence_immutable BEFORE INSERT OR UPDATE ON qq_official_native_schedule_occurrences
 FOR EACH ROW EXECUTE FUNCTION guard_native_schedule_occurrence()
    """,
    """CREATE FUNCTION guard_proactive_observation() RETURNS trigger LANGUAGE plpgsql AS $$
 DECLARE i qq_official_inbound_events%ROWTYPE; m messages%ROWTYPE; p delivery_plans%ROWTYPE;
 BEGIN
 IF TG_OP='INSERT' THEN
   SELECT * INTO i FROM qq_official_inbound_events WHERE inbox_id=NEW.inbox_id;
   SELECT * INTO m FROM messages WHERE canonical_message_id=NEW.canonical_message_id;
   IF i.inbox_id IS NULL OR m.canonical_message_id IS NULL OR NEW.state<>'scoring'
      OR i.account_key IS DISTINCT FROM NEW.account_key OR i.endpoint_key IS DISTINCT FROM NEW.endpoint_key
      OR i.canonical_message_id IS DISTINCT FROM NEW.canonical_message_id OR i.native_message_id IS DISTINCT FROM NEW.source_native_message_id
      OR i.received_at IS DISTINCT FROM NEW.source_received_at OR i.ingest_job_id IS DISTINCT FROM NEW.ingest_job_id
      OR i.endpoint_generation IS DISTINCT FROM NEW.endpoint_generation OR i.route_epoch IS DISTINCT FROM NEW.endpoint_route_epoch
      OR i.status<>'processed' OR i.echo_state<>'not_echo' OR i.trigger_requested OR i.dispatch_job_id IS NOT NULL
      OR m.source_endpoint_key IS DISTINCT FROM NEW.endpoint_key OR m.sender_actor_key IS DISTINCT FROM NEW.actor_key OR m.direction<>'inbound' THEN
     RAISE EXCEPTION 'proactive_source_mismatch';
   END IF;
   RETURN NEW;
 END IF;
 IF (to_jsonb(NEW)-ARRAY['state','plan_id']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','plan_id'])
    OR OLD.plan_id IS NOT NULL AND NEW.plan_id IS DISTINCT FROM OLD.plan_id
    OR OLD.state IN ('discarded','queued') AND NEW.state IS DISTINCT FROM OLD.state THEN
   RAISE EXCEPTION 'immutable_proactive_source';
 END IF;
 IF NEW.plan_id IS NOT NULL AND OLD.plan_id IS NULL THEN
   SELECT * INTO p FROM delivery_plans WHERE plan_id=NEW.plan_id;
   IF p.plan_id IS NULL OR p.account_key IS DISTINCT FROM NEW.account_key OR p.endpoint_key IS DISTINCT FROM NEW.endpoint_key
      OR p.requester_actor_key IS DISTINCT FROM NEW.actor_key OR p.purpose<>'unsolicited'
      OR p.expires_at>NEW.deadline_at OR p.endpoint_generation IS DISTINCT FROM NEW.endpoint_generation
      OR p.route_epoch IS DISTINCT FROM NEW.endpoint_route_epoch OR p.source_canonical_message_id IS DISTINCT FROM NEW.canonical_message_id THEN
     RAISE EXCEPTION 'proactive_plan_mismatch';
   END IF;
 END IF;
 RETURN NEW;
 END $$
    """,
    """CREATE TRIGGER proactive_observation_immutable BEFORE INSERT OR UPDATE ON qq_official_proactive_observations
 FOR EACH ROW EXECUTE FUNCTION guard_proactive_observation()
    """,
    """CREATE INDEX qq_native_schedule_active ON qq_official_native_schedule_sources(account_key,routine_id,revision) WHERE state='active'
    """,
    """CREATE INDEX qq_proactive_window ON qq_official_proactive_observations(endpoint_key,created_at)
    """,
)

DOWNGRADE = (
    """DROP TABLE qq_official_proactive_observations
    """,
    """DROP TABLE qq_official_native_schedule_occurrences
    """,
    """DROP TABLE qq_official_native_schedule_sources
    """,
    """DROP FUNCTION guard_proactive_observation()
    """,
    """DROP FUNCTION guard_native_schedule_occurrence()
    """,
    """DROP FUNCTION guard_native_schedule_source()
    """,
    """DROP TABLE qq_official_project_resource_grants
    """,
    """DROP TABLE qq_official_project_resource_origins
    """,
    """DROP TABLE qq_official_wiki_configurations
    """,
    """DROP FUNCTION qq_project_resource_grant_validate()
    """,
    """DROP FUNCTION qq_project_resource_origin_immutable()
    """,
    """DROP FUNCTION qq_wiki_source_immutable()
    """,
    """ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT ck_qq_file_image_contract
    """,
    """ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT qq_official_file_artifacts_mime_check
    """,
    """ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT qq_official_file_artifacts_mime_check
       CHECK (mime IN ('application/pdf','application/zip'))
    """,
)

NEW_TABLES = ('qq_official_native_schedule_occurrences', 'qq_official_native_schedule_sources', 'qq_official_proactive_observations', 'qq_official_project_resource_grants', 'qq_official_project_resource_origins', 'qq_official_wiki_configurations')

AFFECTED_TABLES = ('qq_official_file_artifacts', 'qq_official_native_schedule_occurrences', 'qq_official_native_schedule_sources', 'qq_official_proactive_observations', 'qq_official_project_resource_grants', 'qq_official_project_resource_origins', 'qq_official_wiki_configurations')

HAS_EVIDENCE = "SELECT EXISTS(SELECT 1 FROM qq_official_native_schedule_occurrences) OR EXISTS(SELECT 1 FROM qq_official_native_schedule_sources) OR EXISTS(SELECT 1 FROM qq_official_proactive_observations) OR EXISTS(SELECT 1 FROM qq_official_project_resource_grants) OR EXISTS(SELECT 1 FROM qq_official_project_resource_origins) OR EXISTS(SELECT 1 FROM qq_official_wiki_configurations) OR EXISTS(SELECT 1 FROM qq_official_file_artifacts WHERE mime IN ('image/png','image/jpeg'))"

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
        raise RuntimeError("capability_evidence_requires_export")
    for statement in DOWNGRADE:
        _execute(statement)
