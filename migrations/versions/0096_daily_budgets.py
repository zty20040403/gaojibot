"""Physical-conversation soft budgets; disabled by default, no quota backfill."""
import os
from alembic import op

revision = '0096_daily_budgets'
down_revision = '0095_billing_source_metadata'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def upgrade():
    schema()
    op.execute('''ALTER TABLE conversation_settings
        ADD COLUMN daily_budget_usd NUMERIC(18,6) CHECK(daily_budget_usd>=0 AND daily_budget_usd<=1000000),
        ADD COLUMN budget_generation BIGINT NOT NULL DEFAULT 0 CHECK(budget_generation>=0)''')
    op.execute('ALTER TABLE qq_official_program_actions ADD COLUMN budget_generation BIGINT NOT NULL DEFAULT 0 CHECK(budget_generation>=0)')
    op.execute('''CREATE TABLE conversation_budget_days (
        scope_key TEXT NOT NULL, day_start BIGINT NOT NULL,
        endpoint_key TEXT NOT NULL REFERENCES qq_official_endpoints(endpoint_key),
        account_epoch BIGINT NOT NULL, endpoint_generation BIGINT NOT NULL, route_epoch BIGINT NOT NULL,
        first_exceeded_at BIGINT NOT NULL, budget_usd NUMERIC(18,6) NOT NULL CHECK(budget_usd>=0),
        spent_usd NUMERIC(40,12) NOT NULL CHECK(spent_usd>=0),
        last_blocked_generation BIGINT NOT NULL CHECK(last_blocked_generation>=0),
        notice_state TEXT NOT NULL DEFAULT 'pending' CHECK(notice_state IN ('pending','sending','committed','failed','unknown','cancelled')),
        notice_plan_id TEXT REFERENCES delivery_plans(plan_id),
        PRIMARY KEY(scope_key,day_start))''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM conversation_settings WHERE daily_budget_usd IS NOT NULL OR budget_generation<>0
        UNION ALL SELECT 1 FROM qq_official_program_actions WHERE budget_generation<>0
        UNION ALL SELECT 1 FROM conversation_budget_days LIMIT 1''').first():
        raise RuntimeError('daily_budget_requires_export_before_downgrade')
    op.execute('DROP TABLE conversation_budget_days')
    op.execute('ALTER TABLE qq_official_program_actions DROP COLUMN budget_generation')
    op.execute('ALTER TABLE conversation_settings DROP COLUMN daily_budget_usd,DROP COLUMN budget_generation')
