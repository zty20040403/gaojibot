"""Freeze new attribution; do not guess historical people/programs from today."""
import os
from alembic import op

revision = '0095_billing_source_metadata'
down_revision = '0094_model_usage_calls'
branch_labels = depends_on = None


def _schema():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    return schema


def _immutable(*,metadata):
    columns = ('call_id','endpoint_key','scope_key','actor_key','purpose','turn_id','background_job_id',
        'program_wake_id','provider','model','profile','pricing_json','started_at')+ (('source_metadata_json',) if metadata else ())
    new,old = (','.join(prefix+'.'+column for column in columns) for prefix in ('NEW','OLD'))
    op.execute(f'''CREATE OR REPLACE FUNCTION immutable_model_usage_call() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW({new}) IS DISTINCT FROM ROW({old}) OR OLD.finished_at IS NOT NULL THEN
                RAISE EXCEPTION 'immutable_model_usage_call';
            END IF;
            RETURN NEW;
        END $$''')


def upgrade():
    _schema()
    op.execute('''ALTER TABLE model_usage_calls ADD COLUMN source_metadata_json TEXT
        CHECK(octet_length(source_metadata_json)<=16384 AND jsonb_typeof(source_metadata_json::jsonb)='object')''')
    # Leave old calls NULL, even when the present-day source is still available.
    _immutable(metadata=True)


def downgrade():
    schema = _schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM model_usage_calls WHERE source_metadata_json IS NOT NULL LIMIT 1').first():
        raise RuntimeError('billing_source_metadata_requires_export_before_downgrade')
    _immutable(metadata=False)
    op.drop_column('model_usage_calls','source_metadata_json',schema=schema)
