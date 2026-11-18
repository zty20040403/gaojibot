"""Store the one-to-one digest on its turn."""
import os

from alembic import op

revision = '0116_inline_turn_digests'
down_revision = '0115_group_interest_sharing'
branch_labels = depends_on = None


def schema():
    name = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot').strip() or 'qq_bot'
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(name)},public')


def verify_copy():
    # A missing row on either side must differ even when the old digest strings are empty.
    mismatch = op.get_bind().exec_driver_sql('''SELECT 1 FROM turn_digests d FULL JOIN (
        SELECT turn_id,digest_skeleton,digest_approach,digest_created_at
        FROM agent_turns WHERE digest_created_at IS NOT NULL
    ) t USING(turn_id)
    WHERE (d.skeleton,d.approach,d.created_at) IS DISTINCT FROM
          (t.digest_skeleton,t.digest_approach,t.digest_created_at) LIMIT 1''').first()
    if mismatch:
        raise RuntimeError('turn_digest_copy_mismatch')


def upgrade():
    schema()
    op.execute('LOCK TABLE agent_turns,turn_digests IN ACCESS EXCLUSIVE MODE')
    op.execute('''ALTER TABLE agent_turns ADD COLUMN digest_skeleton TEXT,
        ADD COLUMN digest_approach TEXT, ADD COLUMN digest_created_at BIGINT,
        ADD CONSTRAINT ck_agent_turns_digest_all_or_none CHECK (
            num_nonnulls(digest_skeleton,digest_approach,digest_created_at) IN (0,3))''')
    op.execute('''UPDATE agent_turns t SET digest_skeleton=d.skeleton,
        digest_approach=d.approach,digest_created_at=d.created_at
        FROM turn_digests d WHERE d.turn_id=t.turn_id''')
    verify_copy()
    op.execute('DROP TABLE turn_digests RESTRICT')


def downgrade():
    schema()
    op.execute('LOCK TABLE agent_turns IN ACCESS EXCLUSIVE MODE')
    op.execute('''CREATE TABLE turn_digests (
        turn_id BIGINT PRIMARY KEY REFERENCES agent_turns(turn_id) ON DELETE CASCADE,
        skeleton TEXT NOT NULL, approach TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL)''')
    op.execute('''INSERT INTO turn_digests (turn_id,skeleton,approach,created_at)
        SELECT turn_id,digest_skeleton,digest_approach,digest_created_at FROM agent_turns
        WHERE digest_created_at IS NOT NULL''')
    verify_copy()
    op.execute('''ALTER TABLE agent_turns DROP CONSTRAINT ck_agent_turns_digest_all_or_none,
        DROP COLUMN digest_skeleton,DROP COLUMN digest_approach,DROP COLUMN digest_created_at''')
