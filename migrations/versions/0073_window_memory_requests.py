"""Conversation window start, the per-conversation memory file, and saved model requests."""

import os

from alembic import op
import sqlalchemy as sa

revision = '0073_window_memory_requests'
down_revision = '0072_qq_simple_binding'
branch_labels = depends_on = None


def _schema():
    return os.getenv('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'


def upgrade():
    schema = _schema()
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(sa.text(f'SET LOCAL search_path TO {quoted},public'))
    # The chat tail starts after this message; it only moves on overflow or /clear,
    # so the prompt prefix stays byte-identical between moves.
    op.create_table(
        'conversation_windows',
        sa.Column('scope_key', sa.Text(), primary_key=True),
        sa.Column('tail_after', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        schema=schema,
    )
    op.create_table(
        'conversation_memory',
        sa.Column('scope_key', sa.Text(), primary_key=True),
        sa.Column('content', sa.Text(), nullable=False, server_default=''),
        sa.Column('previous_content', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('tidied_at', sa.BigInteger(), nullable=True),
        schema=schema,
    )
    op.create_table(
        'turn_requests',
        sa.Column('request_id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('turn_id', sa.BigInteger(), nullable=True),
        sa.Column('scope_key', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=False),
        sa.Column('messages_json', sa.Text(), nullable=False),
        sa.Column('tools_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        schema=schema,
    )
    op.create_index('ix_turn_requests_turn', 'turn_requests', ['turn_id'], schema=schema)
    op.create_index('ix_turn_requests_created', 'turn_requests', ['created_at'], schema=schema)


def downgrade():
    schema = _schema()
    op.drop_table('turn_requests', schema=schema)
    op.drop_table('conversation_memory', schema=schema)
    op.drop_table('conversation_windows', schema=schema)
