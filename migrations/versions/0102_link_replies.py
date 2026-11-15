"""Immutable body-free control outcomes bound to the original authenticated input."""
import importlib
from alembic import op

revision = '0102_link_replies'
down_revision = '0101_link_notices'
branch_labels = depends_on = None


def upgrade():
    importlib.import_module('migrations.versions.0101_link_notices').schema()
    op.execute('''CREATE TABLE qq_official_link_replies (
        source_inbox_id TEXT PRIMARY KEY,
        account_key TEXT NOT NULL,
        account_epoch BIGINT NOT NULL CHECK(account_epoch>0),
        endpoint_key TEXT NOT NULL,
        actor_key TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>0),
        route_epoch BIGINT NOT NULL CHECK(route_epoch>0),
        receipt_hash TEXT NOT NULL CHECK(receipt_hash ~ '^[0-9a-f]{64}$'),
        authority_hash TEXT CHECK(authority_hash ~ '^[0-9a-f]{64}$'),
        received_at BIGINT NOT NULL CHECK(received_at>=0),
        expires_at BIGINT NOT NULL CHECK(expires_at>received_at AND expires_at<=received_at+600),
        status TEXT NOT NULL CHECK(status IN ('sending','issued','confirming','active','expired','cancelled',
            'denied','unsupported','limited','invalid','already_linked','revoked','listed','use_unlink')),
        challenge_ref TEXT CHECK(challenge_ref ~ '^LC-[0-9a-f]{32}$'),
        link_ref TEXT CHECK(link_ref ~ '^[0-9a-f]{32}$'),
        entries_json TEXT NOT NULL CHECK(jsonb_typeof(entries_json::jsonb)='array' AND jsonb_array_length(entries_json::jsonb)<=32),
        more BOOLEAN NOT NULL,
        source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
        FOREIGN KEY(source_inbox_id,account_key,endpoint_key,actor_key,receipt_hash)
            REFERENCES qq_official_inbound_events(inbox_id,account_key,endpoint_key,link_control_actor_key,link_control_receipt_hash),
        CONSTRAINT link_reply_unprivileged CHECK(authority_hash IS NOT NULL OR
            (status IN ('denied','unsupported') AND challenge_ref IS NULL AND link_ref IS NULL AND entries_json::jsonb='[]' AND NOT more)),
        CONSTRAINT link_reply_list_only CHECK(status='listed' OR (entries_json::jsonb='[]' AND NOT more)))''')
    op.execute('''CREATE FUNCTION protect_link_reply() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE original RECORD; BEGIN
        IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'immutable_link_reply'; END IF;
        SELECT * INTO original FROM qq_official_inbound_events WHERE inbox_id=NEW.source_inbox_id;
        IF original.inbox_id IS NULL OR original.payload_json IS NOT NULL OR original.status<>'processed'
          OR original.reason_code<>'link_control' OR original.echo_state<>'not_echo'
          OR original.link_control_platform_at IS NULL OR NEW.expires_at>original.link_control_platform_at+600
          OR ROW(NEW.received_at,NEW.endpoint_generation,NEW.route_epoch) IS DISTINCT FROM
             ROW(original.received_at,original.endpoint_generation,original.route_epoch) THEN
            RAISE EXCEPTION 'link_reply_original_input_required'; END IF; RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_link_reply BEFORE INSERT OR UPDATE OR DELETE ON qq_official_link_replies
        FOR EACH ROW EXECUTE FUNCTION protect_link_reply()''')


def downgrade():
    importlib.import_module('migrations.versions.0101_link_notices').schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_link_replies LIMIT 1').first():
        raise RuntimeError('link_replies_require_export_before_downgrade')
    op.execute('DROP TABLE qq_official_link_replies')
    op.execute('DROP FUNCTION protect_link_reply()')
