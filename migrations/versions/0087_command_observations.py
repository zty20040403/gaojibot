"""Persist command elapsed/output facts and fair monitor visitation."""
import os
from alembic import op

revision = '0087_command_observations'
down_revision = '0086_background_commands'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''ALTER TABLE qq_official_background_commands
        ADD COLUMN elapsed_seconds INTEGER CHECK(elapsed_seconds>=0),
        ADD COLUMN output_bytes BIGINT NOT NULL DEFAULT 0 CHECK(output_bytes>=0),
        ADD COLUMN checked_at BIGINT NOT NULL DEFAULT 0 CHECK(checked_at>=0)''')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM qq_official_background_commands
            WHERE elapsed_seconds IS NOT NULL OR output_bytes<>0 OR checked_at<>0 LIMIT 1''').first():
        raise RuntimeError('command_observations_require_export_before_downgrade')
    for column in ('elapsed_seconds','output_bytes','checked_at'):
        op.drop_column('qq_official_background_commands',column,schema=schema)
