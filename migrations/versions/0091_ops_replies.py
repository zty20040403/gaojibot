"""Authenticated private consent receipts, without persisting reply text/codes."""
import os
from alembic import op

revision = '0091_ops_replies'
down_revision = '0090_ops_notices'
branch_labels = depends_on = None


def schema():
    value = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(value)},public')


def upgrade():
    schema()
    op.execute('ALTER TABLE ops_actions ADD CONSTRAINT ops_reply_identity UNIQUE(action_id,endpoint_key,actor_key)')
    op.execute('''ALTER TABLE qq_official_inbound_events
        ADD COLUMN ops_reply_action_id TEXT,
        ADD COLUMN ops_reply_actor_key TEXT CHECK(octet_length(ops_reply_actor_key) BETWEEN 1 AND 1280),
        ADD COLUMN ops_reply_commitment TEXT CHECK(ops_reply_commitment ~ '^[a-f0-9]{64}$'),
        ADD COLUMN ops_reply_occurred_at BIGINT,
        ADD CONSTRAINT ops_reply_source FOREIGN KEY(ops_reply_action_id,endpoint_key,ops_reply_actor_key)
            REFERENCES ops_actions(action_id,endpoint_key,actor_key),
        ADD CONSTRAINT ops_reply_capture CHECK(
            (ops_reply_action_id IS NULL AND ops_reply_actor_key IS NULL AND ops_reply_commitment IS NULL AND ops_reply_occurred_at IS NULL)
            OR (ops_reply_action_id IS NOT NULL AND ops_reply_actor_key IS NOT NULL AND ops_reply_commitment IS NOT NULL
                AND endpoint_key IS NOT NULL AND native_message_id IS NOT NULL
                AND event_type='C2C_MESSAGE_CREATE' AND echo_state='not_echo')),
        ADD CONSTRAINT ops_reply_receipt_identity UNIQUE(inbox_id,ops_reply_action_id,ops_reply_actor_key)''')
    op.execute('''CREATE TABLE ops_replies (
        inbox_id TEXT PRIMARY KEY, action_id TEXT NOT NULL, account_key TEXT NOT NULL,
        endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL, source_session_id TEXT NOT NULL, source_seq BIGINT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('approved','denied','expired','cancelled')), created_at BIGINT NOT NULL,
        UNIQUE(inbox_id,action_id,actor_key),
        FOREIGN KEY(inbox_id,account_key,endpoint_key) REFERENCES qq_official_inbound_events(inbox_id,account_key,endpoint_key),
        FOREIGN KEY(inbox_id,action_id,actor_key) REFERENCES qq_official_inbound_events(inbox_id,ops_reply_action_id,ops_reply_actor_key),
        FOREIGN KEY(account_key,source_session_id,source_seq,inbox_id)
            REFERENCES qq_official_gateway_receipts(account_key,session_id,seq,inbox_id))''')
    op.execute('''ALTER TABLE ops_actions ADD CONSTRAINT ops_approved_reply
        FOREIGN KEY(approved_inbox_id,action_id,approved_actor_key) REFERENCES ops_replies(inbox_id,action_id,actor_key)''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_inbound_events WHERE ops_reply_action_id IS NOT NULL LIMIT 1').first():
        raise RuntimeError('ops_replies_require_export_before_downgrade')
    op.execute('ALTER TABLE ops_actions DROP CONSTRAINT ops_approved_reply')
    op.drop_table('ops_replies')
    op.execute('''ALTER TABLE qq_official_inbound_events DROP COLUMN ops_reply_action_id,
        DROP COLUMN ops_reply_actor_key,DROP COLUMN ops_reply_commitment,DROP COLUMN ops_reply_occurred_at''')
    op.execute('ALTER TABLE ops_actions DROP CONSTRAINT ops_reply_identity')
