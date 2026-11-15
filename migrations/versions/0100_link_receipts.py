"""Late accepted issuance is a fact, never renewed consent or a replay request."""
import os
from alembic import op

revision = '0100_link_receipts'
down_revision = '0099_link_role_evidence'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def protect_challenge(receipts):
    delivery = ('''(OLD.delivery_state<>'pending' AND NEW.delivery_state IS DISTINCT FROM OLD.delivery_state
        AND NOT (NEW.delivery_state='committed' AND OLD.delivery_receipt_hash IS NULL AND NEW.delivery_receipt_hash IS NOT NULL))
        OR (OLD.delivery_receipt_hash IS NOT NULL AND ROW(NEW.delivery_receipt_hash,NEW.delivery_received_at)
            IS DISTINCT FROM ROW(OLD.delivery_receipt_hash,OLD.delivery_received_at))''' if receipts else
        "(OLD.delivery_state<>'pending' AND NEW.delivery_state IS DISTINCT FROM OLD.delivery_state)")
    terminal = ('''ROW(NEW.state,NEW.code_hmac,NEW.activated_at,NEW.target_endpoint_key,NEW.target_actor_key,NEW.target_scope_key,
        NEW.target_generation,NEW.target_route_epoch,NEW.target_receipt_hash,NEW.target_authority_hash,NEW.target_confirmed_at)
        IS DISTINCT FROM ROW(OLD.state,OLD.code_hmac,OLD.activated_at,OLD.target_endpoint_key,OLD.target_actor_key,OLD.target_scope_key,
        OLD.target_generation,OLD.target_route_epoch,OLD.target_receipt_hash,OLD.target_authority_hash,OLD.target_confirmed_at)'''
        if receipts else 'NEW IS DISTINCT FROM OLD')
    op.execute(f'''CREATE OR REPLACE FUNCTION protect_link_challenge() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF ROW(NEW.challenge_id,NEW.account_key,NEW.account_epoch,NEW.source_endpoint_key,NEW.source_actor_key,
            NEW.source_scope_key,NEW.source_generation,NEW.source_route_epoch,NEW.source_receipt_hash,
            NEW.source_authority_hash,NEW.created_at,NEW.expires_at) IS DISTINCT FROM
           ROW(OLD.challenge_id,OLD.account_key,OLD.account_epoch,OLD.source_endpoint_key,OLD.source_actor_key,
            OLD.source_scope_key,OLD.source_generation,OLD.source_route_epoch,OLD.source_receipt_hash,
            OLD.source_authority_hash,OLD.created_at,OLD.expires_at)
          OR (NEW.code_hmac IS DISTINCT FROM OLD.code_hmac AND NEW.code_hmac<>'')
          OR (OLD.target_endpoint_key IS NOT NULL AND ROW(NEW.target_endpoint_key,NEW.target_actor_key,NEW.target_scope_key,
            NEW.target_generation,NEW.target_route_epoch,NEW.target_receipt_hash,NEW.target_authority_hash,NEW.target_confirmed_at)
            IS DISTINCT FROM ROW(OLD.target_endpoint_key,OLD.target_actor_key,OLD.target_scope_key,
            OLD.target_generation,OLD.target_route_epoch,OLD.target_receipt_hash,OLD.target_authority_hash,OLD.target_confirmed_at))
          OR {delivery}
          OR (OLD.state IN ('active','cancelled','expired') AND ({terminal})) THEN
            RAISE EXCEPTION 'immutable_link_challenge'; END IF; RETURN NEW; END $$''')


def protect_link(receipts):
    required = 'OR c.delivery_receipt_hash IS NULL' if receipts else ''
    op.execute(f'''CREATE OR REPLACE FUNCTION protect_conversation_link() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE c RECORD; BEGIN
        IF TG_OP='INSERT' THEN
            SELECT * INTO c FROM qq_official_link_challenges WHERE challenge_id=NEW.challenge_id;
            IF c.state IS DISTINCT FROM 'active' OR c.delivery_state IS DISTINCT FROM 'committed' {required}
              OR ROW(NEW.account_key,NEW.source_endpoint_key,NEW.target_endpoint_key,NEW.source_scope_key,NEW.target_scope_key,
                NEW.source_generation,NEW.source_route_epoch,NEW.target_generation,NEW.target_route_epoch,NEW.created_at)
                IS DISTINCT FROM ROW(c.account_key,c.source_endpoint_key,c.target_endpoint_key,c.source_scope_key,c.target_scope_key,
                c.source_generation,c.source_route_epoch,c.target_generation,c.target_route_epoch,c.activated_at)
              OR NEW.generation<>1 OR NEW.state<>'active' THEN RAISE EXCEPTION 'link_requires_bilateral_committed_proof'; END IF;
        ELSE
            IF ROW(NEW.link_id,NEW.challenge_id,NEW.account_key,NEW.source_endpoint_key,NEW.target_endpoint_key,
                NEW.source_scope_key,NEW.target_scope_key,NEW.source_generation,NEW.source_route_epoch,
                NEW.target_generation,NEW.target_route_epoch,NEW.created_at) IS DISTINCT FROM
               ROW(OLD.link_id,OLD.challenge_id,OLD.account_key,OLD.source_endpoint_key,OLD.target_endpoint_key,
                OLD.source_scope_key,OLD.target_scope_key,OLD.source_generation,OLD.source_route_epoch,
                OLD.target_generation,OLD.target_route_epoch,OLD.created_at)
              OR (OLD.state='revoked' AND NEW IS DISTINCT FROM OLD)
              OR NEW.generation<>OLD.generation+(CASE WHEN OLD.state='active' AND NEW.state='revoked' THEN 1 ELSE 0 END)
              THEN RAISE EXCEPTION 'immutable_conversation_link'; END IF;
        END IF; RETURN NEW; END $$''')


def upgrade():
    schema()
    op.execute('''ALTER TABLE qq_official_link_challenges
        ADD COLUMN delivery_receipt_hash TEXT CHECK(delivery_receipt_hash ~ '^[0-9a-f]{64}$'),
        ADD COLUMN delivery_received_at BIGINT,
        ADD CONSTRAINT link_receipt_pair CHECK((delivery_receipt_hash IS NULL)=(delivery_received_at IS NULL)
            AND (delivery_receipt_hash IS NULL OR (delivery_state='committed' AND delivery_received_at>=created_at)))''')
    op.execute('''CREATE UNIQUE INDEX uq_link_delivery_receipt ON qq_official_link_challenges(delivery_receipt_hash)
        WHERE delivery_receipt_hash IS NOT NULL''')
    # Existing 0098 synthetic/unsupported active rows have no receipt. Preserve
    # their audit, don't fabricate it. New writes and link inserts require proof.
    op.execute('''ALTER TABLE qq_official_link_challenges ADD CONSTRAINT link_activation_receipt
        CHECK(state<>'active' OR delivery_receipt_hash IS NOT NULL) NOT VALID''')
    protect_challenge(True); protect_link(True)


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_link_challenges WHERE delivery_receipt_hash IS NOT NULL LIMIT 1').first():
        raise RuntimeError('link_receipts_require_export_before_downgrade')
    protect_challenge(False); protect_link(False)
    op.execute('ALTER TABLE qq_official_link_challenges DROP CONSTRAINT link_activation_receipt')
    op.execute('ALTER TABLE qq_official_link_challenges DROP COLUMN delivery_receipt_hash,DROP COLUMN delivery_received_at')
