"""Retire approval state without discarding outstanding notices or delivery evidence."""
from alembic import op

revision = '0132_retire_approval_tables'
down_revision = '0131_merge_security_audit'
branch_labels = depends_on = None


def upgrade():
    op.execute('''LOCK TABLE admin_approvals, admin_approval_codes, admin_approval_references IN ACCESS EXCLUSIVE MODE''')
    db = op.get_bind()
    if db.exec_driver_sql("""SELECT 1 FROM admin_approvals WHERE status IN
            ('sending','pending','awaiting_quorum','queued','executing') OR
            (notice_sent=0 AND (status IN ('failed','needs_attention') OR
             (status='succeeded' AND identity_transport<>'onebot'))) LIMIT 1""").first():
        raise RuntimeError('approval retirement requires review: active approval or unsent receipt')
    # OneBot has no delivery route. Record the outcome without claiming the notice was sent.
    db.exec_driver_sql('''INSERT INTO admin_audit_log(resource_key,security_audit_id,actor,action,target,security_detail,created_at)
        SELECT 'security','retired-onebot-notice:' || approval_id,account_id,'approval.notice.retired_unsent',
            approval_id,'{"transport":"onebot","status":"succeeded","notice_sent":false}',updated_at
        FROM admin_approvals WHERE identity_transport='onebot' AND status='succeeded' AND notice_sent=0''')
    op.execute('ALTER TABLE delivery_plans DROP COLUMN approval_source_id, DROP COLUMN approval_source_expires_at')
    db.exec_driver_sql('''ALTER TABLE delivery_plans ADD CONSTRAINT delivery_plans_check2 CHECK (
        (purpose='binding_otp' AND payload_mode='ephemeral' AND digest_scheme='hmac_sha256_v1'
            AND digest_key_id IS NOT NULL AND part_count=1 AND challenge_id IS NOT NULL
            AND challenge_expires_at IS NOT NULL AND expires_at<=challenge_expires_at)
        OR (purpose IN ('final','requested_artifact','background_result','progress','unsolicited')
            AND payload_mode IN ('persistent','protected') AND digest_scheme='sha256'
            AND digest_key_id IS NULL AND challenge_id IS NULL AND challenge_expires_at IS NULL)
        OR (purpose IN ('ops_approval','link_otp','link_feedback','question_notice')
            AND payload_mode='ephemeral' AND digest_scheme='hmac_sha256_v1'
            AND digest_key_id IS NOT NULL AND part_count=1 AND challenge_id IS NULL
            AND challenge_expires_at IS NULL AND manifest_json='{"v":1,"ephemeral":true}'))''')
    op.execute('DROP TABLE admin_approval_codes, admin_approval_references, admin_approvals')


def downgrade():
    raise RuntimeError('approval_retirement_requires_backup_restore')
