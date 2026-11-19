"""Retire OneBot share indexes, not the messages they indexed."""
from alembic import op

revision = '0127_retire_legacy_shares'
down_revision = '0126_retire_turn_edges'
branch_labels = depends_on = None


def upgrade():
    schema = op.get_context().version_table_schema
    op.drop_table('message_sources', schema=schema)
    op.drop_table('content_sources', schema=schema)


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore the pre-0127 full database backup')
