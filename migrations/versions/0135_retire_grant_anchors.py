"""Keep actor links, not grants, as the group-to-private identity association."""
from alembic import op

revision = '0135_retire_grant_anchors'
down_revision = '0134_retire_reply_windows'
branch_labels = depends_on = None


def upgrade():
    op.execute('''LOCK TABLE qq_official_owner_actor_links, qq_official_owner_grants,
        qq_official_binding_candidates, qq_official_binding_challenges, delivery_plans IN ACCESS EXCLUSIVE MODE''')
    op.execute('ALTER TABLE qq_official_owner_actor_links DROP CONSTRAINT ck_qq_official_owner_link_anchor')
    # Carry only the identity 0129 would resolve, never a grant as proof of ownership.
    # The live account configuration and endpoint fences still gate every lookup.
    op.execute('''UPDATE qq_official_owner_actor_links l SET configured_owner_actor_key=g.c2c_actor_key
        FROM qq_official_owner_grants g WHERE l.owner_grant_id=g.grant_id AND l.account_key=g.account_key
        AND l.state='active' AND g.state='active' AND l.owner_revision=g.revision''')
    op.execute("""UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1
        WHERE configured_owner_actor_key IS NULL AND state='active'""")
    op.execute('''ALTER TABLE qq_official_owner_actor_links DROP COLUMN owner_grant_id, DROP COLUMN owner_revision,
        DROP COLUMN candidate_id, ADD CONSTRAINT ck_owner_actor_link_anchor CHECK (
            (configured_owner_actor_key IS NOT NULL AND octet_length(configured_owner_actor_key) BETWEEN 1 AND 1280)
            OR (configured_owner_actor_key IS NULL AND state='revoked'))''')
    # Historical ephemeral plans and their attempts/receipts retain their original identity fields.
    op.execute('''ALTER TABLE delivery_plans DROP CONSTRAINT delivery_plans_challenge_id_account_key_fkey,
        DROP CONSTRAINT delivery_plans_challenge_id_endpoint_key_challenge_expires_fkey,
        DROP CONSTRAINT delivery_plans_challenge_id_requester_actor_key_fkey''')
    op.execute('DROP TABLE qq_official_binding_candidates, qq_official_binding_challenges, qq_official_owner_grants')


def downgrade():
    raise RuntimeError('owner_grant_retirement_requires_backup_restore')
