"""Freeze a once-only budget notice's original stop and budget authorization."""
import os
from alembic import op

revision = '0097_budget_notices'
down_revision = '0096_daily_budgets'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def upgrade():
    schema()
    op.execute('''ALTER TABLE conversation_budget_days
        ADD COLUMN conversation_generation BIGINT CHECK(conversation_generation>=0),
        ADD COLUMN notice_budget_generation BIGINT CHECK(notice_budget_generation>=0)''')
    # 0096 had no sender and captured neither authorization. Do not guess a
    # fresh generation: it could replay a crossing from before stop or restore.
    op.execute("UPDATE conversation_budget_days SET notice_state='cancelled' WHERE notice_state IN ('pending','sending')")
    op.execute('''CREATE UNIQUE INDEX uq_budget_notice_plan ON conversation_budget_days(notice_plan_id)
        WHERE notice_plan_id IS NOT NULL''')
    op.execute('''CREATE FUNCTION protect_budget_notice_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.scope_key,NEW.day_start,NEW.endpoint_key,NEW.account_epoch,NEW.endpoint_generation,
                NEW.route_epoch,NEW.first_exceeded_at,NEW.budget_usd,NEW.spent_usd,
                NEW.conversation_generation,NEW.notice_budget_generation)
                IS DISTINCT FROM ROW(OLD.scope_key,OLD.day_start,OLD.endpoint_key,OLD.account_epoch,OLD.endpoint_generation,
                OLD.route_epoch,OLD.first_exceeded_at,OLD.budget_usd,OLD.spent_usd,
                OLD.conversation_generation,OLD.notice_budget_generation)
                OR (OLD.notice_plan_id IS NOT NULL AND NEW.notice_plan_id IS DISTINCT FROM OLD.notice_plan_id) THEN
                RAISE EXCEPTION 'immutable_budget_notice_source';
            END IF;
            RETURN NEW;
        END $$''')
    op.execute('''CREATE TRIGGER protect_budget_notice_source BEFORE UPDATE ON conversation_budget_days
        FOR EACH ROW EXECUTE FUNCTION protect_budget_notice_source()''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM conversation_budget_days WHERE conversation_generation IS NOT NULL
        OR notice_budget_generation IS NOT NULL OR notice_plan_id IS NOT NULL LIMIT 1''').first():
        raise RuntimeError('budget_notice_requires_export_before_downgrade')
    op.execute('DROP TRIGGER protect_budget_notice_source ON conversation_budget_days')
    op.execute('DROP FUNCTION protect_budget_notice_source()')
    op.execute('DROP INDEX uq_budget_notice_plan')
    op.execute('ALTER TABLE conversation_budget_days DROP COLUMN conversation_generation,DROP COLUMN notice_budget_generation')
