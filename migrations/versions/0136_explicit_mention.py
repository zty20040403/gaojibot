"""Keep QQ @ evidence separate from the inbox dispatch decision."""
from alembic import op

revision = '0136_explicit_mention'
down_revision = '0135_retire_grant_anchors'
branch_labels = depends_on = None


def upgrade():
    op.execute('ALTER TABLE qq_official_inbound_events ADD COLUMN explicit_mention BOOLEAN NOT NULL DEFAULT FALSE')
    # Backfill where the original payload or an accepted late AT proves it.
    # trigger_requested alone is not evidence: it also included member quotes.
    op.execute("""UPDATE qq_official_inbound_events i SET explicit_mention=TRUE
        WHERE i.trigger_requested AND i.event_type IN ('GROUP_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE')
          AND (i.event_type='GROUP_AT_MESSAGE_CREATE'
            OR CASE WHEN pg_input_is_valid(i.payload_json, 'jsonb')
                    THEN i.payload_json::jsonb @? 'strict $.d.mentions[*] ? (@.is_you == true)'
                    ELSE FALSE END
            OR EXISTS (SELECT 1 FROM qq_official_gateway_receipts r
                WHERE r.inbox_id=i.inbox_id AND r.account_key=i.account_key
                  AND r.disposition='inbox' AND r.event_type='GROUP_AT_MESSAGE_CREATE'))""")


def downgrade():
    op.execute('ALTER TABLE qq_official_inbound_events DROP COLUMN explicit_mention')
