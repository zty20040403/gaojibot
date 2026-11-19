"""Retire share ingestion without dropping historical content or its message links."""
revision = '0127_retire_legacy_shares'
down_revision = '0126_retire_turn_edges'
branch_labels = depends_on = None


def upgrade():
    pass


def downgrade():
    # An earlier build of this revision dropped both tables.
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore the pre-0127 full database backup')
