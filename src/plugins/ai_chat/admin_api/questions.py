"""Private audit reads: global admin session, exact account/physical selection.

Saved state is not a grant; consume is not delivery. No runtime/reducer calls.
"""
import base64
from contextlib import contextmanager
import json
import re

from fastapi import HTTPException, Path, Query

from ..platform_types import AccountKey, EndpointKey
from .context import authorize


_MAX_ID = 2**63-1


def _encode(kind, account, endpoint, position):
    return base64.urlsafe_b64encode(json.dumps([1,kind,account,endpoint,position],separators=(',',':')).encode()).decode()


def _decode(value, kind, account, endpoint):
    if not value:return None
    try:
        payload = json.loads(base64.b64decode(value,altchars=b'-_',validate=True))
        if not isinstance(payload,list) or len(payload)!=5 or payload[:4]!=[1,kind,account,endpoint]:raise ValueError
        position = payload[4]
        if not isinstance(position,list) or len(position)!=2:raise ValueError
        if type(position[0]) is not int or not 0<=position[0]<=_MAX_ID:raise ValueError
        if kind=='links':
            if not isinstance(position[1],str) or not re.fullmatch('[0-9a-f]{32}',position[1]):raise ValueError
        elif type(position[1]) is not int or not 1<=position[1]<=position[0]:raise ValueError
        return position
    except (ValueError,TypeError,UnicodeError):
        raise HTTPException(422,'无效或不匹配的问答审计游标') from None


def _selection(cursor, account_key, endpoint_key):
    try:
        account = AccountKey.parse(account_key)
        if account.platform!='qq-official' or account.key!=account_key:raise ValueError
        if endpoint_key:
            endpoint = EndpointKey.parse(endpoint_key)
            if endpoint.account!=account or endpoint.key!=endpoint_key:raise ValueError
    except (TypeError,ValueError):raise HTTPException(404,'找不到该账号或物理会话') from None
    if cursor.execute('SELECT 1 FROM qq_official_accounts WHERE account_key=?',(account_key,)).fetchone() is None:
        raise HTTPException(404,'找不到该账号或物理会话')
    if endpoint_key and cursor.execute('SELECT 1 FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=?',
            (account_key,endpoint_key)).fetchone() is None:raise HTTPException(404,'找不到该账号或物理会话')


def _endpoint(key):
    endpoint = EndpointKey.parse(key)
    return dict(endpoint_key=key,kind=endpoint.kind,native_id=endpoint.native_conversation_id)


def _question(row, *, detail=False):
    fields = ('question_id','link_id','link_generation','state','reason_code','created_at','deadline_at',
        'started_at','finished_at','consumed_at')
    return {**{key:row[key] for key in fields},'job_id':str(row['job_id']),
        'source':_endpoint(row['source_endpoint_key']),'target':_endpoint(row['target_endpoint_key']),
        'delivery_state':'not_tracked',**({'question':row['question'],'answer':row['answer']} if detail else {})}


