"""New agents snapshot a physical-conversation duration; existing deadlines stay."""
import os
from alembic import op

revision = '0088_agent_time_limits'
down_revision = '0087_command_observations'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''ALTER TABLE conversation_settings ADD COLUMN agent_timeout_hours INTEGER NOT NULL DEFAULT 6
        CHECK(agent_timeout_hours BETWEEN 1 AND 6)''')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM conversation_settings WHERE agent_timeout_hours<>6 LIMIT 1').first():
        raise RuntimeError('agent_time_limits_require_export_before_downgrade')
    op.drop_column('conversation_settings','agent_timeout_hours',schema=schema)
