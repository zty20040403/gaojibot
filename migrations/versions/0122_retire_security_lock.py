"""Replace the synthetic security mutex row with a transaction advisory lock."""
import sqlalchemy as sa
from alembic import op

revision = '0122_retire_security_lock'
down_revision = '0121_attention_reason'
branch_labels = depends_on = None


def upgrade():
    op.drop_table('admin_security_lock', schema=op.get_context().version_table_schema)


def downgrade():
    op.create_table('admin_security_lock',
                    sa.Column('lock_id', sa.Integer, primary_key=True, autoincrement=False),
                    sa.Column('revision', sa.BigInteger, nullable=False), schema=op.get_context().version_table_schema)
