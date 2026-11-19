"""Remove historical import audit, topic links and quota associations with no consumers."""
import os

from alembic import op

revision = '0119_retire_import_residue'
down_revision = '0118_retire_semantic_state'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    for table in ('delivery_attempt_quotas', 'message_topic_edges', 'storage_migration_runs'):
        op.drop_table(table, schema=schema)


def downgrade():
    raise RuntimeError('Restore the verified pre-0119 full database backup to recover historical records')
