"""One immutable target-bot notice proposal; no sender or public effect yet."""
import os
from alembic import op

revision = '0105_question_notice_sources'
down_revision = '0104_conversation_questions'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def upgrade():
    schema()
    op.execute('''ALTER TABLE qq_official_conversation_questions ADD CONSTRAINT uq_question_notice_identity
        UNIQUE(question_id,account_key,target_endpoint_key,job_id)''')
    op.execute('''CREATE TABLE qq_official_question_notice_sources (
        notice_id TEXT PRIMARY KEY CHECK(notice_id ~ '^QN-[0-9a-f]{32}$'),
        question_id TEXT NOT NULL UNIQUE,
        account_key TEXT NOT NULL, account_epoch BIGINT NOT NULL CHECK(account_epoch>=1),
        account_holder_id TEXT NOT NULL CHECK(length(btrim(account_holder_id))>0),
        target_endpoint_key TEXT NOT NULL, target_scope_key TEXT NOT NULL,
        target_generation BIGINT NOT NULL CHECK(target_generation>=1),
        target_route_epoch BIGINT NOT NULL CHECK(target_route_epoch>=1),
        target_stop_generation BIGINT NOT NULL CHECK(target_stop_generation>=0),
        target_budget_generation BIGINT NOT NULL CHECK(target_budget_generation>=0),
        capability_revision BIGINT NOT NULL CHECK(capability_revision>=0),
        job_id BIGINT NOT NULL, worker_id TEXT NOT NULL CHECK(length(btrim(worker_id))>0),
        lease_generation BIGINT NOT NULL CHECK(lease_generation=1), attempts BIGINT NOT NULL CHECK(attempts=1),
        bot_native_id TEXT NOT NULL CHECK(length(btrim(bot_native_id))>0 AND octet_length(bot_native_id)<=512),
        bot_identity_source TEXT NOT NULL CHECK(bot_identity_source IN ('ready','self_api')),
        bot_identity_revision BIGINT NOT NULL CHECK(bot_identity_revision>=1),
        content_hmac TEXT NOT NULL CHECK(content_hmac ~ '^[0-9a-f]{64}$'),
        created_at BIGINT NOT NULL CHECK(created_at>=0), expires_at BIGINT NOT NULL,
        CONSTRAINT question_notice_target FOREIGN KEY(question_id,account_key,target_endpoint_key,job_id)
            REFERENCES qq_official_conversation_questions(question_id,account_key,target_endpoint_key,job_id),
        CHECK(created_at<expires_at AND expires_at-created_at<=30)
    )''')
    op.execute('''CREATE INDEX question_notice_account_recent ON qq_official_question_notice_sources
        (account_key,created_at DESC,notice_id DESC)''')
    op.execute('''CREATE FUNCTION protect_question_notice_source() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE q RECORD; j RECORD; a RECORD; e RECORD;
        BEGIN
        IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'immutable_question_notice_source'; END IF;
        SELECT * INTO q FROM qq_official_conversation_questions WHERE question_id=NEW.question_id;
        SELECT * INTO j FROM durable_jobs WHERE job_id=NEW.job_id;
        SELECT * INTO a FROM qq_official_accounts WHERE account_key=NEW.account_key;
        SELECT * INTO e FROM qq_official_endpoints WHERE endpoint_key=NEW.target_endpoint_key;
        IF q.state IS DISTINCT FROM 'running' OR q.started_at IS NULL OR q.finished_at IS NOT NULL
          OR NEW.created_at<q.started_at OR NEW.expires_at>q.deadline_at
          OR ROW(NEW.account_epoch,NEW.target_scope_key,NEW.target_generation,NEW.target_route_epoch,
                 NEW.target_stop_generation,NEW.target_budget_generation) IS DISTINCT FROM
             ROW(q.account_epoch,q.target_scope_key,q.target_generation,q.target_route_epoch,
                 q.target_stop_generation,q.target_budget_generation)
          OR a.fencing_epoch IS DISTINCT FROM NEW.account_epoch OR a.holder_id IS DISTINCT FROM NEW.account_holder_id
          OR a.lock_backend_pid IS NULL OR NOT a.enabled OR a.gateway_state='paused'
          OR ROW(NEW.bot_native_id,NEW.bot_identity_source,NEW.bot_identity_revision) IS DISTINCT FROM
             ROW(a.bot_native_id,a.bot_identity_source,a.bot_identity_revision)
          OR j.status IS DISTINCT FROM 'running' OR j.lease_until IS NULL OR j.lease_until<=NEW.created_at
          OR ROW(NEW.worker_id,NEW.lease_generation,NEW.attempts) IS DISTINCT FROM
             ROW(j.lease_owner,j.lease_generation,j.attempts)
          OR j.max_attempts IS DISTINCT FROM 1 OR j.resume_on_lease_loss IS DISTINCT FROM 0
          OR j.kind IS DISTINCT FROM 'qq_official.question.'||encode(sha256(convert_to(NEW.account_key,'UTF8')),'hex')
          OR j.scope_key IS DISTINCT FROM 'qqq:'||encode(sha256(convert_to(NEW.target_endpoint_key,'UTF8')),'hex')
          OR j.payload_json::jsonb IS DISTINCT FROM jsonb_build_object('question_id',NEW.question_id)
          OR e.kind IS DISTINCT FROM 'group' OR e.binding_state IS DISTINCT FROM 'active'
          OR e.membership_state IS DISTINCT FROM 'present' OR e.proactive_state IS DISTINCT FROM 'allowed'
          OR ROW(NEW.target_scope_key,NEW.target_generation,NEW.target_route_epoch,NEW.capability_revision) IS DISTINCT FROM
             ROW(e.canonical_scope_key,e.generation,e.route_epoch,e.capability_revision) THEN
            RAISE EXCEPTION 'question_notice_original_source_required'; END IF;
        RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_question_notice_source BEFORE INSERT OR UPDATE OR DELETE
        ON qq_official_question_notice_sources FOR EACH ROW EXECUTE FUNCTION protect_question_notice_source()''')
    op.execute('''CREATE TRIGGER protect_question_notice_truncate BEFORE TRUNCATE
        ON qq_official_question_notice_sources FOR EACH STATEMENT EXECUTE FUNCTION protect_question_notice_source()''')


def downgrade():
    schema()
    op.execute('LOCK TABLE qq_official_question_notice_sources IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_question_notice_sources LIMIT 1').first():
        raise RuntimeError('question_notice_sources_require_export_before_downgrade')
    op.execute('DROP TABLE qq_official_question_notice_sources')
    op.execute('DROP FUNCTION protect_question_notice_source()')
    op.execute('ALTER TABLE qq_official_conversation_questions DROP CONSTRAINT uq_question_notice_identity')
