"""Owner code binding connects an official group to its existing conversation."""
import os
from alembic import op
import sqlalchemy as sa

revision = '0072_qq_simple_binding'
down_revision = '0071_qq_direct_turns'
branch_labels = depends_on = None


def upgrade():
    schema = os.getenv('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(sa.text(f'SET LOCAL search_path TO {quoted},public'))
    op.add_column('qq_official_binding_challenges', sa.Column('source_scope_key', sa.Text(), nullable=True), schema=schema)
    op.alter_column('qq_official_admin_otp_sources', 'window_id', nullable=True, schema=schema)
    constraint = op.get_bind().execute(sa.text("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname='ck_admin_approval_transport' AND conrelid='admin_approvals'::regclass""")).scalar_one()
    op.drop_constraint('ck_admin_approval_transport', 'admin_approvals', schema=schema)
    op.execute(sa.text('ALTER TABLE admin_approvals ADD CONSTRAINT ck_admin_approval_transport '
        + constraint.replace('quorum = 2', 'quorum IN (1, 2)')))
    # Only preserve already approved migrations; a draft never changes history.
    op.execute(sa.text('''UPDATE qq_official_endpoints e SET canonical_scope_key=r.source_scope_key
        FROM migration_routes r JOIN migration_journals j USING(route_id)
        WHERE r.target_endpoint_key=e.endpoint_key AND e.binding_state='active'
        AND (r.phase='completed' OR j.approved_by IS NOT NULL)'''))
    op.execute(sa.text('''UPDATE qq_official_project_grants g SET route_epoch=e.route_epoch
        FROM qq_official_endpoints e WHERE g.endpoint_key=e.endpoint_key'''))


def downgrade():
    op.drop_column('qq_official_binding_challenges', 'source_scope_key', schema=os.getenv('AI_POSTGRES_SCHEMA', 'qq_bot'))
