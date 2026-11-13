"""Bind caption policy to physical endpoints without inheriting alias grants."""
import os
from alembic import op

revision = '0092_caption_scopes'
down_revision = '0091_ops_replies'
branch_labels = depends_on = None


def schema():
    value = os.environ.get('AI_POSTGRES_SCHEMA','qq_bot')
    op.execute(f'SET LOCAL search_path TO {op.get_bind().dialect.identifier_preparer.quote(value)},public')


# EndpointKey/ConversationScope serialize the same canonical JSON body.
ALIASES = """SELECT canonical_scope_key AS scope_key FROM qq_official_endpoints
    WHERE canonical_scope_key<>regexp_replace(endpoint_key,'^endpoint:','scope:')
    UNION SELECT regexp_replace(endpoint_key,'^endpoint:','scope:') FROM qq_official_endpoints
    WHERE canonical_scope_key<>regexp_replace(endpoint_key,'^endpoint:','scope:')"""


def upgrade():
    schema()
    # Do not guess who should inherit a history-scoped grant. Alias-related
    # controls must be explicitly reviewed/re-enabled for the physical endpoint.
    op.execute('UPDATE conversation_settings SET image_captions=FALSE,image_daily_limit=200 WHERE scope_key IN ('+ALIASES+')')
    # Never refund already admitted work. A conservative maximum also handles
    # swapped routes and inactive endpoints without overwriting another bucket.
    op.execute('''WITH historical AS MATERIALIZED (
        SELECT regexp_replace(e.endpoint_key,'^endpoint:','scope:') AS scope_key,d.day,MAX(d.admitted) AS admitted
        FROM qq_official_endpoints e JOIN image_caption_daily d ON d.scope_key=e.canonical_scope_key
        GROUP BY 1,2)
        INSERT INTO image_caption_daily(scope_key,day,admitted) SELECT scope_key,day,admitted FROM historical
        ON CONFLICT(scope_key,day) DO UPDATE SET admitted=GREATEST(image_caption_daily.admitted,EXCLUDED.admitted)''')


def downgrade():
    schema()
    if op.get_bind().exec_driver_sql('''SELECT 1 FROM conversation_settings WHERE image_captions AND scope_key IN ('''+ALIASES+''')
        UNION ALL SELECT 1 FROM image_caption_daily WHERE admitted>0 AND scope_key IN ('''+ALIASES+''') LIMIT 1''').first():
        raise RuntimeError('caption_alias_policy_requires_export_before_downgrade')
