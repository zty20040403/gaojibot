"""Scoped, default-off captions over the existing media queue and analysis tables."""
import os
from alembic import op

revision = '0078_image_captions'
down_revision = '0077_retired_features'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE conversation_settings (
        scope_key TEXT PRIMARY KEY, image_captions BOOLEAN NOT NULL DEFAULT FALSE,
        image_daily_limit INTEGER NOT NULL DEFAULT 200 CHECK(image_daily_limit BETWEEN 0 AND 10000)
    )''')
    op.execute('''CREATE TABLE image_caption_daily (
        scope_key TEXT NOT NULL, day TEXT NOT NULL, admitted INTEGER NOT NULL DEFAULT 0 CHECK(admitted>=0),
        PRIMARY KEY(scope_key,day)
    )''')
    op.drop_constraint('uq_message_media_scope_message_segment', 'message_media', schema=schema)
    op.execute('''CREATE UNIQUE INDEX uq_message_media_canonical_segment ON message_media(canonical_message_id,segment_index)
        WHERE canonical_message_id IS NOT NULL''')
    op.execute('''CREATE UNIQUE INDEX uq_message_media_legacy_segment ON message_media(scope_key,native_message_id,segment_index)
        WHERE canonical_message_id IS NULL''')
    op.drop_constraint('ck_message_media_fetch_status', 'message_media', schema=schema)
    op.execute("ALTER TABLE message_media ADD CONSTRAINT ck_message_media_fetch_status CHECK(fetch_status IN ('pending','fetching','ready','failed','disabled','quota'))")
    op.drop_constraint('ck_media_jobs_type', 'media_jobs', schema=schema)
    op.execute("ALTER TABLE media_jobs ADD CONSTRAINT ck_media_jobs_type CHECK(job_type IN ('fetch','caption','embedding','official_caption'))")
    op.execute('''CREATE VIEW messages_with_captions AS SELECT m.*, (
        SELECT jsonb_object_agg(mm.segment_index::text,a.summary) FROM message_media mm
        JOIN media_analysis a USING(media_id)
        WHERE mm.canonical_message_id=m.canonical_message_id AND mm.fetch_status='ready'
    ) AS media_captions FROM messages m''')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql("SELECT 1 FROM media_jobs WHERE job_type='official_caption' LIMIT 1").first():
        raise RuntimeError('caption_history_requires_export_before_downgrade')
    op.execute('DROP VIEW messages_with_captions')
    op.execute("UPDATE message_media SET fetch_status='failed' WHERE fetch_status IN ('disabled','quota')")
    op.drop_constraint('ck_message_media_fetch_status', 'message_media', schema=schema)
    op.execute("ALTER TABLE message_media ADD CONSTRAINT ck_message_media_fetch_status CHECK(fetch_status IN ('pending','fetching','ready','failed'))")
    op.drop_constraint('ck_media_jobs_type', 'media_jobs', schema=schema)
    op.execute("ALTER TABLE media_jobs ADD CONSTRAINT ck_media_jobs_type CHECK(job_type IN ('fetch','caption','embedding'))")
    op.execute('DROP INDEX uq_message_media_canonical_segment')
    op.execute('DROP INDEX uq_message_media_legacy_segment')
    op.create_unique_constraint('uq_message_media_scope_message_segment', 'message_media',
        ['scope_key', 'native_message_id', 'segment_index'], schema=schema)
    op.drop_table('image_caption_daily', schema=schema)
    op.drop_table('conversation_settings', schema=schema)
