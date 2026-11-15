"""Explicit bilateral physical links; no inferred membership or owner authority."""
import os
from alembic import op

revision = '0098_conversation_links'
down_revision = '0097_budget_notices'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def upgrade():
    schema()
    op.execute('''CREATE TABLE qq_official_link_challenges (
        challenge_id TEXT PRIMARY KEY CHECK(challenge_id ~ '^LC-[0-9a-f]{32}$'),
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        account_epoch BIGINT NOT NULL CHECK(account_epoch>=1),
        source_endpoint_key TEXT NOT NULL, source_actor_key TEXT NOT NULL,
        source_scope_key TEXT NOT NULL, source_generation BIGINT NOT NULL CHECK(source_generation>=1),
        source_route_epoch BIGINT NOT NULL CHECK(source_route_epoch>=1),
        source_receipt_hash TEXT NOT NULL UNIQUE CHECK(source_receipt_hash ~ '^[0-9a-f]{64}$'),
        source_authority_hash TEXT NOT NULL CHECK(source_authority_hash ~ '^[0-9a-f]{64}$'),
        code_hmac TEXT NOT NULL CHECK(code_hmac='' OR code_hmac ~ '^[0-9a-f]{64}$'),
        target_endpoint_key TEXT, target_actor_key TEXT, target_scope_key TEXT,
        target_generation BIGINT CHECK(target_generation>=1), target_route_epoch BIGINT CHECK(target_route_epoch>=1),
        target_receipt_hash TEXT UNIQUE CHECK(target_receipt_hash ~ '^[0-9a-f]{64}$'),
        target_authority_hash TEXT CHECK(target_authority_hash ~ '^[0-9a-f]{64}$'), target_confirmed_at BIGINT,
        delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_state IN ('pending','committed','failed','unknown')),
        state TEXT NOT NULL DEFAULT 'sending' CHECK(state IN ('sending','issued','confirming','active','cancelled','expired')),
        created_at BIGINT NOT NULL CHECK(created_at>=0), expires_at BIGINT NOT NULL, activated_at BIGINT,
        FOREIGN KEY(source_endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
        FOREIGN KEY(target_endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
        CHECK(expires_at>created_at AND expires_at-created_at<=600),
        CHECK(target_endpoint_key IS NULL OR target_endpoint_key<>source_endpoint_key),
        CHECK((target_endpoint_key IS NULL AND target_actor_key IS NULL AND target_scope_key IS NULL
            AND target_generation IS NULL AND target_route_epoch IS NULL AND target_receipt_hash IS NULL
            AND target_authority_hash IS NULL AND target_confirmed_at IS NULL)
          OR (target_endpoint_key IS NOT NULL AND target_actor_key IS NOT NULL AND target_scope_key IS NOT NULL
            AND target_generation IS NOT NULL AND target_route_epoch IS NOT NULL AND target_receipt_hash IS NOT NULL
            AND target_authority_hash IS NOT NULL AND target_confirmed_at IS NOT NULL
            AND target_confirmed_at>=created_at AND target_confirmed_at<expires_at)),
        CHECK((state='active')=(activated_at IS NOT NULL)),
        CHECK(state<>'active' OR (delivery_state='committed' AND target_endpoint_key IS NOT NULL
            AND activated_at>=target_confirmed_at AND activated_at<expires_at)),
        CHECK(state<>'issued' OR (delivery_state='committed' AND target_endpoint_key IS NULL)),
        CHECK(state<>'confirming' OR target_endpoint_key IS NOT NULL),
        CHECK(state<>'sending' OR (delivery_state='pending' AND target_endpoint_key IS NULL)),
        CHECK((code_hmac='')=(state IN ('active','cancelled','expired'))))''')
    op.execute("CREATE UNIQUE INDEX uq_link_live_code ON qq_official_link_challenges(account_key,code_hmac) WHERE code_hmac<>''")
    op.execute('''CREATE TABLE qq_official_conversation_links (
        link_id TEXT PRIMARY KEY CHECK(link_id ~ '^[0-9a-f]{32}$'),
        challenge_id TEXT NOT NULL UNIQUE REFERENCES qq_official_link_challenges(challenge_id),
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        source_endpoint_key TEXT NOT NULL, target_endpoint_key TEXT NOT NULL,
        source_scope_key TEXT NOT NULL, target_scope_key TEXT NOT NULL,
        source_generation BIGINT NOT NULL, source_route_epoch BIGINT NOT NULL,
        target_generation BIGINT NOT NULL, target_route_epoch BIGINT NOT NULL,
        generation BIGINT NOT NULL DEFAULT 1 CHECK(generation>=1),
        state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
        created_at BIGINT NOT NULL, revoked_at BIGINT,
        FOREIGN KEY(source_endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
        FOREIGN KEY(target_endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key),
        CHECK(source_endpoint_key<>target_endpoint_key),
        CHECK((state='revoked')=(revoked_at IS NOT NULL)), CHECK(revoked_at>=created_at))''')
    op.execute("CREATE UNIQUE INDEX uq_link_active_pair ON qq_official_conversation_links(account_key,source_endpoint_key,target_endpoint_key) WHERE state='active'")
    op.execute('''CREATE FUNCTION protect_link_challenge() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
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
          OR (OLD.delivery_state<>'pending' AND NEW.delivery_state IS DISTINCT FROM OLD.delivery_state)
          OR (OLD.state IN ('active','cancelled','expired') AND NEW IS DISTINCT FROM OLD) THEN
            RAISE EXCEPTION 'immutable_link_challenge'; END IF; RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_link_challenge BEFORE UPDATE ON qq_official_link_challenges
        FOR EACH ROW EXECUTE FUNCTION protect_link_challenge()''')
    op.execute('''CREATE FUNCTION protect_conversation_link() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE c RECORD; BEGIN
        IF TG_OP='INSERT' THEN
            SELECT * INTO c FROM qq_official_link_challenges WHERE challenge_id=NEW.challenge_id;
            IF c.state IS DISTINCT FROM 'active' OR c.delivery_state IS DISTINCT FROM 'committed'
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
    op.execute('''CREATE TRIGGER protect_conversation_link BEFORE INSERT OR UPDATE ON qq_official_conversation_links
        FOR EACH ROW EXECUTE FUNCTION protect_conversation_link()''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_link_challenges LIMIT 1').first():
        raise RuntimeError('conversation_links_require_export_before_downgrade')
    op.execute('DROP TABLE qq_official_conversation_links,qq_official_link_challenges')
    op.execute('DROP FUNCTION protect_conversation_link(),protect_link_challenge()')
