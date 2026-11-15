"""Private bilateral question tasks on the existing queue, never public history."""
import os
from alembic import op

revision = '0104_conversation_questions'
down_revision = '0103_link_feedback'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def upgrade():
    schema()
    op.execute('''ALTER TABLE qq_official_conversation_links ADD CONSTRAINT uq_question_link_identity
        UNIQUE(link_id,account_key,source_endpoint_key,target_endpoint_key)''')
    op.execute('''ALTER TABLE qq_official_turn_executions ADD CONSTRAINT uq_question_execution_identity
        UNIQUE(execution_id,account_key,endpoint_key,requester_actor_key)''')
    op.execute('''CREATE TABLE qq_official_conversation_questions (
        question_id TEXT PRIMARY KEY CHECK(question_id ~ '^CQ-[0-9a-f]{32}$'),
        job_id BIGINT NOT NULL UNIQUE REFERENCES durable_jobs(job_id),
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        account_epoch BIGINT NOT NULL CHECK(account_epoch>=1),
        link_id TEXT NOT NULL, link_generation BIGINT NOT NULL CHECK(link_generation>=1),
        source_endpoint_key TEXT NOT NULL, target_endpoint_key TEXT NOT NULL,
        source_scope_key TEXT NOT NULL, target_scope_key TEXT NOT NULL,
        source_generation BIGINT NOT NULL CHECK(source_generation>=1), source_route_epoch BIGINT NOT NULL CHECK(source_route_epoch>=1),
        target_generation BIGINT NOT NULL CHECK(target_generation>=1), target_route_epoch BIGINT NOT NULL CHECK(target_route_epoch>=1),
        source_actor_key TEXT NOT NULL, source_execution_id TEXT NOT NULL,
        source_execution_revision BIGINT NOT NULL CHECK(source_execution_revision>=1),
        source_stop_generation BIGINT NOT NULL CHECK(source_stop_generation>=0),
        target_stop_generation BIGINT NOT NULL CHECK(target_stop_generation>=0),
        target_budget_generation BIGINT NOT NULL CHECK(target_budget_generation>=0),
        request_hmac TEXT NOT NULL CHECK(request_hmac ~ '^[0-9a-f]{64}$'),
        question TEXT NOT NULL CHECK(length(btrim(question))>0 AND length(question)<=8000),
        state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','answered','refused','cancelled','expired','unknown')),
        answer TEXT NOT NULL DEFAULT '' CHECK(length(answer)<=16000),
        reason_code TEXT NOT NULL DEFAULT '' CHECK(reason_code IN ('','declined','cancelled','expired','source_unavailable','target_unavailable','model_unknown','restart')),
        created_at BIGINT NOT NULL CHECK(created_at>=0), source_deadline_at BIGINT NOT NULL, deadline_at BIGINT NOT NULL,
        started_at BIGINT, finished_at BIGINT, consumed_at BIGINT,
        CONSTRAINT question_link_identity FOREIGN KEY(link_id,account_key,source_endpoint_key,target_endpoint_key)
            REFERENCES qq_official_conversation_links(link_id,account_key,source_endpoint_key,target_endpoint_key),
        CONSTRAINT question_source_execution FOREIGN KEY(source_execution_id,account_key,source_endpoint_key,source_actor_key)
            REFERENCES qq_official_turn_executions(execution_id,account_key,endpoint_key,requester_actor_key),
        UNIQUE(account_key,source_execution_id,request_hmac),
        CHECK(source_endpoint_key<>target_endpoint_key),
        CHECK(created_at<deadline_at AND deadline_at-created_at<=7200 AND deadline_at<=source_deadline_at),
        CHECK(started_at IS NULL OR started_at>=created_at AND started_at<deadline_at),
        CHECK(finished_at IS NULL OR finished_at>=COALESCE(started_at,created_at)),
        CHECK((state IN ('answered','refused','cancelled','expired','unknown'))=(finished_at IS NOT NULL)),
        CHECK(state<>'queued' OR started_at IS NULL),
        CHECK(state NOT IN ('running','answered','refused','unknown') OR started_at IS NOT NULL),
        CHECK((state='answered')=(length(btrim(answer))>0)), CHECK(state='answered' OR answer=''),
        CHECK(state NOT IN ('answered','refused') OR finished_at<deadline_at),
        CHECK(state<>'expired' OR finished_at>=deadline_at),
        CHECK((state IN ('queued','running','answered'))=(reason_code='')),
        CHECK(consumed_at IS NULL OR state IN ('answered','refused') AND consumed_at>=finished_at AND consumed_at<deadline_at)
    )''')
    op.execute('CREATE INDEX question_account_recent ON qq_official_conversation_questions(account_key,created_at DESC,question_id DESC)')
    op.execute('''CREATE FUNCTION protect_conversation_question() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE l RECORD; x RECORD; j RECORD; c RECORD; a RECORD;
        BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_conversation_question'; END IF;
        IF TG_OP='INSERT' THEN
            SELECT * INTO l FROM qq_official_conversation_links WHERE link_id=NEW.link_id;
            SELECT * INTO c FROM qq_official_link_challenges WHERE challenge_id=l.challenge_id;
            SELECT * INTO x FROM qq_official_turn_executions WHERE execution_id=NEW.source_execution_id;
            SELECT * INTO j FROM durable_jobs WHERE job_id=NEW.job_id;
            SELECT * INTO a FROM qq_official_accounts WHERE account_key=NEW.account_key;
            IF a.fencing_epoch IS DISTINCT FROM NEW.account_epoch OR a.holder_id IS NULL OR NOT a.enabled
              OR l.state IS DISTINCT FROM 'active' OR c.delivery_receipt_hash IS NULL OR c.delivery_state IS DISTINCT FROM 'committed'
              OR ROW(NEW.link_generation,NEW.source_scope_key,NEW.target_scope_key,NEW.source_generation,NEW.source_route_epoch,
                NEW.target_generation,NEW.target_route_epoch) IS DISTINCT FROM ROW(l.generation,l.source_scope_key,l.target_scope_key,
                l.source_generation,l.source_route_epoch,l.target_generation,l.target_route_epoch)
              OR x.state IS DISTINCT FROM 'executing' OR x.execution_kind IS DISTINCT FROM 'chat'
              OR ROW(NEW.account_epoch,NEW.source_execution_revision,NEW.source_generation,NEW.source_route_epoch,NEW.source_scope_key,NEW.source_deadline_at)
                IS DISTINCT FROM ROW(x.account_fencing_epoch,x.execution_revision,x.endpoint_generation,x.route_epoch,x.scope_key,x.deadline_at)
              OR NEW.created_at<x.created_at OR NEW.created_at>=x.deadline_at
              OR j.status IS DISTINCT FROM 'pending' OR j.max_attempts IS DISTINCT FROM 1 OR j.resume_on_lease_loss IS DISTINCT FROM 0
              OR j.kind IS DISTINCT FROM 'qq_official.question.'||encode(sha256(convert_to(NEW.account_key,'UTF8')),'hex')
              OR j.scope_key IS DISTINCT FROM 'qqq:'||encode(sha256(convert_to(NEW.target_endpoint_key,'UTF8')),'hex')
              OR j.payload_json::jsonb IS DISTINCT FROM jsonb_build_object('question_id',NEW.question_id)
              OR NEW.state<>'queued' OR NEW.answer<>'' OR NEW.reason_code<>'' OR NEW.consumed_at IS NOT NULL THEN
                RAISE EXCEPTION 'question_original_source_required'; END IF;
        ELSE
            IF (to_jsonb(NEW)-ARRAY['state','answer','reason_code','started_at','finished_at','consumed_at']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['state','answer','reason_code','started_at','finished_at','consumed_at'])
              OR OLD.state IN ('answered','refused','cancelled','expired','unknown') AND
                (to_jsonb(NEW)-'consumed_at') IS DISTINCT FROM (to_jsonb(OLD)-'consumed_at')
              OR OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
              OR NEW.consumed_at IS NOT NULL AND OLD.state NOT IN ('answered','refused')
              OR OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at
              OR OLD.state='running' AND NEW.state='queued'
              OR OLD.state='queued' AND NEW.state IN ('answered','refused','unknown') THEN
                RAISE EXCEPTION 'immutable_conversation_question'; END IF;
        END IF; RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_conversation_question BEFORE INSERT OR UPDATE OR DELETE
        ON qq_official_conversation_questions FOR EACH ROW EXECUTE FUNCTION protect_conversation_question()''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_conversation_questions LIMIT 1').first():
        raise RuntimeError('conversation_questions_require_export_before_downgrade')
    op.execute('DROP TABLE qq_official_conversation_questions')
    op.execute('DROP FUNCTION protect_conversation_question()')
    op.execute('ALTER TABLE qq_official_turn_executions DROP CONSTRAINT uq_question_execution_identity')
    op.execute('ALTER TABLE qq_official_conversation_links DROP CONSTRAINT uq_question_link_identity')
