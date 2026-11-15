"""Body-free original ingress and one immutable ephemeral issuance plan."""
import importlib
import os
from alembic import op

revision = '0101_link_notices'
down_revision = '0100_link_receipts'
branch_labels = depends_on = None

# Exactly the same four historical baseline constraints as 0090, retaining
# ops and all non-link invariants. No legacy upgrade/downgrade is invoked.
BASE = importlib.import_module('migrations.versions.0090_ops_notices').BASE


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def constraints(extended):
    if len(BASE)!=4:raise RuntimeError('link_notice_constraint_baseline_missing')
    for original in BASE:
        prefix,expression = original.split(' CHECK',1)
        _,_,table,_,_,name = prefix.split()
        kinds = "'binding_otp','approval_otp','ops_approval'"+(",'link_otp'" if extended else '')
        if name=='delivery_plans_check2':
            expression = expression.replace("purpose NOT IN ('binding_otp','approval_otp')",f'purpose NOT IN ({kinds})')
            expression = '('+expression+''' OR (purpose='ops_approval' AND payload_mode='ephemeral'
                AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                AND challenge_id IS NULL AND challenge_expires_at IS NULL
                AND approval_source_id IS NULL AND approval_source_expires_at IS NULL
                AND ops_action_id IS NOT NULL AND manifest_json='{"v":1,"ephemeral":true}'))'''
            if extended:
                expression = '('+expression+''' OR (purpose='link_otp' AND payload_mode='ephemeral'
                    AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                    AND challenge_id IS NULL AND challenge_expires_at IS NULL AND ops_action_id IS NULL
                    AND approval_source_id IS NULL AND approval_source_expires_at IS NULL
                    AND link_challenge_id IS NOT NULL AND manifest_json='{"v":1,"ephemeral":true}'))'''
        else:expression = expression.replace("'approval_otp'","'approval_otp','ops_approval'"+(",'link_otp'" if extended else ''))
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT {name}')
        op.get_bind().exec_driver_sql(prefix+' CHECK '+expression)


def upgrade():
    schema()
    op.execute('''ALTER TABLE qq_official_inbound_events
        ADD COLUMN link_control_actor_key TEXT CHECK(octet_length(link_control_actor_key) BETWEEN 1 AND 1280),
        ADD COLUMN link_control_receipt_hash TEXT CHECK(link_control_receipt_hash ~ '^[0-9a-f]{64}$'),
        ADD COLUMN link_control_platform_at BIGINT,
        ADD CONSTRAINT link_control_body_free CHECK(
            (link_control_actor_key IS NULL AND link_control_receipt_hash IS NULL AND link_control_platform_at IS NULL) OR
            (link_control_actor_key IS NOT NULL AND link_control_receipt_hash IS NOT NULL AND link_control_platform_at IS NOT NULL
                AND link_control_platform_at>=0 AND link_control_platform_at<=received_at AND payload_json IS NULL
                AND endpoint_key IS NOT NULL AND native_message_id IS NOT NULL AND echo_state='not_echo'
                AND status='processed' AND reason_code='link_control')),
        ADD CONSTRAINT link_ingress_identity UNIQUE(inbox_id,account_key,endpoint_key,link_control_actor_key,link_control_receipt_hash)''')
    op.execute('''ALTER TABLE qq_official_link_challenges
        ADD COLUMN source_inbox_id TEXT UNIQUE,
        ADD COLUMN notice_plan_id TEXT UNIQUE,
        ADD CONSTRAINT link_original_ingress FOREIGN KEY(source_inbox_id,account_key,source_endpoint_key,source_actor_key,source_receipt_hash)
            REFERENCES qq_official_inbound_events(inbox_id,account_key,endpoint_key,link_control_actor_key,link_control_receipt_hash),
        ADD CONSTRAINT link_notice_original_source CHECK(notice_plan_id IS NULL OR source_inbox_id IS NOT NULL)''')
    op.execute('''ALTER TABLE delivery_plans
        ADD COLUMN link_challenge_id TEXT UNIQUE REFERENCES qq_official_link_challenges(challenge_id),
        ADD CONSTRAINT link_notice_kind CHECK((purpose='link_otp')=(link_challenge_id IS NOT NULL)),
        ADD CONSTRAINT link_notice_identity UNIQUE(plan_id,link_challenge_id,account_key,endpoint_key,requester_actor_key)''')
    op.execute('''ALTER TABLE qq_official_link_challenges ADD CONSTRAINT link_notice_source
        FOREIGN KEY(notice_plan_id,challenge_id,account_key,source_endpoint_key,source_actor_key)
        REFERENCES delivery_plans(plan_id,link_challenge_id,account_key,endpoint_key,requester_actor_key)''')
    constraints(True)
    op.execute('''CREATE FUNCTION protect_link_notice_source() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF (OLD.source_inbox_id IS NOT NULL AND NEW.source_inbox_id IS DISTINCT FROM OLD.source_inbox_id)
          OR (OLD.notice_plan_id IS NOT NULL AND NEW.notice_plan_id IS DISTINCT FROM OLD.notice_plan_id)
          OR (OLD.state NOT IN ('sending','issued','confirming') AND
            ROW(NEW.source_inbox_id,NEW.notice_plan_id) IS DISTINCT FROM ROW(OLD.source_inbox_id,OLD.notice_plan_id)) THEN
            RAISE EXCEPTION 'immutable_link_notice_source'; END IF; RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_link_notice_source BEFORE UPDATE ON qq_official_link_challenges
        FOR EACH ROW EXECUTE FUNCTION protect_link_notice_source()''')
    op.execute('''CREATE FUNCTION protect_link_ingress() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF OLD.link_control_actor_key IS NOT NULL AND
          ROW(NEW.account_key,NEW.endpoint_key,NEW.native_message_id,NEW.event_id,NEW.event_type,NEW.received_at,
              NEW.link_control_actor_key,NEW.link_control_receipt_hash,NEW.link_control_platform_at) IS DISTINCT FROM
          ROW(OLD.account_key,OLD.endpoint_key,OLD.native_message_id,OLD.event_id,OLD.event_type,OLD.received_at,
              OLD.link_control_actor_key,OLD.link_control_receipt_hash,OLD.link_control_platform_at)
          THEN RAISE EXCEPTION 'immutable_link_ingress'; END IF; RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_link_ingress BEFORE UPDATE ON qq_official_inbound_events
        FOR EACH ROW EXECUTE FUNCTION protect_link_ingress()''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM qq_official_inbound_events WHERE link_control_actor_key IS NOT NULL
        UNION ALL SELECT 1 FROM delivery_plans WHERE link_challenge_id IS NOT NULL LIMIT 1''').first():
        raise RuntimeError('link_notices_require_export_before_downgrade')
    op.execute('DROP TRIGGER protect_link_notice_source ON qq_official_link_challenges')
    op.execute('DROP TRIGGER protect_link_ingress ON qq_official_inbound_events')
    op.execute('DROP FUNCTION protect_link_notice_source(),protect_link_ingress()')
    op.execute('ALTER TABLE qq_official_link_challenges DROP CONSTRAINT link_notice_source')
    constraints(False)
    op.execute('ALTER TABLE delivery_plans DROP COLUMN link_challenge_id')
    op.execute('ALTER TABLE qq_official_link_challenges DROP COLUMN source_inbox_id,DROP COLUMN notice_plan_id')
    op.execute('''ALTER TABLE qq_official_inbound_events DROP COLUMN link_control_actor_key,
        DROP COLUMN link_control_receipt_hash,DROP COLUMN link_control_platform_at''')
