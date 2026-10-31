"""Reminders and routines use the same simple schedule table."""
import os
from alembic import op

revision = '0076_schedules'
down_revision = '0075_background_results'
branch_labels = depends_on = None


def upgrade():
    quoted = op.get_bind().dialect.identifier_preparer.quote(os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot'))
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE schedules (
        id BIGSERIAL PRIMARY KEY, scope_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('reminder','routine')), text TEXT NOT NULL,
        next_at BIGINT, cron TEXT, created_at BIGINT NOT NULL, cancelled_at BIGINT,
        source_json TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT ''
    )''')
    op.execute('CREATE INDEX schedules_due ON schedules(next_at) WHERE cancelled_at IS NULL')


def downgrade():
    op.drop_table('schedules', schema=os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot'))
