"""Store each history floor on its conversation."""
import os

from alembic import op

revision = '0117_conversation_history_floor'
down_revision = '0116_inline_turn_digests'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def verify_copy():
    mismatch = op.get_bind().exec_driver_sql('''SELECT 1 FROM conversations c
        LEFT JOIN conversation_visibility v USING(conversation_id)
        WHERE (c.min_canonical_message_id,c.history_cleared_at) IS DISTINCT FROM
              (v.min_canonical_message_id,v.cleared_at) LIMIT 1''').first()
    if mismatch:
        raise RuntimeError('conversation_visibility_copy_mismatch')


def upgrade():
    schema()
    op.execute('LOCK TABLE conversations,conversation_visibility IN ACCESS EXCLUSIVE MODE')
    op.execute('''ALTER TABLE conversations ADD COLUMN min_canonical_message_id BIGINT,
        ADD COLUMN history_cleared_at BIGINT,
        ADD CONSTRAINT ck_conversations_history_floor CHECK (
            min_canonical_message_id IS NOT NULL OR history_cleared_at IS NULL)''')
    op.execute('''UPDATE conversations c SET min_canonical_message_id=v.min_canonical_message_id,
        history_cleared_at=v.cleared_at FROM conversation_visibility v
        WHERE v.conversation_id=c.conversation_id''')
    verify_copy()
    op.execute('DROP TABLE conversation_visibility RESTRICT')


def downgrade():
    schema()
    op.execute('LOCK TABLE conversations IN ACCESS EXCLUSIVE MODE')
    op.execute('''CREATE TABLE conversation_visibility (
        conversation_id BIGINT PRIMARY KEY REFERENCES conversations(conversation_id),
        min_canonical_message_id BIGINT NOT NULL DEFAULT 1,
        cleared_at BIGINT)''')
    op.execute('''INSERT INTO conversation_visibility
        (conversation_id,min_canonical_message_id,cleared_at)
        SELECT conversation_id,min_canonical_message_id,history_cleared_at FROM conversations
        WHERE min_canonical_message_id IS NOT NULL''')
    verify_copy()
    op.execute('''ALTER TABLE conversations DROP CONSTRAINT ck_conversations_history_floor,
        DROP COLUMN min_canonical_message_id,
        DROP COLUMN history_cleared_at''')
