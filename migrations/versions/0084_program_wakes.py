"""Bounded durable wake batches and a program-level usage association."""
import os
from alembic import op

revision = '0084_program_wakes'
down_revision = '0083_program_actions'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE qq_official_program_wakes (
        wake_id TEXT PRIMARY KEY CHECK(wake_id ~ '^[0-9a-f]{32}$'),
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL REFERENCES qq_official_endpoints(endpoint_key),
        batch_key TEXT UNIQUE, due_at BIGINT NOT NULL, funded BOOLEAN NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN
            ('queued','running','answered','completed','failed','cancelled','ambiguous')),
        run_id TEXT, input_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(octet_length(input_ids_json)<=8192),
        answer_text TEXT NOT NULL DEFAULT '' CHECK(octet_length(answer_text)<=16384),
        reply_action_id TEXT UNIQUE REFERENCES qq_official_program_actions(action_id),
        error_code TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL,
        started_at BIGINT, finished_at BIGINT)''')
    op.execute("CREATE INDEX program_wakes_due ON qq_official_program_wakes(account_key,due_at,wake_id) WHERE state='queued'")
    op.execute('ALTER TABLE qq_official_program_actions ADD COLUMN wake_id TEXT REFERENCES qq_official_program_wakes(wake_id)')
    op.execute('CREATE INDEX program_actions_wake ON qq_official_program_actions(wake_id) WHERE wake_id IS NOT NULL')
    op.execute('ALTER TABLE usage_events ADD COLUMN program_wake_id TEXT REFERENCES qq_official_program_wakes(wake_id)')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_program_wakes LIMIT 1').first():
        raise RuntimeError('program_wakes_require_export_before_downgrade')
    op.drop_column('usage_events','program_wake_id',schema=schema)
    op.drop_column('qq_official_program_actions','wake_id',schema=schema)
    op.drop_table('qq_official_program_wakes',schema=schema)
