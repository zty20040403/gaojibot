"""Keep private security event IDs and payloads distinct from control receipts."""
from alembic import op

revision = '0131_merge_security_audit'
down_revision = '0130_retire_share_sources'
branch_labels = depends_on = None


def upgrade():
    op.execute('LOCK TABLE admin_security_audit, admin_audit_log IN ACCESS EXCLUSIVE MODE')
    op.execute('''ALTER TABLE admin_audit_log ADD COLUMN security_audit_id TEXT,
        ADD COLUMN security_detail TEXT, ALTER COLUMN status DROP NOT NULL, ALTER COLUMN resource_version DROP NOT NULL,
        ADD CHECK ((security_audit_id IS NULL) = (security_detail IS NULL)
            AND (security_audit_id IS NULL) = (status IS NOT NULL)
            AND (security_audit_id IS NULL) = (resource_version IS NOT NULL)
            AND (security_audit_id IS NULL OR resource_key = 'security'))''')
    op.execute('''CREATE UNIQUE INDEX admin_audit_security_id ON admin_audit_log(security_audit_id)
        WHERE security_audit_id IS NOT NULL''')
    op.execute('''INSERT INTO admin_audit_log(resource_key,security_audit_id,actor,action,target,security_detail,created_at)
        SELECT 'security',audit_id,account_id,action,target,detail,created_at FROM admin_security_audit''')
    op.drop_table('admin_security_audit', schema=op.get_context().version_table_schema)


def downgrade():
    raise RuntimeError('restore verified pre-0131 full database backup')
