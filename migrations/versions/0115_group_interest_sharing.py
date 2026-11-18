"""Group interest policy and at-most-once daily reservation on existing rows."""
import os
from alembic import op

revision = '0115_group_interest_sharing'
down_revision = '0114_retire_legacy_import'
branch_labels = depends_on = None


def schema():
    value = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(value)},public')


def upgrade():
    schema()
    op.execute('ALTER TABLE conversation_settings ADD COLUMN interest_sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE')
    op.execute('''ALTER TABLE qq_official_endpoints ADD COLUMN interest_after_lull BIGINT NOT NULL DEFAULT 0
        CHECK(interest_after_lull>=0)''')
    op.execute('''ALTER TABLE qq_official_proactive_observations
        ADD COLUMN interest_day BIGINT,
        ADD COLUMN interest_candidate_json TEXT,
        ADD CONSTRAINT ck_interest_reservation CHECK
          ((interest_day IS NULL AND interest_candidate_json IS NULL) OR
           (interest_day IS NOT NULL AND interest_day>=0 AND interest_candidate_json IS NOT NULL))''')
    op.execute('''CREATE UNIQUE INDEX uq_group_interest_day ON qq_official_proactive_observations(endpoint_key,interest_day)
        WHERE interest_day IS NOT NULL''')


def downgrade():
    schema()
    op.execute('LOCK TABLE conversation_settings,qq_official_proactive_observations IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM qq_official_proactive_observations WHERE interest_day IS NOT NULL
        UNION ALL SELECT 1 FROM conversation_settings WHERE NOT interest_sharing_enabled LIMIT 1''').first():
        raise RuntimeError('group_interest_requires_export_before_downgrade')
    op.execute('DROP INDEX uq_group_interest_day')
    op.execute('ALTER TABLE qq_official_proactive_observations DROP CONSTRAINT ck_interest_reservation,DROP COLUMN interest_day,DROP COLUMN interest_candidate_json')
    op.execute('ALTER TABLE qq_official_endpoints DROP COLUMN interest_after_lull')
    op.execute('ALTER TABLE conversation_settings DROP COLUMN interest_sharing_enabled')
