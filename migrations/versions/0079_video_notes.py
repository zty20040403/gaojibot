"""Scoped background video conclusions; original message bodies stay immutable."""
import os
from alembic import op

revision = '0079_video_notes'
down_revision = '0078_image_captions'
branch_labels = depends_on = None


def upgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    op.execute('''CREATE TABLE message_media_notes (
        canonical_message_id BIGINT NOT NULL REFERENCES messages(canonical_message_id),
        segment_index INTEGER NOT NULL CHECK(segment_index BETWEEN 0 AND 511),
        summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 500),
        method TEXT NOT NULL CHECK(method IN ('frames','video')),
        job_id BIGINT NOT NULL REFERENCES background_jobs(job_id),
        updated_at BIGINT NOT NULL,
        PRIMARY KEY(canonical_message_id,segment_index)
    )''')
    op.execute('ALTER TABLE usage_events ADD COLUMN background_job_id BIGINT REFERENCES background_jobs(job_id)')
    op.execute('''CREATE OR REPLACE VIEW messages_with_captions AS SELECT m.*, (
        SELECT jsonb_object_agg(derived.segment_index::text,derived.summary) FROM (
            SELECT mm.segment_index,a.summary FROM message_media mm JOIN media_analysis a USING(media_id)
            WHERE mm.canonical_message_id=m.canonical_message_id AND mm.fetch_status='ready'
            UNION ALL SELECT n.segment_index,n.summary FROM message_media_notes n
            WHERE n.canonical_message_id=m.canonical_message_id
        ) derived
    ) AS media_captions FROM messages m''')


def downgrade():
    schema = os.environ.get('AI_POSTGRES_SCHEMA', 'qq_bot')
    quoted = op.get_bind().dialect.identifier_preparer.quote(schema)
    op.execute(f'SET LOCAL search_path TO {quoted},public')
    if op.get_bind().exec_driver_sql('SELECT 1 FROM message_media_notes UNION ALL SELECT 1 FROM usage_events WHERE background_job_id IS NOT NULL LIMIT 1').first():
        raise RuntimeError('video_notes_and_costs_require_export_before_downgrade')
    op.execute('''CREATE OR REPLACE VIEW messages_with_captions AS SELECT m.*, (
        SELECT jsonb_object_agg(mm.segment_index::text,a.summary) FROM message_media mm
        JOIN media_analysis a USING(media_id)
        WHERE mm.canonical_message_id=m.canonical_message_id AND mm.fetch_status='ready'
    ) AS media_captions FROM messages m''')
    op.drop_column('usage_events','background_job_id',schema=schema)
    op.drop_table('message_media_notes',schema=schema)
