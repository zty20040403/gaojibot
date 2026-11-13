"""Transactional image-caption admission and per-conversation operator controls."""
from contextlib import contextmanager, nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from .message_ir import MediaNode
from .conversation_scope import ConversationScope
from .platform_types import EndpointKey


def caption_day(now):
    return datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).date().isoformat()


def ingest_captions(cursor, message, now):
    nodes = [node for node in message.body.nodes if isinstance(node, MediaNode)
             and node.media_kind in {'image', 'sticker'}]
    if not nodes:
        return
    physical_scope = EndpointKey.parse(message.source_endpoint_key).scope.key
    settings = cursor.execute('SELECT * FROM conversation_settings WHERE scope_key=? FOR SHARE',
                              (physical_scope,)).fetchone()
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
                       (physical_scope,day))
        admitted = cursor.execute('''UPDATE image_caption_daily SET admitted=admitted+1
            WHERE scope_key=? AND day=? AND admitted<? RETURNING admitted''',
            (physical_scope,day,settings['image_daily_limit'])).fetchone()
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

    def set(self, scope_key, *, enabled, daily_limit, transaction_cursor=None):
        if type(enabled) is not bool or type(daily_limit) is not int or not 0 <= daily_limit <= 10000:
            raise ValueError('invalid_caption_settings')
        scope = ConversationScope.from_key(scope_key)
        if scope.platform!='qq-official' or scope.account is None:
            raise ValueError('caption_scope_unavailable')
        endpoint = EndpointKey(scope.account,scope.kind,scope.native_conversation_id)
        with self.transaction() if transaction_cursor is None else nullcontext(transaction_cursor) as connection:
            connection.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
            if connection.execute('SELECT 1 FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone() is None:
                raise ValueError('caption_scope_unavailable')
            connection.execute('''INSERT INTO conversation_settings(scope_key,image_captions,image_daily_limit)
                VALUES (?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                image_captions=EXCLUDED.image_captions,image_daily_limit=EXCLUDED.image_daily_limit''',
                (scope_key,enabled,daily_limit))
        return {'scope_key':scope_key,'image_captions':enabled,'image_daily_limit':daily_limit}

    def snapshot(self, now):
        return self.versioned_snapshot(now)['items']

    def versioned_snapshot(self, now):
        with self.transaction() as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            version = connection.execute("SELECT version FROM admin_resource_versions WHERE resource_key='captions'").fetchone()
            # Endpoint/scope keys have the exact same canonical JSON body; only
            # the kind tag changes. Never use the movable canonical history key.
            items = [dict(row) for row in connection.execute('''WITH endpoints AS (
                SELECT endpoint_key,regexp_replace(endpoint_key,'^endpoint:','scope:') AS scope_key
                FROM qq_official_endpoints)
                SELECT e.scope_key,COALESCE(s.image_captions,FALSE) AS image_captions,
                COALESCE(s.image_daily_limit,200) AS image_daily_limit,COALESCE(d.admitted,0) AS admitted,
                (SELECT count(*) FROM message_media mm JOIN messages m USING(canonical_message_id)
                    WHERE m.source_endpoint_key=e.endpoint_key AND mm.fetch_status='failed') AS failed,
                (SELECT count(*) FROM message_media mm JOIN messages m USING(canonical_message_id)
                    WHERE m.source_endpoint_key=e.endpoint_key AND mm.fetch_status='quota') AS quota_rejected
                FROM endpoints e LEFT JOIN conversation_settings s ON s.scope_key=e.scope_key
                LEFT JOIN image_caption_daily d ON d.scope_key=e.scope_key AND d.day=?
                ORDER BY e.scope_key''', (caption_day(now),)).fetchall()]
            return {'items':items,'resource_version':version['version'] if version else 0}
