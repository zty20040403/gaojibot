"""Program-owned artifacts reuse immutable manifests without a human source."""
import os
from alembic import op

revision = '0085_program_files'
down_revision = '0084_program_wakes'
branch_labels = depends_on = None

LEGACY_SOURCE = """(source_migration_item_id IS NULL AND source_extension_kind IS DISTINCT FROM 'migration'
    AND source_execution_id IS NOT NULL AND source_canonical_message_id IS NOT NULL AND source_native_message_id IS NOT NULL)
 OR (source_migration_item_id IS NOT NULL AND source_extension_kind IS NOT NULL AND source_extension_kind IN ('migration','project')
    AND source_execution_id IS NULL AND source_canonical_message_id IS NULL AND source_native_message_id IS NULL
    AND ((source_task_id IS NOT NULL AND source_task_revision IS NOT NULL AND source_task_revision=1)
      OR (source_extension_kind='migration' AND source_task_id IS NULL AND source_task_revision IS NULL)))"""


def upgrade():
    quoted = op.get_bind().dialect.identifier_preparer.quote(os.environ.get('AI_POSTGRES_SCHEMA','qq_bot'))
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''ALTER TABLE qq_official_program_actions ADD CONSTRAINT uq_program_file_source
        UNIQUE(action_id,endpoint_key,conversation_generation)''')
    op.execute('''ALTER TABLE qq_official_file_artifacts ADD COLUMN source_program_action_id TEXT UNIQUE,
        ADD CONSTRAINT fk_program_file_source FOREIGN KEY(source_program_action_id,endpoint_key,conversation_generation)
            REFERENCES qq_official_program_actions(action_id,endpoint_key,conversation_generation),
        ADD CONSTRAINT ck_program_file_source CHECK (
            (source_program_action_id IS NOT NULL)=(source_extension_kind IS NOT DISTINCT FROM 'program'))''')
    op.execute('ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT ck_file_source_extension_kind')
    op.execute("ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_extension_kind CHECK(source_extension_kind IN ('project','migration','background','program'))")
    op.execute('ALTER TABLE qq_official_file_artifacts DROP CONSTRAINT ck_file_source_union')
    op.execute('''ALTER TABLE qq_official_file_artifacts ADD CONSTRAINT ck_file_source_union CHECK (('''+LEGACY_SOURCE+''') OR (
        source_extension_kind IS NOT DISTINCT FROM 'program' AND source_program_action_id IS NOT NULL
        AND source_execution_id IS NULL AND source_task_id IS NULL AND source_task_revision IS NULL
        AND source_canonical_message_id IS NULL AND source_native_message_id IS NULL
        AND source_migration_item_id IS NULL AND source_job_id IS NULL))''')
    op.execute('''CREATE FUNCTION program_file_source_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.source_program_action_id IS DISTINCT FROM OLD.source_program_action_id THEN
            RAISE EXCEPTION 'immutable_program_file_source';
        END IF; RETURN NEW; END $$''')
    op.execute('''CREATE TRIGGER program_file_source_immutable BEFORE UPDATE ON qq_official_file_artifacts
        FOR EACH ROW EXECUTE FUNCTION program_file_source_immutable()''')


def downgrade():
    raise RuntimeError('program_file_evidence_requires_export_before_downgrade')
