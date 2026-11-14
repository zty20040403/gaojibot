"""Body-free, revocable roles observed only from authenticated transport."""
import os
from alembic import op

revision = '0099_link_role_evidence'
down_revision = '0098_conversation_links'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def upgrade():
    schema()
    # Do not backfill a current role from old history, labels or forwarded data.
    op.execute('''CREATE TABLE qq_official_link_roles (
        account_key TEXT NOT NULL REFERENCES qq_official_accounts(account_key),
        endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
        endpoint_generation BIGINT NOT NULL CHECK(endpoint_generation>=1),
        route_epoch BIGINT NOT NULL CHECK(route_epoch>=1),
        role TEXT NOT NULL CHECK(role IN ('','owner','admin')),
        revision BIGINT NOT NULL CHECK(revision>=1), observed_at BIGINT NOT NULL CHECK(observed_at>=0),
        receipt_hash TEXT NOT NULL CHECK(receipt_hash ~ '^[0-9a-f]{64}$'),
        PRIMARY KEY(endpoint_key,actor_key),
        FOREIGN KEY(endpoint_key,account_key) REFERENCES qq_official_endpoints(endpoint_key,account_key))''')
    op.execute('''CREATE FUNCTION protect_link_role() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_link_role_fence'; END IF;
        IF ROW(NEW.account_key,NEW.endpoint_key,NEW.actor_key) IS DISTINCT FROM ROW(OLD.account_key,OLD.endpoint_key,OLD.actor_key)
          OR NEW.revision<>OLD.revision+(CASE WHEN ROW(NEW.role,NEW.endpoint_generation,NEW.route_epoch)
            IS DISTINCT FROM ROW(OLD.role,OLD.endpoint_generation,OLD.route_epoch) THEN 1 ELSE 0 END)
          OR (ROW(NEW.endpoint_generation,NEW.route_epoch)=ROW(OLD.endpoint_generation,OLD.route_epoch)
            AND NEW.observed_at<OLD.observed_at) THEN RAISE EXCEPTION 'immutable_link_role_fence'; END IF;
        RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER protect_link_role BEFORE UPDATE OR DELETE ON qq_official_link_roles
        FOR EACH ROW EXECUTE FUNCTION protect_link_role()''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('SELECT 1 FROM qq_official_link_roles LIMIT 1').first():
        raise RuntimeError('link_role_evidence_requires_export_before_downgrade')
    op.execute('DROP TABLE qq_official_link_roles')
    op.execute('DROP FUNCTION protect_link_role()')
