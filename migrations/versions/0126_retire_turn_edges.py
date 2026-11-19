"""Retire the write-only turn fork edges; reply lookup uses send links."""
from alembic import op

revision = '0126_retire_turn_edges'
down_revision = '0125_retire_turn_context_plans'
branch_labels = depends_on = None


def upgrade():
    op.drop_table('turn_edges', schema=op.get_context().version_table_schema)


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore the pre-0126 full database backup')
