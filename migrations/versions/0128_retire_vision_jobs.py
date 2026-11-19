"""Retire the empty legacy vision queue; media captions have their own jobs."""
from alembic import op

revision = '0128_retire_vision_jobs'
down_revision = '0127_retire_legacy_shares'
branch_labels = depends_on = None


def upgrade():
    schema = op.get_context().version_table_schema
    table = f'"{schema}".vision_jobs'
    op.execute(f'LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().exec_driver_sql(f'SELECT EXISTS (SELECT FROM {table})').scalar_one():
        raise RuntimeError('preserve existing vision jobs before retirement')
    op.drop_table('vision_jobs', schema=schema)


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore the pre-0128 full database backup')
