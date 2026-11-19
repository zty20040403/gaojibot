"""Retire the unused index checkpoint, not the searchable vectors."""
import os

from alembic import op
import sqlalchemy as sa

revision = '0118_retire_semantic_state'
down_revision = '0117_conversation_history_floor'
branch_labels = depends_on = None


def schema():
    return os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'


def upgrade():
    op.drop_table('semantic_index_state', schema=schema())


def downgrade():
    # This recreates the old cache schema; only a backup restores its contents.
    op.create_table('semantic_index_state', sa.Column('source_key', sa.Text(), primary_key=True),
        sa.Column('content_hash', sa.Text(), nullable=False), sa.Column('indexed_at', sa.BigInteger(), nullable=False),
        schema=schema())
