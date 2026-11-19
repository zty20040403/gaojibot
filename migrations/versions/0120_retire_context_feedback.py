"""Remove the unused legacy context feedback store, preserving plans and traces."""
import os

from alembic import op

revision = '0120_retire_context_feedback'
down_revision = '0119_retire_import_residue'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    op.drop_table('turn_context_feedback', schema=schema)


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore the verified pre-0120 full database backup')
