"""Retire window reservations without modifying delivery attempts or their receipts."""
from alembic import op

revision = '0134_retire_reply_windows'
down_revision = '0133_retire_console_bindings'
branch_labels = depends_on = None


def upgrade():
    op.execute('ALTER TABLE delivery_attempts DROP CONSTRAINT fk_delivery_attempts_window')
    op.execute('DROP TABLE qq_official_control_decisions, qq_official_reply_windows')


def downgrade():
    raise RuntimeError('window_retirement_requires_backup_restore')
