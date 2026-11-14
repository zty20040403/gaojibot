"""Read-only cost dimensions; shared model calls remain one joint attribution."""
import base64
import hashlib
import json
import re

from .model_billing_views import METRICS,metrics,usd

# Only these host-owned expressions enter SQL. No request can supply a column,
# operator, actor identity, pricing rule or arbitrary group-by expression.
ORIGINS = "jsonb_build_object('programs',COALESCE(b.meta->'programs','[]'::jsonb),'commands',COALESCE(b.meta->'commands','[]'::jsonb))::text"
BUCKETS = {
    'scope':"''",
    'job':"CASE WHEN b.background_job_id IS NOT NULL THEN 'job:'||b.background_job_id::text "
        "WHEN jsonb_array_length(COALESCE(b.meta->'commands','[]'::jsonb))>0 THEN 'origins:'||"+ORIGINS+" ELSE 'none' END",
    'program':"CASE WHEN jsonb_array_length(COALESCE(b.meta->'programs','[]'::jsonb))>0 "
        "THEN 'origins:'||"+ORIGINS+" ELSE 'none' END",
    'actor':"CASE WHEN b.meta->>'person' IS NOT NULL THEN 'person:'||(b.meta->>'person') "
        "WHEN b.actor_key IS NULL THEN 'not_human' ELSE 'unknown' END",
}


def public_metrics(row):
    result = metrics(row)
    # JS Number cannot preserve every BIGINT sum or decimal amount. Return
    # token totals and money as decimal strings; unknown counters stay explicit.
    for name in ('input_tokens','output_tokens','cached_tokens','cache_write_tokens'):
        result[name] = str(result[name])
    result['cost_usd'] = usd(result['cost_usd'])
    result['cache_percent'] = str(result['cache_percent']) if result['cache_percent'] is not None else None
    return result


def _item(row,dimension):
    scope,bucket = row['scope_key'],row['bucket']
    meta = json.loads(row['source_metadata_json']) if row['source_metadata_json'] else {}
    result = dict(id=hashlib.sha256(json.dumps([scope,dimension,bucket],ensure_ascii=False).encode()).hexdigest(),
        scope_key=scope,label='本会话',person=None,display=None,job=None,programs=[],commands=[],shared=False)
    if dimension in {'job','program'} and bucket.startswith('origins:'):
        origins = json.loads(bucket[8:])
        programs,commands = origins['programs'],origins['commands']
        result.update(programs=programs,commands=commands,shared=len(programs)+len(commands)>1,
            label=' + '.join(programs) if dimension=='program' else '命令完成／程序共同唤醒' if programs else '命令完成唤醒')
    elif dimension=='job':
        if bucket.startswith('job:'):
            result.update(job=bucket[4:],label='job#'+bucket[4:])
        else:result['label'] = '非后台任务／未关联任务'
    elif dimension=='program':
        result['label'] = '非程序调用／未记录程序归属'
    elif dimension=='actor':
        if bucket.startswith('person:'):
            result.update(person=bucket[7:],display=meta.get('display'),label=meta.get('display') or '未知显示名')
        else:result['label'] = '程序或系统（不归属真人）' if bucket=='not_human' else '未记录发起人'
    return {**result,**public_metrics(row)}


def position(after,query):
    if not after:return None
    try:
        if type(after) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,30000}',after) is None:raise ValueError
        value = json.loads(base64.urlsafe_b64decode(after+'='*(-len(after)%4)).decode())
        if (type(value) is not list or len(value)!=6 or value[:4]!=query
                or any(type(part) is not str for part in value[4:])):raise ValueError
        return value[4:]
    except (ValueError,UnicodeError):
        raise ValueError('invalid_billing_cursor') from None


def snapshot(database,*,dimension,days,as_of,scope_key='',limit=50,after=''):
    if dimension not in BUCKETS:raise ValueError('invalid_billing_dimension')
    if type(days) is not int or not 1<=days<=365 or type(as_of) is not int or not 0<=as_of<=2**63-1:
        raise ValueError('invalid_billing_window')
    if type(limit) is not int or not 1<=limit<=100 or type(scope_key) is not str or len(scope_key)>1000:
        raise ValueError('invalid_billing_page')
    query = [dimension,days,as_of,scope_key]
    seek = position(after,query)
    since = max(0,as_of-days*86400)
    params = [since,as_of]
    where = 'b.started_at>=? AND b.started_at<?'
    if scope_key:where += ' AND b.scope_key=?'; params.append(scope_key)
    bucket = BUCKETS[dimension]
    base = 'WITH base AS (SELECT b.*,b.source_metadata_json::jsonb AS meta FROM model_usage_calls b WHERE '+where+') '
    groups = base+'SELECT b.scope_key,'+bucket+' AS bucket,'+METRICS+''',
        (array_agg(b.source_metadata_json ORDER BY b.started_at DESC,b.call_id DESC))[1] AS source_metadata_json
        FROM base b GROUP BY b.scope_key,bucket'''
    connection = database.store_connection()
    try:
        cursor = connection.cursor()
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        page = 'SELECT * FROM ('+groups+') AS g'
        values = list(params)
        if seek:
            page += ' WHERE (g.scope_key,g.bucket)>(?,?)'; values.extend(seek)
        rows = cursor.execute(page+' ORDER BY g.scope_key,g.bucket LIMIT ?',(*values,limit+1)).fetchall()
        count = cursor.execute('SELECT count(*) AS n FROM ('+groups+') AS grouped',params).fetchone()['n']
        totals = cursor.execute('SELECT '+METRICS+' FROM model_usage_calls b WHERE '+where,params).fetchone()
        connection.commit()
    except BaseException:
        connection.rollback(); raise
    finally:connection.close()
    more = len(rows)>limit
    rows = rows[:limit]
    last = rows[-1] if more else None
    next_cursor = base64.urlsafe_b64encode(json.dumps(query+[last['scope_key'],last['bucket']],
        ensure_ascii=False,separators=(',',':')).encode()).decode().rstrip('=') if last else None
    return dict(dimension=dimension,days=days,as_of=as_of,since=since,items=[_item(row,dimension) for row in rows],
        totals=public_metrics(totals),group_count=count,next_cursor=next_cursor,
        attribution='shared_calls_are_joint_not_duplicated')
