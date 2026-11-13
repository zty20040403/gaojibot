"""Host-observed calls, immutable source/rate snapshots; not quota reservations."""
import os
from alembic import op

revision = '0094_model_usage_calls'
down_revision = '0093_conversation_config'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE model_usage_calls (
        call_id TEXT PRIMARY KEY CHECK(call_id ~ '^[0-9a-f]{32}$'),
        endpoint_key TEXT NOT NULL REFERENCES qq_official_endpoints(endpoint_key),
        scope_key TEXT NOT NULL, actor_key TEXT, purpose TEXT NOT NULL CHECK(purpose ~ '^[a-z_]{1,40}$'),
        turn_id BIGINT, background_job_id BIGINT REFERENCES background_jobs(job_id),
        program_wake_id TEXT REFERENCES qq_official_program_wakes(wake_id),
        provider TEXT NOT NULL, model TEXT NOT NULL, profile TEXT NOT NULL,
        pricing_json TEXT CHECK(octet_length(pricing_json)<=2048 AND jsonb_typeof(pricing_json::jsonb)='object'),
        started_at BIGINT NOT NULL, finished_at BIGINT,
        input_tokens BIGINT CHECK(input_tokens>=0), output_tokens BIGINT CHECK(output_tokens>=0),
        cached_tokens BIGINT CHECK(cached_tokens>=0), cache_write_tokens BIGINT CHECK(cache_write_tokens>=0),
        cost_usd NUMERIC(40,12) NOT NULL DEFAULT 0 CHECK(cost_usd>=0), cost_complete BOOLEAN NOT NULL DEFAULT FALSE,
        CHECK(input_tokens IS NULL OR COALESCE(cached_tokens,0)::numeric+COALESCE(cache_write_tokens,0)<=input_tokens),
        CHECK(NOT cost_complete OR (finished_at IS NOT NULL AND pricing_json IS NOT NULL
            AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
            AND cached_tokens IS NOT NULL AND cache_write_tokens IS NOT NULL AND cache_write_tokens=0)))''')
    op.execute('CREATE INDEX model_usage_scope_time ON model_usage_calls(scope_key,started_at DESC,call_id)')
    op.execute('CREATE INDEX model_usage_time ON model_usage_calls(started_at DESC,call_id)')
    op.execute('''CREATE FUNCTION immutable_model_usage_call() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.call_id,NEW.endpoint_key,NEW.scope_key,NEW.actor_key,NEW.purpose,NEW.turn_id,
                   NEW.background_job_id,NEW.program_wake_id,NEW.provider,NEW.model,NEW.profile,NEW.pricing_json,NEW.started_at)
               IS DISTINCT FROM
               ROW(OLD.call_id,OLD.endpoint_key,OLD.scope_key,OLD.actor_key,OLD.purpose,OLD.turn_id,
                   OLD.background_job_id,OLD.program_wake_id,OLD.provider,OLD.model,OLD.profile,OLD.pricing_json,OLD.started_at)
               OR OLD.finished_at IS NOT NULL THEN
                RAISE EXCEPTION 'immutable_model_usage_call';
            END IF;
            RETURN NEW;
        END $$''')
    op.execute('CREATE TRIGGER model_usage_call_immutable BEFORE UPDATE ON model_usage_calls FOR EACH ROW EXECUTE FUNCTION immutable_model_usage_call()')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM model_usage_calls LIMIT 1').first():
        raise RuntimeError('model_usage_requires_export_before_downgrade')
    op.drop_table('model_usage_calls',schema=schema)
    op.execute('DROP FUNCTION immutable_model_usage_call()')
