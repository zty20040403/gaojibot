"""Owner-private recipe intents; consent never authorizes a different job/lease."""
import os
from alembic import op

revision = '0089_ops_actions'
down_revision = '0088_agent_time_limits'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE ops_actions (
        action_id TEXT PRIMARY KEY CHECK(action_id ~ '^[a-f0-9]{32}$'),
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL REFERENCES qq_official_endpoints(endpoint_key),
        actor_key TEXT NOT NULL, scope_key TEXT NOT NULL,
        job_id BIGINT NOT NULL REFERENCES background_jobs(job_id),
        request_id TEXT NOT NULL CHECK(request_id ~ '^[A-Za-z0-9_-]{1,64}$'),
        account_epoch BIGINT NOT NULL, job_lease_owner TEXT NOT NULL, job_lease_generation BIGINT NOT NULL,
        endpoint_generation BIGINT NOT NULL, route_epoch BIGINT NOT NULL, conversation_generation BIGINT NOT NULL,
        owner_grant_id TEXT NOT NULL REFERENCES qq_official_owner_grants(grant_id), owner_revision BIGINT NOT NULL,
        plan_name TEXT NOT NULL CHECK(plan_name ~ '^[a-z][a-z0-9_-]{0,47}$'),
        parameters_json TEXT NOT NULL CHECK(octet_length(parameters_json)<=16384 AND jsonb_typeof(parameters_json::jsonb)='object'),
        binding_hash TEXT NOT NULL CHECK(binding_hash ~ '^[a-f0-9]{64}$'),
        intent_hash TEXT NOT NULL CHECK(intent_hash ~ '^[a-f0-9]{64}$'),
        state TEXT NOT NULL CHECK(state IN ('sending','pending','approved','executing','succeeded','failed','denied','expired','cancelled','unknown')),
        code_hash TEXT NOT NULL CHECK(code_hash='' OR code_hash ~ '^[a-f0-9]{64}$'),
        notice_plan_id TEXT UNIQUE REFERENCES delivery_plans(plan_id),
        approved_inbox_id TEXT UNIQUE REFERENCES qq_official_inbound_events(inbox_id),
        approved_actor_key TEXT, approved_at BIGINT, started_at BIGINT, finished_at BIGINT,
        remote_id TEXT NOT NULL DEFAULT '', remote_contract TEXT NOT NULL DEFAULT '', remote_version BIGINT,
        error_code TEXT NOT NULL DEFAULT '' CHECK(error_code='' OR error_code ~ '^[a-z0-9_]{1,80}$'),
        output_summary TEXT NOT NULL DEFAULT '' CHECK(octet_length(output_summary)<=16384),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
        UNIQUE(job_id,request_id),
        CHECK(expires_at>created_at AND expires_at<=created_at+600),
        CHECK((state IN ('sending','pending'))=(code_hash<>'')),
        CHECK((state IN ('succeeded','failed','denied','expired','cancelled','unknown'))=(finished_at IS NOT NULL)),
        CHECK((approved_at IS NULL)=(approved_actor_key IS NULL)),
        CHECK((approved_at IS NULL)=(approved_inbox_id IS NULL)),
        CHECK(state NOT IN ('approved','executing','succeeded') OR approved_at IS NOT NULL),
        CHECK(state<>'executing' OR started_at IS NOT NULL)
    )''')
    # One outstanding question per private endpoint gives "anything else denies"
    # an unambiguous meaning, even with several independent background agents.
    op.execute("CREATE UNIQUE INDEX ops_actions_question ON ops_actions(endpoint_key) WHERE state IN ('sending','pending')")
    op.execute('CREATE INDEX ops_actions_recent ON ops_actions(created_at DESC,action_id DESC)')
    op.execute('CREATE INDEX ops_actions_account ON ops_actions(account_key,state)')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM ops_actions LIMIT 1').first():
        raise RuntimeError('ops_actions_require_export_before_downgrade')
    op.drop_table('ops_actions',schema=schema)
