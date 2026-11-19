"""Retire the old file manifest and usage counters, not current delivery receipts."""
from alembic import op

revision = '0123_retire_outbox_diagnostics'
down_revision = '0122_retire_security_lock'
branch_labels = depends_on = None


def upgrade():
    schema = op.get_context().version_table_schema
    op.execute(f'''LOCK TABLE "{schema}".qq_official_file_deliveries, "{schema}".qq_official_file_artifacts,
        "{schema}".qq_official_quota_periods IN ACCESS EXCLUSIVE MODE''')
    db = op.get_bind()
    if db.exec_driver_sql(f'''SELECT EXISTS (SELECT FROM "{schema}".qq_official_file_artifacts
            WHERE cleanup_pending OR bytes_state <> 'deleted') OR EXISTS (SELECT FROM "{schema}".qq_official_file_deliveries
            WHERE state NOT IN ('message_accepted','failed','cancelled','expired')) OR EXISTS (
            SELECT FROM "{schema}".qq_official_quota_periods WHERE reserved_count <> 0)''').scalar_one():
        raise RuntimeError('resolve outstanding legacy files or quota reservations before retiring diagnostics')
    for table in ('qq_official_file_deliveries', 'qq_official_file_artifacts', 'qq_official_quota_periods'):
        op.drop_table(table, schema=schema)
    for function in ('program_file_source_immutable', 'qq_artifact_extension_immutable',
                     'qq_migration_artifact_immutable', 'qq_official_file_manifest_immutable'):
        op.execute(f'DROP FUNCTION "{schema}"."{function}"() RESTRICT')


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore the verified pre-0123 full database backup')
