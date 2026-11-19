"""Anchor owner links in configuration and hold one account-scoped code."""
from alembic import op

revision = '0129_configured_owner_links'
down_revision = '0128_retire_vision_jobs'
branch_labels = depends_on = None


def upgrade():
    schema = op.get_context().version_table_schema
    op.execute(f'''ALTER TABLE "{schema}".qq_official_inbound_events
        DROP CONSTRAINT qq_official_inbound_events_control_action_check,
        DROP CONSTRAINT qq_official_inbound_events_control_decision_code_check,
        ADD CONSTRAINT qq_official_inbound_events_control_action_check CHECK (control_action IN
          ('whoami','create','submit','status','confirm','reject','cancel','invalid','owner_entry','retired')),
        ADD CONSTRAINT qq_official_inbound_events_control_decision_code_check CHECK (control_decision_code IN
          ('already_confirmed','cancelled','candidate','confirmed','conflict','denied','expired','identity','invalid',
           'issued','limited','locked','pending','rejected','sending','stale','syntax','unavailable','retired',
           'owner_code_issued','guide'))''')
    op.execute(f'''ALTER TABLE "{schema}".qq_official_accounts
        ADD COLUMN owner_link_actor_key TEXT,
        ADD COLUMN owner_link_code_hmac TEXT,
        ADD COLUMN owner_link_expires_at BIGINT,
        ADD COLUMN owner_link_epoch BIGINT,
        ADD CONSTRAINT ck_qq_official_owner_link_code CHECK (
          (owner_link_actor_key IS NULL AND owner_link_code_hmac IS NULL
           AND owner_link_expires_at IS NULL AND owner_link_epoch IS NULL)
          OR (owner_link_actor_key IS NOT NULL AND owner_link_code_hmac IS NOT NULL
              AND owner_link_expires_at IS NOT NULL AND owner_link_epoch IS NOT NULL
              AND octet_length(owner_link_actor_key) BETWEEN 1 AND 1280
              AND owner_link_code_hmac ~ '^[a-f0-9]{{64}}$'
              AND owner_link_expires_at >= 0 AND owner_link_epoch >= 1))''')
    op.execute(f'''ALTER TABLE "{schema}".qq_official_owner_actor_links
        ADD COLUMN configured_owner_actor_key TEXT,
        ALTER COLUMN owner_grant_id DROP NOT NULL,
        ALTER COLUMN owner_revision DROP NOT NULL,
        ALTER COLUMN candidate_id DROP NOT NULL,
        ADD CONSTRAINT ck_qq_official_owner_link_anchor CHECK (
          (configured_owner_actor_key IS NULL AND owner_grant_id IS NOT NULL
           AND owner_revision IS NOT NULL AND candidate_id IS NOT NULL)
          OR (configured_owner_actor_key IS NOT NULL
              AND octet_length(configured_owner_actor_key) BETWEEN 1 AND 1280
              AND owner_grant_id IS NULL AND owner_revision IS NULL AND candidate_id IS NULL))''')


def downgrade():
    schema = op.get_context().version_table_schema
    db = op.get_bind()
    if db.exec_driver_sql(f'''SELECT EXISTS (SELECT FROM "{schema}".qq_official_accounts
            WHERE owner_link_code_hmac IS NOT NULL) OR EXISTS (
            SELECT FROM "{schema}".qq_official_owner_actor_links
            WHERE configured_owner_actor_key IS NOT NULL) OR EXISTS (
            SELECT FROM "{schema}".qq_official_inbound_events
            WHERE control_action IN ('owner_entry','retired') OR control_decision_code IN
                  ('owner_code_issued','guide','retired','unavailable'))''').scalar_one():
        raise RuntimeError('configured owner links require backup restore before downgrade')
    op.execute(f'''ALTER TABLE "{schema}".qq_official_owner_actor_links
        DROP CONSTRAINT ck_qq_official_owner_link_anchor,
        ALTER COLUMN owner_grant_id SET NOT NULL,
        ALTER COLUMN owner_revision SET NOT NULL,
        ALTER COLUMN candidate_id SET NOT NULL,
        DROP COLUMN configured_owner_actor_key''')
    op.execute(f'''ALTER TABLE "{schema}".qq_official_accounts
        DROP CONSTRAINT ck_qq_official_owner_link_code,
        DROP COLUMN owner_link_actor_key,
        DROP COLUMN owner_link_code_hmac,
        DROP COLUMN owner_link_expires_at,
        DROP COLUMN owner_link_epoch''')
    op.execute(f'''ALTER TABLE "{schema}".qq_official_inbound_events
        DROP CONSTRAINT qq_official_inbound_events_control_action_check,
        DROP CONSTRAINT qq_official_inbound_events_control_decision_code_check,
        ADD CONSTRAINT qq_official_inbound_events_control_action_check CHECK (control_action IN
          ('whoami','create','submit','status','confirm','reject','cancel','invalid')),
        ADD CONSTRAINT qq_official_inbound_events_control_decision_code_check CHECK (control_decision_code IN
          ('already_confirmed','cancelled','candidate','confirmed','conflict','denied','expired','identity','invalid',
           'issued','limited','locked','pending','rejected','sending','stale','syntax'))''')