def create_router(context):
    router = context.new_router()

    @contextmanager
    def read():
        authorize()
        database = context.services.database
        if database is None:raise HTTPException(503,'问答审计数据库未配置')
        connection = database.store_connection()
        try:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            yield connection.cursor()
        finally:connection.close()

    @router.get('/api/conversation-questions')
    def accounts(after: str = Query('',max_length=1000),limit: int = Query(50,ge=1,le=100)):
        authorize()
        if context.services.database is None:return dict(available=False,accounts=[],next_cursor=None)
        with read() as cursor:
            rows = cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key>? ORDER BY account_key LIMIT ?',
                (after,limit+1)).fetchall()
        items = [dict(account_key=row['account_key'],app_id=AccountKey.parse(row['account_key']).app_id,
            environment=AccountKey.parse(row['account_key']).environment) for row in rows[:limit]]
        return dict(available=True,accounts=items,next_cursor=items[-1]['account_key'] if len(rows)>limit else None)

    @router.get('/api/conversation-questions/links')
    def links(account_key: str = Query(...,max_length=1000),endpoint_key: str = Query('',max_length=1000),
              after: str = Query('',max_length=6000),limit: int = Query(50,ge=1,le=100)):
        with read() as cursor:
            _selection(cursor,account_key,endpoint_key)
            position = _decode(after,'links',account_key,endpoint_key)
            where,params = 'l.account_key=?',[account_key]
            if endpoint_key:where+=' AND (l.source_endpoint_key=? OR l.target_endpoint_key=?)'; params.extend((endpoint_key,endpoint_key))
            if position:where+=' AND (l.created_at,l.link_id)<(?,?)'; params.extend(position)
            rows = cursor.execute('''SELECT l.link_id,l.generation,l.state,l.created_at,l.revoked_at,l.source_endpoint_key,
                l.target_endpoint_key,c.delivery_state,c.delivery_receipt_hash IS NOT NULL AS receipt_recorded
                FROM qq_official_conversation_links l JOIN qq_official_link_challenges c USING(challenge_id)
                WHERE '''+where+' ORDER BY l.created_at DESC,l.link_id DESC LIMIT ?',(*params,limit+1)).fetchall()
        items = [dict(link_id=row['link_id'],generation=row['generation'],state=row['state'],created_at=row['created_at'],
            revoked_at=row['revoked_at'],source=_endpoint(row['source_endpoint_key']),target=_endpoint(row['target_endpoint_key']),
            issue_state=row['delivery_state'],receipt_recorded=row['receipt_recorded'],authorization='not_checked') for row in rows[:limit]]
        return dict(items=items,next_cursor=_encode('links',account_key,endpoint_key,
            [rows[limit-1]['created_at'],rows[limit-1]['link_id']]) if len(rows)>limit else None,pagination='live')

    @router.get('/api/conversation-questions/records')
    def records(account_key: str = Query(...,max_length=1000),endpoint_key: str = Query('',max_length=1000),
                after: str = Query('',max_length=6000),limit: int = Query(50,ge=1,le=100)):
        with read() as cursor:
            _selection(cursor,account_key,endpoint_key)
            position = _decode(after,'records',account_key,endpoint_key)
            where,params = 'account_key=?',[account_key]
            if endpoint_key:where+=' AND (source_endpoint_key=? OR target_endpoint_key=?)'; params.extend((endpoint_key,endpoint_key))
            fence = position[0] if position else cursor.execute('SELECT COALESCE(MAX(job_id),0) AS fence FROM qq_official_conversation_questions WHERE '+where,params).fetchone()['fence']
            where+=' AND job_id<=?'; params.append(fence)
            if position:where+=' AND job_id<?'; params.append(position[1])
            rows = cursor.execute('SELECT question_id,job_id,link_id,link_generation,state,reason_code,created_at,deadline_at,started_at,finished_at,consumed_at,source_endpoint_key,target_endpoint_key FROM qq_official_conversation_questions WHERE '+where+' ORDER BY job_id DESC LIMIT ?',(*params,limit+1)).fetchall()
        return dict(items=[_question(row) for row in rows[:limit]],snapshot_job_id=str(fence),
            next_cursor=_encode('records',account_key,endpoint_key,[fence,rows[limit-1]['job_id']]) if len(rows)>limit else None,
            pagination='job_id_fence',state_snapshot='live')

    @router.get('/api/conversation-questions/records/{question_id}')
    def detail(question_id: str = Path(...,pattern='^CQ-[0-9a-f]{32}$'),account_key: str = Query(...,max_length=1000)):
        with read() as cursor:
            _selection(cursor,account_key,'')
            row = cursor.execute('SELECT * FROM qq_official_conversation_questions WHERE account_key=? AND question_id=?',
                (account_key,question_id)).fetchone()
            if row is None:raise HTTPException(404,'找不到该账号的问答记录')
        return dict(item=_question(row,detail=True))

    return router
