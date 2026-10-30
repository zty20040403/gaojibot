"""Allow arbitrary named files and direct file message plans; preserve old rows."""
import os
from alembic import op
import sqlalchemy as sa

revision = '0070_qq_simple_files'
down_revision = '0069_qq_simple_sender'
branch_labels = depends_on = None


def upgrade():
    schema = os.getenv('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(sa.text(f'SET LOCAL search_path TO {quoted},public'))
    op.drop_constraint('qq_official_file_artifacts_mime_check', 'qq_official_file_artifacts', schema=schema)
    op.drop_constraint('fk_qq_file_active_plan_source', 'qq_official_file_deliveries', schema=schema)


def downgrade():
    raise RuntimeError('simple_file_history_requires_export_before_downgrade')
