"""Store the classifier's bounded reason without changing observation authority."""
import os

from alembic import op
import sqlalchemy as sa

revision = '0121_attention_reason'
down_revision = '0120_retire_context_feedback'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    op.add_column('qq_official_proactive_observations', sa.Column('decision_reason', sa.String(40)), schema=schema)
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'''SET LOCAL search_path TO {quoted},public''')
    op.execute('''CREATE OR REPLACE FUNCTION guard_proactive_observation() RETURNS trigger LANGUAGE plpgsql AS $$
 DECLARE i qq_official_inbound_events%ROWTYPE; m messages%ROWTYPE; p delivery_plans%ROWTYPE;
 BEGIN
 IF TG_OP='INSERT' THEN
   SELECT * INTO i FROM qq_official_inbound_events WHERE inbox_id=NEW.inbox_id;
   SELECT * INTO m FROM messages WHERE canonical_message_id=NEW.canonical_message_id;
   IF NEW.decision_reason IS NOT NULL OR i.inbox_id IS NULL OR m.canonical_message_id IS NULL OR NEW.state<>'scoring'
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
 IF (to_jsonb(NEW)-ARRAY['state','plan_id','decision_reason']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','plan_id','decision_reason'])
    OR OLD.plan_id IS NOT NULL AND NEW.plan_id IS DISTINCT FROM OLD.plan_id
    OR OLD.state IN ('discarded','queued') AND (NEW.state IS DISTINCT FROM OLD.state OR NEW.decision_reason IS DISTINCT FROM OLD.decision_reason)
    OR OLD.state='scoring' AND NEW.state='scoring' AND NEW.decision_reason IS DISTINCT FROM OLD.decision_reason THEN
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
 END $$''')


def downgrade():
    raise RuntimeError('attention_reasons_require_backup_restore_before_downgrade')
