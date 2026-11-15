"""One ephemeral question-notice plan and real applied receipt fact per notice source."""
import importlib
from alembic import op

revision = '0106_question_notices'
down_revision = '0105_question_notice_sources'
branch_labels = depends_on = None
baseline = importlib.import_module('migrations.versions.0101_link_notices')


def constraints(extended):
    if len(baseline.BASE)!=4:raise RuntimeError('question_notice_constraint_baseline_missing')
    for original in baseline.BASE:
        prefix,expression = original.split(' CHECK',1); _,_,table,_,_,name = prefix.split()
        kinds = "'binding_otp','approval_otp','ops_approval','link_otp','link_feedback'"+(",'question_notice'" if extended else '')
        if name=='delivery_plans_check2':
            expression = expression.replace("purpose NOT IN ('binding_otp','approval_otp')",f'purpose NOT IN ({kinds})')
            expression = '('+expression+''' OR (purpose='ops_approval' AND payload_mode='ephemeral'
                AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                AND challenge_id IS NULL AND challenge_expires_at IS NULL
                AND approval_source_id IS NULL AND approval_source_expires_at IS NULL
                AND ops_action_id IS NOT NULL AND manifest_json='{"v":1,"ephemeral":true}'))'''
            expression = '('+expression+''' OR (purpose='link_otp' AND payload_mode='ephemeral'
                AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                AND challenge_id IS NULL AND challenge_expires_at IS NULL AND ops_action_id IS NULL
                AND approval_source_id IS NULL AND approval_source_expires_at IS NULL
                AND link_challenge_id IS NOT NULL AND manifest_json='{"v":1,"ephemeral":true}'))'''
            expression = '('+expression+''' OR (purpose='link_feedback' AND payload_mode='ephemeral'
                AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                AND challenge_id IS NULL AND challenge_expires_at IS NULL AND ops_action_id IS NULL
                AND approval_source_id IS NULL AND approval_source_expires_at IS NULL AND link_challenge_id IS NULL
                AND link_reply_inbox_id IS NOT NULL AND manifest_json='{"v":1,"ephemeral":true}'))'''
            if extended:
                expression = '('+expression+''' OR (purpose='question_notice' AND payload_mode='ephemeral'
                    AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                    AND challenge_id IS NULL AND challenge_expires_at IS NULL AND ops_action_id IS NULL
                    AND approval_source_id IS NULL AND approval_source_expires_at IS NULL AND link_challenge_id IS NULL
                    AND link_reply_inbox_id IS NULL AND question_notice_id IS NOT NULL
                    AND manifest_json='{"v":1,"ephemeral":true}'))'''
        else:expression = expression.replace("'approval_otp'","'approval_otp','ops_approval','link_otp','link_feedback'"+(",'question_notice'" if extended else ''))
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT {name}')
        op.get_bind().exec_driver_sql(prefix+' CHECK '+expression)


