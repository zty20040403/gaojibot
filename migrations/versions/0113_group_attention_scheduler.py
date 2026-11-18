"""Durable bounded attention cursor on each physical QQ group."""
import os
from alembic import op

revision = '0113_group_attention_scheduler'
down_revision = '0112_proactive_attention'
branch_labels = depends_on = None


def schema():
    value = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(value)},public')


def upgrade():
    schema()
    op.execute('''ALTER TABLE qq_official_endpoints
        ADD COLUMN attention_cursor BIGINT NOT NULL DEFAULT 0 CHECK(attention_cursor>=0),
        ADD COLUMN attention_end BIGINT NOT NULL DEFAULT 0 CHECK(attention_end>=attention_cursor),
        ADD COLUMN attention_next_at BIGINT,
        ADD COLUMN attention_activity_at BIGINT,
        ADD COLUMN attention_suppressed_through BIGINT NOT NULL DEFAULT 0 CHECK(attention_suppressed_through>=0),
        ADD CONSTRAINT ck_group_attention_pending CHECK
          ((attention_end=attention_cursor) OR
           (kind='group' AND attention_end>attention_cursor AND attention_next_at IS NOT NULL))''')
    op.execute('''CREATE INDEX ix_qq_attention_due ON qq_official_endpoints(account_key,attention_next_at)
        WHERE attention_end>attention_cursor''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_endpoints WHERE attention_end>attention_cursor LIMIT 1').first():
        raise RuntimeError('pending_group_attention_requires_drain')
    op.execute('DROP INDEX ix_qq_attention_due')
    op.execute('ALTER TABLE qq_official_endpoints DROP CONSTRAINT ck_group_attention_pending')
    for name in ('attention_suppressed_through','attention_activity_at','attention_next_at','attention_end','attention_cursor'):
        op.execute(f'ALTER TABLE qq_official_endpoints DROP COLUMN {name}')
