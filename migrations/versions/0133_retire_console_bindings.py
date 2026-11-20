"""Retire the old QQ console enrollment, not accounts, login sessions or inbox evidence."""
from alembic import op

revision = '0133_retire_console_bindings'
down_revision = '0132_retire_approval_tables'
branch_labels = depends_on = None


def upgrade():
    op.execute('DROP TABLE qq_official_admin_identity_challenges, qq_official_admin_bindings, qq_official_binding_rate_limits')
    op.execute('DROP FUNCTION qq_admin_identity_immutable()')


def downgrade():
    raise RuntimeError('console_binding_retirement_requires_backup_restore')
