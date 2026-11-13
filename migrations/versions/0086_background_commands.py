"""VM-owned one-shot commands and a kill fence distinct from conversation stop."""
import os
from alembic import op

revision = '0086_background_commands'
down_revision = '0085_program_files'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''ALTER TABLE conversation_settings ADD COLUMN command_generation BIGINT NOT NULL DEFAULT 0
        CHECK(command_generation>=0)''')
    op.execute('''CREATE TABLE qq_official_background_commands (
        command_id TEXT PRIMARY KEY CHECK(command_id ~ '^[0-9a-f]{32}$'),
        endpoint_key TEXT NOT NULL REFERENCES qq_official_endpoints(endpoint_key),scope_key TEXT NOT NULL,
        request_id TEXT NOT NULL,program_name TEXT NOT NULL,
        argv_json TEXT NOT NULL CHECK(octet_length(argv_json) BETWEEN 1 AND 16384),
        intent_digest TEXT NOT NULL CHECK(intent_digest ~ '^[0-9a-f]{64}$'),
        sandbox_id TEXT NOT NULL,boot_id TEXT NOT NULL,token_hash TEXT NOT NULL,
        token_generation BIGINT NOT NULL,account_epoch BIGINT NOT NULL,
        endpoint_generation BIGINT NOT NULL,route_epoch BIGINT NOT NULL,
        conversation_generation BIGINT NOT NULL CHECK(conversation_generation>=0),
        command_generation BIGINT NOT NULL CHECK(command_generation>=0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','starting','running','stopping','completed','failed','cancelled','ambiguous')),
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        created_at BIGINT NOT NULL,deadline_at BIGINT NOT NULL,started_at BIGINT,finished_at BIGINT,
        exit_code INTEGER,output_head TEXT NOT NULL DEFAULT '',output_tail TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',notification_error TEXT NOT NULL DEFAULT '',
        notification_action_id TEXT UNIQUE REFERENCES qq_official_program_actions(action_id),
        UNIQUE(endpoint_key,request_id),CHECK(deadline_at>created_at AND deadline_at<=created_at+21600))''')
    op.execute('''CREATE INDEX background_commands_active ON qq_official_background_commands(endpoint_key,created_at)
        WHERE finished_at IS NULL''')
    op.execute('''ALTER TABLE qq_official_program_actions ADD COLUMN source_command_id TEXT UNIQUE
        REFERENCES qq_official_background_commands(command_id),
        ADD CONSTRAINT program_command_wake_only CHECK(source_command_id IS NULL OR kind='wake')''')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_background_commands LIMIT 1').first():
        raise RuntimeError('background_commands_require_export_before_downgrade')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM conversation_settings WHERE command_generation<>0 LIMIT 1').first():
        raise RuntimeError('command_generations_require_export_before_downgrade')
    op.drop_column('qq_official_program_actions','source_command_id',schema=schema)
    op.drop_table('qq_official_background_commands',schema=schema)
    op.drop_column('conversation_settings','command_generation',schema=schema)
