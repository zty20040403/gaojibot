"""Retire old context selections, preserving turn journal and trace records."""
from alembic import op

revision = '0125_retire_turn_context_plans'
down_revision = '0124_retire_approval_receipts'
branch_labels = depends_on = None


def upgrade():
    op.drop_table('turn_context_plans', schema=op.get_context().version_table_schema)


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore the pre-0125 full database backup')
