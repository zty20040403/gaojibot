"""Persistent per-physical-conversation API reservations and credential-free audit."""
import os
from alembic import op

revision = '0081_host_api_calls'
down_revision = '0080_conversation_control'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE host_api_quota (
        scope_key TEXT NOT NULL, api_name TEXT NOT NULL, hour BIGINT NOT NULL,
        used BIGINT NOT NULL CHECK(used>=0), PRIMARY KEY(scope_key,api_name,hour))''')
    op.execute('''CREATE TABLE host_api_calls (
        call_id BIGSERIAL PRIMARY KEY, scope_key TEXT NOT NULL, api_name TEXT NOT NULL,
        caller_ref TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
        status_code INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL, finished_at BIGINT,
        CHECK(outcome IN ('pending','completed','failed','denied','interrupted')))''')
    op.execute('CREATE INDEX host_api_calls_scope_time ON host_api_calls(scope_key,created_at DESC)')
    op.execute('CREATE INDEX host_api_calls_time ON host_api_calls(created_at)')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM host_api_calls LIMIT 1').first():
        raise RuntimeError('host_api_calls_require_export_before_downgrade')
    op.drop_table('host_api_calls',schema=schema)
    op.drop_table('host_api_quota',schema=schema)
