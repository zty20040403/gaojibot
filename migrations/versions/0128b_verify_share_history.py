"""Reject databases upgraded by the destructive version of 0127."""
from alembic import op
from sqlalchemy import inspect

revision = '0128b_verify_share_history'
down_revision = '0128_retire_vision_jobs'
branch_labels = depends_on = None


def upgrade():
    tables = inspect(op.get_bind()).get_table_names(schema=op.get_context().version_table_schema)
    if not {'content_sources', 'message_sources'}.issubset(tables):
        raise RuntimeError('share history missing: recover the original tables from the pre-0127 backup')


def downgrade():
    pass
