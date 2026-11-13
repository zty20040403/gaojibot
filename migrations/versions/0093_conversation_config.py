"""Conversation-local proactive switch and a non-replay generation."""
import os
from alembic import op

revision = '0093_conversation_config'
down_revision = '0092_caption_scopes'
branch_labels = depends_on = None


def schema():
    value = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(value)},public')


def upgrade():
    schema()
    op.execute('''ALTER TABLE conversation_settings ADD COLUMN proactive_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        ADD COLUMN proactive_generation BIGINT NOT NULL DEFAULT 0 CHECK(proactive_generation>=0)''')
    for table in ('qq_official_proactive_observations','qq_official_inbound_events'):
        op.execute('ALTER TABLE '+table+' ADD COLUMN proactive_generation BIGINT NOT NULL DEFAULT 0 CHECK(proactive_generation>=0)')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM conversation_settings WHERE NOT proactive_enabled OR proactive_generation<>0
        UNION ALL SELECT 1 FROM qq_official_proactive_observations WHERE proactive_generation<>0
        UNION ALL SELECT 1 FROM qq_official_inbound_events WHERE proactive_generation<>0 LIMIT 1''').first():
        raise RuntimeError('conversation_config_requires_export_before_downgrade')
    op.execute('ALTER TABLE qq_official_proactive_observations DROP COLUMN proactive_generation')
    op.execute('ALTER TABLE qq_official_inbound_events DROP COLUMN proactive_generation')
    op.execute('ALTER TABLE conversation_settings DROP COLUMN proactive_enabled,DROP COLUMN proactive_generation')
