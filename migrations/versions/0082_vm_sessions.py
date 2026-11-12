"""Boot-scoped conversation VM credentials; only token hashes reach PostgreSQL."""
import os
from alembic import op

revision = '0082_vm_sessions'
down_revision = '0081_host_api_calls'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE qq_official_vm_sessions (
        endpoint_key TEXT PRIMARY KEY REFERENCES qq_official_endpoints(endpoint_key) ON DELETE CASCADE,
        sandbox_id TEXT NOT NULL UNIQUE, boot_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE CHECK(token_hash ~ '^[0-9a-f]{64}$'),
        token_generation BIGINT NOT NULL CHECK(token_generation>0),
        account_epoch BIGINT NOT NULL, endpoint_generation BIGINT NOT NULL, route_epoch BIGINT NOT NULL,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, revoked_at BIGINT)''')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_vm_sessions LIMIT 1').first():
        raise RuntimeError('vm_sessions_require_export_before_downgrade')
    op.drop_table('qq_official_vm_sessions',schema=schema)
