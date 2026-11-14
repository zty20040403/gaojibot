"""One ephemeral feedback plan per frozen original control outcome."""
import importlib
from alembic import op

revision = '0103_link_feedback'
down_revision = '0102_link_replies'
branch_labels = depends_on = None
baseline = importlib.import_module('migrations.versions.0101_link_notices')


def constraints(extended):
    if len(baseline.BASE)!=4:raise RuntimeError('link_feedback_constraint_baseline_missing')
    for original in baseline.BASE:
        prefix,expression = original.split(' CHECK',1); _,_,table,_,_,name = prefix.split()
        kinds = "'binding_otp','approval_otp','ops_approval','link_otp'"+(",'link_feedback'" if extended else '')
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
            if extended:
                expression = '('+expression+''' OR (purpose='link_feedback' AND payload_mode='ephemeral'
                    AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                    AND challenge_id IS NULL AND challenge_expires_at IS NULL AND ops_action_id IS NULL
                    AND approval_source_id IS NULL AND approval_source_expires_at IS NULL AND link_challenge_id IS NULL
                    AND link_reply_inbox_id IS NOT NULL AND manifest_json='{"v":1,"ephemeral":true}'))'''
        else:expression = expression.replace("'approval_otp'","'approval_otp','ops_approval','link_otp'"+(",'link_feedback'" if extended else ''))
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT {name}')
        op.get_bind().exec_driver_sql(prefix+' CHECK '+expression)


def upgrade():
    baseline.schema()
    op.execute('''ALTER TABLE qq_official_link_replies ADD CONSTRAINT link_reply_identity
        UNIQUE(source_inbox_id,account_key,endpoint_key,actor_key)''')
    op.execute('''ALTER TABLE delivery_plans ADD COLUMN link_reply_inbox_id TEXT UNIQUE,
        ADD CONSTRAINT link_feedback_kind CHECK((purpose='link_feedback')=(link_reply_inbox_id IS NOT NULL)),
        ADD CONSTRAINT link_feedback_original_source FOREIGN KEY(link_reply_inbox_id,account_key,endpoint_key,requester_actor_key)
            REFERENCES qq_official_link_replies(source_inbox_id,account_key,endpoint_key,actor_key)''')
    constraints(True)
    op.execute('''CREATE FUNCTION protect_link_feedback_plan() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE original RECORD; BEGIN
        IF TG_OP='DELETE' THEN
            IF OLD.link_reply_inbox_id IS NOT NULL THEN RAISE EXCEPTION 'immutable_link_feedback_plan'; END IF; RETURN OLD;
        END IF;
        IF TG_OP='UPDATE' THEN
            IF OLD.link_reply_inbox_id IS NOT NULL OR NEW.link_reply_inbox_id IS NOT NULL THEN
                RAISE EXCEPTION 'immutable_link_feedback_plan'; END IF; RETURN NEW;
        END IF;
        IF NEW.link_reply_inbox_id IS NULL THEN RETURN NEW; END IF;
        SELECT * INTO original FROM qq_official_link_replies WHERE source_inbox_id=NEW.link_reply_inbox_id;
        IF original.source_inbox_id IS NULL OR NEW.created_at<original.received_at OR NEW.created_at>=original.expires_at
          OR NEW.expires_at>original.expires_at OR NEW.expires_at>NEW.created_at+30
          OR ROW(NEW.source_scope_key,NEW.endpoint_generation,NEW.route_epoch) IS DISTINCT FROM
             ROW(original.scope_key,original.endpoint_generation,original.route_epoch)
          OR NEW.source_canonical_message_id IS NOT NULL OR NEW.turn_id IS NOT NULL THEN
            RAISE EXCEPTION 'link_feedback_original_source_required'; END IF; RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_link_feedback_plan BEFORE INSERT OR UPDATE OR DELETE ON delivery_plans
        FOR EACH ROW EXECUTE FUNCTION protect_link_feedback_plan()''')


def downgrade():
    baseline.schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM delivery_plans WHERE link_reply_inbox_id IS NOT NULL LIMIT 1').first():
        raise RuntimeError('link_feedback_requires_export_before_downgrade')
    op.execute('DROP TRIGGER protect_link_feedback_plan ON delivery_plans')
    op.execute('DROP FUNCTION protect_link_feedback_plan()')
    constraints(False)
    op.execute('ALTER TABLE delivery_plans DROP COLUMN link_reply_inbox_id')
    op.execute('ALTER TABLE qq_official_link_replies DROP CONSTRAINT link_reply_identity')
