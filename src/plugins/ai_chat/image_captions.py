"""Transactional image-caption admission and per-conversation operator controls."""
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from .message_ir import MediaNode


def caption_day(now):
    return datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).date().isoformat()


def ingest_captions(cursor, message, now):
    nodes = [node for node in message.body.nodes if isinstance(node, MediaNode)
             and node.media_kind in {'image', 'sticker'}]
    if not nodes:
        return
    settings = cursor.execute('SELECT * FROM conversation_settings WHERE scope_key=? FOR SHARE',
                              (message.scope_key,)).fetchone()
    enabled = settings is not None and settings['image_captions']
    for node in nodes:
        link = cursor.execute('''INSERT INTO message_media(scope_key,canonical_message_id,native_message_id,
            sender_native_user_id,segment_index,media_kind,source_type,source_url,fetch_status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'qq-official.caption','','disabled',?,?)
            ON CONFLICT(canonical_message_id,segment_index) WHERE canonical_message_id IS NOT NULL DO NOTHING
            RETURNING message_media_id''', (message.scope_key,message.canonical_message_id,message.native_message_id,
                message.sender_native_user_id,node.segment_index,node.media_kind,now,now)).fetchone()
        if link is None or not enabled:
            continue
        # One atomic row update caps concurrent messages; replay cannot reserve twice.
        day = caption_day(now)
        cursor.execute('''INSERT INTO image_caption_daily(scope_key,day) VALUES (?,?) ON CONFLICT DO NOTHING''',
                       (message.scope_key,day))
        admitted = cursor.execute('''UPDATE image_caption_daily SET admitted=admitted+1
            WHERE scope_key=? AND day=? AND admitted<? RETURNING admitted''',
            (message.scope_key,day,settings['image_daily_limit'])).fetchone()
        cursor.execute('UPDATE message_media SET fetch_status=? WHERE message_media_id=?',
                       ('pending' if admitted else 'quota',link['message_media_id']))
        if admitted:
            cursor.execute('''INSERT INTO media_jobs(job_type,message_media_id,status,attempts,next_attempt_at,created_at,updated_at)
                VALUES ('official_caption',?,'pending',0,?,?,?)''', (link['message_media_id'],now,now,now))


class CaptionSettings:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def transaction(self):
        connection = self.database.store_connection()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set(self, scope_key, *, enabled, daily_limit):
        if type(enabled) is not bool or type(daily_limit) is not int or not 0 <= daily_limit <= 10000:
            raise ValueError('invalid_caption_settings')
        with self.transaction() as connection:
            if connection.execute('SELECT 1 FROM qq_official_endpoints WHERE canonical_scope_key=?', (scope_key,)).fetchone() is None:
                raise ValueError('caption_scope_unavailable')
            connection.execute('''INSERT INTO conversation_settings(scope_key,image_captions,image_daily_limit)
                VALUES (?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                image_captions=EXCLUDED.image_captions,image_daily_limit=EXCLUDED.image_daily_limit''',
                (scope_key,enabled,daily_limit))
        return {'scope_key':scope_key,'image_captions':enabled,'image_daily_limit':daily_limit}

    def snapshot(self, now):
        with self.transaction() as connection:
            return [dict(row) for row in connection.execute('''SELECT DISTINCT e.canonical_scope_key AS scope_key,
                COALESCE(s.image_captions,FALSE) AS image_captions,COALESCE(s.image_daily_limit,200) AS image_daily_limit,
                COALESCE(d.admitted,0) AS admitted,
                (SELECT count(*) FROM message_media mm WHERE mm.scope_key=e.canonical_scope_key AND mm.fetch_status='failed') AS failed,
                (SELECT count(*) FROM message_media mm WHERE mm.scope_key=e.canonical_scope_key AND mm.fetch_status='quota') AS quota_rejected
                FROM qq_official_endpoints e LEFT JOIN conversation_settings s ON s.scope_key=e.canonical_scope_key
                LEFT JOIN image_caption_daily d ON d.scope_key=e.canonical_scope_key AND d.day=?
                ORDER BY scope_key''', (caption_day(now),)).fetchall()]
