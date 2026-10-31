"""Simple background agents on the existing durable queue."""
import os
from alembic import op
import sqlalchemy as sa

revision = '0074_background_agents'
down_revision = '0073_window_memory_requests'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(sa.text(f'SET LOCAL search_path TO {quoted},public'))
    op.execute('''CREATE TABLE background_jobs (
        job_id BIGINT PRIMARY KEY REFERENCES durable_jobs(job_id),
        account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
        scope_key TEXT NOT NULL, source_execution_id TEXT,
        canonical_message_id BIGINT NOT NULL REFERENCES messages(canonical_message_id),
        native_message_id TEXT NOT NULL, endpoint_generation BIGINT NOT NULL, route_epoch BIGINT NOT NULL,
        task TEXT NOT NULL, title TEXT NOT NULL, profile TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed','failed','cancelled')),
        steering_json TEXT NOT NULL DEFAULT '[]', last_tool TEXT NOT NULL DEFAULT '',
        report TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL, started_at BIGINT, finished_at BIGINT,
        deadline_at BIGINT NOT NULL, delivered_at BIGINT
    )''')
    op.execute('CREATE INDEX background_jobs_endpoint ON background_jobs(endpoint_key, actor_key, created_at)')


def downgrade():
    op.drop_table('background_jobs', schema=os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot'))
