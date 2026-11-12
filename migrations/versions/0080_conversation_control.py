"""Persistent physical-conversation stop fences and once-only control receipts."""
import os
from alembic import op

revision = '0080_conversation_control'
down_revision = '0079_video_notes'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('ALTER TABLE conversation_settings ADD COLUMN stopped BOOLEAN NOT NULL DEFAULT FALSE')
    op.execute('ALTER TABLE conversation_settings ADD COLUMN stop_generation BIGINT NOT NULL DEFAULT 0 CHECK(stop_generation>=0)')
    op.execute('ALTER TABLE qq_official_inbound_events ADD COLUMN conversation_generation BIGINT NOT NULL DEFAULT 0')
    op.execute('ALTER TABLE qq_official_inbound_events ADD COLUMN conversation_stopped BOOLEAN NOT NULL DEFAULT FALSE')
    op.execute('ALTER TABLE qq_official_inbound_events ADD COLUMN conversation_control JSONB')
    op.execute('ALTER TABLE delivery_plans ADD COLUMN conversation_generation BIGINT NOT NULL DEFAULT 0')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM conversation_settings WHERE stop_generation>0 LIMIT 1').first():
        raise RuntimeError('conversation_control_requires_export_before_downgrade')
    for table,columns in (
        ('delivery_plans',('conversation_generation',)),
        ('qq_official_inbound_events',('conversation_generation','conversation_stopped','conversation_control')),
        ('conversation_settings',('stopped','stop_generation'))):
        for column in columns:
            op.drop_column(table,column,schema=schema)
