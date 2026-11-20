"""Retire historical share associations and content."""
from alembic import op

revision = '0130_retire_share_sources'
down_revision = '0129b_retire_otp_sources'
branch_labels = depends_on = None


def upgrade():
    op.execute('DROP TABLE message_sources')
    op.execute('DROP TABLE content_sources')


def downgrade():
    raise RuntimeError('restore verified pre-0130 full database backup')
