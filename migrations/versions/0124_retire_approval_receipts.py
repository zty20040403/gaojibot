"""Retire the unused approval command receipts without discarding audit evidence."""
from alembic import op

revision = '0124_retire_approval_receipts'
down_revision = '0123_retire_outbox_diagnostics'
branch_labels = depends_on = None


def upgrade():
    schema = op.get_context().version_table_schema
    table = 'qq_official_admin_control_receipts'
    op.execute(f'LOCK TABLE "{schema}".{table} IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().exec_driver_sql(f'SELECT EXISTS (SELECT FROM "{schema}".{table})').scalar_one():
        raise RuntimeError('preserve existing approval control receipts before retirement')
    op.drop_table(table, schema=schema)


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore verified pre-0124 full database backup')