def upgrade():
    baseline.schema()
    op.execute('''ALTER TABLE qq_official_question_notice_sources
        ADD CONSTRAINT uq_question_notice_source_plan UNIQUE(
            notice_id,account_key,target_endpoint_key,target_scope_key,target_generation,target_route_epoch,capability_revision),
        ADD CONSTRAINT uq_question_notice_source_identity UNIQUE(
            notice_id,question_id,account_key,target_endpoint_key,bot_native_id)''')
    op.execute('''ALTER TABLE delivery_receipts ADD CONSTRAINT uq_delivery_receipts_account_endpoint
        UNIQUE(receipt_id,account_key,endpoint_key)''')
    op.execute('''ALTER TABLE delivery_plans ADD COLUMN question_notice_id TEXT UNIQUE,
        ADD CONSTRAINT question_notice_kind CHECK((purpose='question_notice')=(question_notice_id IS NOT NULL)),
        ADD CONSTRAINT question_notice_plan_identity UNIQUE(plan_id,question_notice_id,account_key,endpoint_key,requester_actor_key),
        ADD CONSTRAINT question_notice_original_source FOREIGN KEY(
            question_notice_id,account_key,endpoint_key,source_scope_key,endpoint_generation,route_epoch,capability_revision)
            REFERENCES qq_official_question_notice_sources(
                notice_id,account_key,target_endpoint_key,target_scope_key,target_generation,target_route_epoch,capability_revision)''')
    constraints(True)
    op.execute('''CREATE FUNCTION protect_question_notice_plan() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE s RECORD; e RECORD; actor_json JSONB;
        BEGIN
        IF TG_OP='DELETE' THEN
            IF OLD.question_notice_id IS NOT NULL THEN RAISE EXCEPTION 'immutable_question_notice_plan'; END IF; RETURN OLD;
        END IF;
        IF TG_OP='UPDATE' THEN
            IF OLD.question_notice_id IS NOT NULL OR NEW.question_notice_id IS NOT NULL THEN
                RAISE EXCEPTION 'immutable_question_notice_plan'; END IF; RETURN NEW;
        END IF;
        IF NEW.question_notice_id IS NULL THEN RETURN NEW; END IF;
        SELECT * INTO s FROM qq_official_question_notice_sources WHERE notice_id=NEW.question_notice_id;
        SELECT * INTO e FROM qq_official_endpoints WHERE endpoint_key=s.target_endpoint_key;
        IF s.notice_id IS NULL OR e.endpoint_key IS NULL OR e.kind IS DISTINCT FROM 'group'
          OR EXISTS(SELECT 1 FROM qq_official_question_notices n WHERE n.notice_id=NEW.question_notice_id AND n.state<>'pending')
          OR NEW.created_at<s.created_at OR NEW.created_at>=s.expires_at
          OR NEW.expires_at>s.expires_at OR NEW.expires_at>NEW.created_at+30
          OR NEW.source_canonical_message_id IS NOT NULL OR NEW.turn_id IS NOT NULL
          OR NEW.requester_actor_key NOT LIKE 'actor:v1:[%]'
          OR NEW.requester_actor_key ~ '^actor:v1:\\s' THEN
            RAISE EXCEPTION 'question_notice_original_source_required'; END IF;
        BEGIN
            actor_json := substring(NEW.requester_actor_key FROM 10)::jsonb;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'question_notice_original_source_required';
        END;
        IF actor_json IS DISTINCT FROM jsonb_build_array(s.account_key,'bot','',s.bot_native_id) THEN
            RAISE EXCEPTION 'question_notice_original_source_required'; END IF;
        RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_question_notice_plan BEFORE INSERT OR UPDATE OR DELETE ON delivery_plans
        FOR EACH ROW EXECUTE FUNCTION protect_question_notice_plan()''')
    op.execute('''CREATE TABLE qq_official_question_notices (
        notice_id TEXT PRIMARY KEY,
        question_id TEXT NOT NULL UNIQUE,
        account_key TEXT NOT NULL,
        target_endpoint_key TEXT NOT NULL,
        bot_native_id TEXT NOT NULL,
        bot_actor_key TEXT NOT NULL CHECK(octet_length(bot_actor_key) BETWEEN 1 AND 1280),
        state TEXT NOT NULL CHECK(state IN ('pending','sending','committed','failed','cancelled','expired','unknown')),
        plan_id TEXT UNIQUE,
        delivery_state TEXT NOT NULL CHECK(delivery_state IN ('pending','committed','failed','cancelled','unknown')),
        receipt_id TEXT UNIQUE,
        delivery_receipt_hash TEXT CHECK(delivery_receipt_hash ~ '^[0-9a-f]{64}$'),
        delivery_received_at BIGINT,
        created_at BIGINT NOT NULL CHECK(created_at>=0),
        updated_at BIGINT NOT NULL CHECK(updated_at>=created_at),
        CONSTRAINT question_notice_source_identity FOREIGN KEY(
            notice_id,question_id,account_key,target_endpoint_key,bot_native_id)
            REFERENCES qq_official_question_notice_sources(
                notice_id,question_id,account_key,target_endpoint_key,bot_native_id),
        CONSTRAINT question_notice_delivery_plan FOREIGN KEY(
            plan_id,notice_id,account_key,target_endpoint_key,bot_actor_key)
            REFERENCES delivery_plans(
                plan_id,question_notice_id,account_key,endpoint_key,requester_actor_key),
        CONSTRAINT question_notice_delivery_receipt FOREIGN KEY(
            receipt_id,account_key,target_endpoint_key)
            REFERENCES delivery_receipts(receipt_id,account_key,endpoint_key),
        CHECK(state<>'sending' OR plan_id IS NOT NULL),
        CHECK(state<>'committed' OR (plan_id IS NOT NULL AND delivery_state='committed'
            AND receipt_id IS NOT NULL AND delivery_receipt_hash IS NOT NULL AND delivery_received_at IS NOT NULL)),
        CHECK((receipt_id IS NULL)=(delivery_receipt_hash IS NULL) AND (receipt_id IS NULL)=(delivery_received_at IS NULL)),
        CHECK(receipt_id IS NULL OR (plan_id IS NOT NULL AND delivery_state='committed')),
        CHECK(delivery_state<>'committed' OR receipt_id IS NOT NULL)
    )''')
    op.execute('''CREATE UNIQUE INDEX uq_question_notice_receipt_hash ON qq_official_question_notices(delivery_receipt_hash)
        WHERE delivery_receipt_hash IS NOT NULL''')
    op.execute('''CREATE INDEX question_notice_account_state ON qq_official_question_notices
        (account_key,state,updated_at DESC,notice_id DESC)''')
    op.execute('''CREATE FUNCTION protect_question_notice() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE s RECORD; rec RECORD; actor_json JSONB;
        BEGIN
        IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'immutable_question_notice'; END IF;
        SELECT * INTO s FROM qq_official_question_notice_sources WHERE notice_id=NEW.notice_id;
        IF s.notice_id IS NULL OR NEW.created_at IS DISTINCT FROM s.created_at
          OR NEW.bot_actor_key NOT LIKE 'actor:v1:[%]' OR NEW.bot_actor_key ~ '^actor:v1:\\s' THEN
            RAISE EXCEPTION 'question_notice_original_source_required'; END IF;
        BEGIN
            actor_json := substring(NEW.bot_actor_key FROM 10)::jsonb;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'question_notice_original_source_required';
        END;
        IF actor_json IS DISTINCT FROM jsonb_build_array(s.account_key,'bot','',s.bot_native_id) THEN
            RAISE EXCEPTION 'question_notice_original_source_required'; END IF;
        IF TG_OP='INSERT' THEN
            IF NEW.state IS DISTINCT FROM 'pending' OR NEW.plan_id IS NOT NULL
              OR NEW.delivery_state IS DISTINCT FROM 'pending' OR NEW.receipt_id IS NOT NULL
              OR NEW.updated_at IS DISTINCT FROM s.created_at THEN
                RAISE EXCEPTION 'immutable_question_notice'; END IF;
            RETURN NEW;
        END IF;
        IF ROW(NEW.notice_id,NEW.question_id,NEW.account_key,NEW.target_endpoint_key,NEW.bot_native_id,NEW.bot_actor_key,NEW.created_at)
            IS DISTINCT FROM ROW(OLD.notice_id,OLD.question_id,OLD.account_key,OLD.target_endpoint_key,OLD.bot_native_id,OLD.bot_actor_key,OLD.created_at)
          OR NEW.updated_at<OLD.updated_at
          OR (OLD.plan_id IS NOT NULL AND NEW.plan_id IS DISTINCT FROM OLD.plan_id)
          OR (OLD.state='sending' AND NEW.state='pending')
          OR (OLD.state IN ('committed','failed','cancelled','expired','unknown')
              AND (NEW.state IS DISTINCT FROM OLD.state OR NEW.plan_id IS DISTINCT FROM OLD.plan_id))
          OR (OLD.receipt_id IS NOT NULL AND ROW(NEW.delivery_state,NEW.receipt_id,NEW.delivery_receipt_hash,NEW.delivery_received_at)
              IS DISTINCT FROM ROW(OLD.delivery_state,OLD.receipt_id,OLD.delivery_receipt_hash,OLD.delivery_received_at))
          OR (OLD.delivery_state IN ('failed','cancelled','unknown') AND NEW.delivery_state<>'committed'
              AND NEW.delivery_state IS DISTINCT FROM OLD.delivery_state) THEN
            RAISE EXCEPTION 'immutable_question_notice'; END IF;
        IF OLD.plan_id IS NULL AND NEW.plan_id IS NOT NULL THEN
            IF OLD.state IS DISTINCT FROM 'pending' OR NEW.state IS DISTINCT FROM 'sending'
              OR NEW.updated_at>=s.expires_at THEN
                RAISE EXCEPTION 'immutable_question_notice'; END IF;
        END IF;
        IF NEW.receipt_id IS NOT NULL AND OLD.receipt_id IS NULL THEN
            SELECT r.*,a.bot_actor_key AS attempt_bot_actor_key,a.bot_identity_source AS attempt_bot_source,
                   a.bot_identity_revision AS attempt_bot_revision,a.send_mode AS attempt_send_mode,
                   d.plan_id AS delivery_plan_id,d.status AS delivery_status,
                   d.native_message_id AS delivery_native_id,d.reply_to_native_message_id AS delivery_reply_to
              INTO rec FROM delivery_receipts r JOIN delivery_attempts a ON a.attempt_id=r.attempt_id
              JOIN deliveries d ON d.delivery_id=a.delivery_id WHERE r.receipt_id=NEW.receipt_id;
            IF rec.receipt_id IS NULL OR rec.kind IS DISTINCT FROM 'response' OR rec.applied_at IS NULL
              OR rec.account_key IS DISTINCT FROM NEW.account_key OR rec.endpoint_key IS DISTINCT FROM NEW.target_endpoint_key
              OR rec.delivery_plan_id IS DISTINCT FROM NEW.plan_id OR rec.delivery_status IS DISTINCT FROM 'committed'
              OR rec.delivery_native_id IS DISTINCT FROM rec.native_message_id
              OR rec.delivery_reply_to IS DISTINCT FROM ''
              OR rec.attempt_send_mode IS DISTINCT FROM 'proactive'
              OR rec.attempt_bot_actor_key IS DISTINCT FROM NEW.bot_actor_key
              OR rec.attempt_bot_source IS DISTINCT FROM s.bot_identity_source
              OR rec.attempt_bot_revision IS DISTINCT FROM s.bot_identity_revision
              OR NEW.delivery_received_at IS DISTINCT FROM rec.observed_at
              OR NEW.delivery_received_at<s.created_at THEN
                RAISE EXCEPTION 'question_notice_receipt_required'; END IF;
        END IF;
        RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_question_notice BEFORE INSERT OR UPDATE OR DELETE
        ON qq_official_question_notices FOR EACH ROW EXECUTE FUNCTION protect_question_notice()''')
    op.execute('''CREATE TRIGGER protect_question_notice_row_truncate BEFORE TRUNCATE
        ON qq_official_question_notices FOR EACH STATEMENT EXECUTE FUNCTION protect_question_notice()''')


def downgrade():
    baseline.schema()
    op.execute('LOCK TABLE qq_official_question_notices,delivery_plans IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM qq_official_question_notices
        UNION ALL SELECT 1 FROM delivery_plans WHERE question_notice_id IS NOT NULL LIMIT 1''').first():
        raise RuntimeError('question_notices_require_export_before_downgrade')
    op.execute('DROP TRIGGER protect_question_notice_plan ON delivery_plans')
    op.execute('DROP FUNCTION protect_question_notice_plan()')
    op.execute('DROP TABLE qq_official_question_notices')
    op.execute('DROP FUNCTION protect_question_notice()')
    constraints(False)
    op.execute('ALTER TABLE delivery_plans DROP COLUMN question_notice_id')
    op.execute('ALTER TABLE delivery_receipts DROP CONSTRAINT uq_delivery_receipts_account_endpoint')
    op.execute('''ALTER TABLE qq_official_question_notice_sources
        DROP CONSTRAINT uq_question_notice_source_plan,
        DROP CONSTRAINT uq_question_notice_source_identity''')
