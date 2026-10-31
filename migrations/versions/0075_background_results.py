"""Background results reuse the existing artifact sender."""
from alembic import op
import os

revision = '0075_background_results'
down_revision = '0074_background_agents'
branch_labels = depends_on = None


def upgrade():
    quoted = op.get_bind().dialect.identifier_preparer.quote(os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot'))
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute("ALTER TABLE background_jobs ADD COLUMN frontend_answer TEXT NOT NULL DEFAULT ''")
    op.execute('ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT ck_file_source_extension_kind')
    op.execute("ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_extension_kind CHECK(source_extension_kind IN ('project','migration','background'))")


def downgrade():
    op.drop_column('background_jobs', 'frontend_answer')
    op.execute('ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT ck_file_source_extension_kind')
    op.execute("ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_extension_kind CHECK(source_extension_kind IN ('project','migration'))")
