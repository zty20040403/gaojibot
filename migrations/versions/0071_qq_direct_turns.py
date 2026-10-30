"""Ordinary turns no longer require durable jobs or duplicate result payloads."""
import os
from alembic import op
import sqlalchemy as sa

revision = '0071_qq_direct_turns'
down_revision = '0070_qq_simple_files'
branch_labels = depends_on = None


def upgrade():
    schema = os.getenv('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(sa.text(f'SET LOCAL search_path TO {quoted},public'))
    for table, column in (('qq_official_turn_executions', 'job_id'),
                          ('qq_official_file_artifacts', 'source_job_id'),
                          ('qq_official_proactive_observations', 'ingest_job_id')):
        op.alter_column(table, column, nullable=True, schema=schema)
    for suffix in ('', '1', '2', '7'):
        op.drop_constraint('qq_official_turn_executions_check' + suffix,
                           'qq_official_turn_executions', schema=schema)
    op.drop_constraint('qq_official_inbound_dedup_check', 'qq_official_inbound_dedup', schema=schema)


def downgrade():
    raise RuntimeError('direct_turn_history_requires_export_before_downgrade')
