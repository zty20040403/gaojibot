"""Physical group rolling wake limit and explicit UTC quiet interval."""
import os
import re

from alembic import op

revision = '0112_proactive_attention'
down_revision = '0111_scope_leases_checkpoints'
branch_labels = depends_on = None


def schema():
    value = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise RuntimeError('Invalid PostgreSQL schema')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(value)},public')


def upgrade():
    schema()
    op.execute('''ALTER TABLE conversation_settings
        ADD COLUMN proactive_hourly_limit INTEGER NOT NULL DEFAULT 8 CHECK (proactive_hourly_limit BETWEEN 0 AND 60),
        ADD COLUMN proactive_quiet_utc TEXT CHECK (proactive_quiet_utc IS NULL OR
            (proactive_quiet_utc ~ '^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]-(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$'
             AND substring(proactive_quiet_utc from 1 for 5) <> substring(proactive_quiet_utc from 7 for 5)))''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM conversation_settings
        WHERE proactive_hourly_limit<>8 OR proactive_quiet_utc IS NOT NULL LIMIT 1''').first():
        raise RuntimeError('proactive_attention_requires_export_before_downgrade')
    op.execute('ALTER TABLE conversation_settings DROP COLUMN proactive_quiet_utc,DROP COLUMN proactive_hourly_limit')
