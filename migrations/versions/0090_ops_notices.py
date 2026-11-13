"""Separate ephemeral recipe consent notices, not global admin OTP approval."""
import importlib
import os
from alembic import op

revision = '0090_ops_notices'
down_revision = '0089_ops_actions'
branch_labels = depends_on = None

# Reuse only these four historical constraint definitions, never the legacy DDL
# or its upgrade/downgrade functions. Their exact non-ops invariants are retained.
NAMES = {'delivery_plans_purpose_check','ck_deliveries_official_fields','ck_deliveries_ephemeral','delivery_plans_check2'}
BASE = tuple(sql for sql in importlib.import_module('migrations.versions.0066_qq_official_migration').UPGRADE
    if any(sql.startswith('ALTER TABLE '+table+' ADD CONSTRAINT '+name+' ') for table in ('delivery_plans','deliveries') for name in NAMES))


def schema():
    value = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(value)
    op.execute(f'SET LOCAL search_path TO {quoted},public')


def constraints(extended):
    if len(BASE)!=4:
        raise RuntimeError('ops_notice_constraint_baseline_missing')
    for original in BASE:
        prefix,expression = original.split(' CHECK',1)
        _,_,table,_,_,name = prefix.split()
        if extended:
            if name=='delivery_plans_check2':
                expression = expression.replace("purpose NOT IN ('binding_otp','approval_otp')", "purpose NOT IN ('binding_otp','approval_otp','ops_approval')")
                expression = '('+expression+''' OR (purpose='ops_approval' AND payload_mode='ephemeral'
                    AND digest_scheme='hmac_sha256_v1' AND digest_key_id IS NOT NULL AND part_count=1
                    AND challenge_id IS NULL AND challenge_expires_at IS NULL
                    AND approval_source_id IS NULL AND approval_source_expires_at IS NULL
                    AND ops_action_id IS NOT NULL AND manifest_json='{"v":1,"ephemeral":true}'))'''
            else:
                expression = expression.replace("'approval_otp'","'approval_otp','ops_approval'")
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT {name}')
        op.get_bind().exec_driver_sql(prefix+' CHECK '+expression)


def upgrade():
    schema()
    op.execute('ALTER TABLE delivery_plans ADD COLUMN ops_action_id TEXT UNIQUE REFERENCES ops_actions(action_id)')
    op.execute("ALTER TABLE delivery_plans ADD CONSTRAINT ops_notice_kind CHECK((purpose='ops_approval')=(ops_action_id IS NOT NULL))")
    op.execute('ALTER TABLE delivery_plans ADD CONSTRAINT ops_notice_identity UNIQUE(plan_id,ops_action_id)')
    op.execute('''ALTER TABLE ops_actions ADD CONSTRAINT ops_notice_source FOREIGN KEY(notice_plan_id,action_id)
        REFERENCES delivery_plans(plan_id,ops_action_id)''')
    constraints(True)


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql("SELECT 1 FROM delivery_plans WHERE purpose='ops_approval' LIMIT 1").first():
        raise RuntimeError('ops_notices_require_export_before_downgrade')
    constraints(False)
    op.execute('ALTER TABLE ops_actions DROP CONSTRAINT ops_notice_source')
    op.execute('ALTER TABLE delivery_plans DROP COLUMN ops_action_id')
