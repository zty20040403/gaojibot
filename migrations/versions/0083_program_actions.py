"""Program controls, bounded action intents and physical-conversation quotas."""
import os
from alembic import op

revision = '0083_program_actions'
down_revision = '0082_vm_sessions'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''ALTER TABLE conversation_settings
        ADD COLUMN programs_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        ADD COLUMN program_say_limit INTEGER NOT NULL DEFAULT 60 CHECK(program_say_limit BETWEEN 0 AND 10000),
        ADD COLUMN program_wake_limit INTEGER NOT NULL DEFAULT 6 CHECK(program_wake_limit BETWEEN 0 AND 10000)''')
    op.execute('''CREATE TABLE program_controls (
        scope_key TEXT NOT NULL, program_name TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at BIGINT NOT NULL, PRIMARY KEY(scope_key,program_name))''')
    op.execute('''CREATE TABLE program_quota (
        scope_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('say','wake')),
        hour BIGINT NOT NULL, used INTEGER NOT NULL CHECK(used>=0), PRIMARY KEY(scope_key,kind,hour))''')
    op.execute('''CREATE TABLE qq_official_program_actions (
        action_id TEXT PRIMARY KEY CHECK(action_id ~ '^[0-9a-f]{32}$'),
        endpoint_key TEXT NOT NULL REFERENCES qq_official_endpoints(endpoint_key), scope_key TEXT NOT NULL,
        request_id TEXT NOT NULL, program_name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('say','wake','send_file')),
        payload_json TEXT NOT NULL CHECK(octet_length(payload_json) BETWEEN 1 AND 16384),
        intent_digest TEXT NOT NULL CHECK(intent_digest ~ '^[0-9a-f]{64}$'),
        sandbox_id TEXT NOT NULL, boot_id TEXT NOT NULL, token_hash TEXT NOT NULL,
        token_generation BIGINT NOT NULL, account_epoch BIGINT NOT NULL,
        endpoint_generation BIGINT NOT NULL, route_epoch BIGINT NOT NULL,
        conversation_generation BIGINT NOT NULL CHECK(conversation_generation>=0),
        plan_id TEXT UNIQUE REFERENCES delivery_plans(plan_id),
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','queued','coalesced','completed','failed','cancelled','ambiguous')),
        error_code TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
        UNIQUE(endpoint_key,request_id), CHECK(expires_at>created_at))''')
    op.execute('CREATE INDEX program_actions_scope_time ON qq_official_program_actions(scope_key,created_at DESC,action_id)')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    for table in ('qq_official_program_actions','program_quota','program_controls'):
        if op.get_bind().exec_driver_sql('SELECT 1 FROM '+table+' LIMIT 1').first():
            raise RuntimeError('program_actions_require_export_before_downgrade')
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM conversation_settings
            WHERE NOT programs_enabled OR program_say_limit<>60 OR program_wake_limit<>6 LIMIT 1''').first():
        raise RuntimeError('program_settings_require_export_before_downgrade')
    for table in ('qq_official_program_actions','program_quota','program_controls'):
        op.drop_table(table,schema=schema)
    for column in ('programs_enabled','program_say_limit','program_wake_limit'):
        op.drop_column('conversation_settings',column,schema=schema)
