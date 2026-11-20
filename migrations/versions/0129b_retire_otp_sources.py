"""Retire the old OTP source only when no source, plan or delivery uses it."""
from alembic import op

revision = '0129b_retire_otp_sources'
down_revision = '0129_configured_owner_links'
branch_labels = depends_on = None


def upgrade():
    conn = op.get_bind()
    conn.exec_driver_sql('LOCK TABLE qq_official_admin_otp_sources, delivery_plans, deliveries IN ACCESS EXCLUSIVE MODE')
    if conn.exec_driver_sql('''SELECT EXISTS (SELECT FROM qq_official_admin_otp_sources)
        OR EXISTS (SELECT FROM delivery_plans WHERE purpose='approval_otp'
            OR approval_source_id IS NOT NULL OR approval_source_expires_at IS NOT NULL)
        OR EXISTS (SELECT FROM deliveries WHERE purpose='approval_otp')''').scalar_one():
        raise RuntimeError('preserve existing approval OTP evidence before retirement')
    op.execute('''ALTER TABLE delivery_plans DROP CONSTRAINT fk_plan_approval_source_actor,
        DROP CONSTRAINT fk_plan_approval_source_endpoint, DROP CONSTRAINT fk_plan_approval_source_expiry,
        ADD CONSTRAINT no_retired_approval_plan CHECK (purpose<>'approval_otp'
            AND approval_source_id IS NULL AND approval_source_expires_at IS NULL)''')
    op.execute("ALTER TABLE deliveries ADD CONSTRAINT no_retired_approval_delivery CHECK (purpose IS DISTINCT FROM 'approval_otp')")
    op.drop_table('qq_official_admin_otp_sources', schema=op.get_context().version_table_schema)


def downgrade():
    raise RuntimeError('retired_feature_data_requires_backup_restore: restore verified pre-0129b full database backup')
