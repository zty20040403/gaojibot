"""Local control for a physical QQ endpoint, never its shared history alias.

All mutations join the authenticated ingress/admin transaction. Incoming roles
come only from the top-level author of the authenticated transport dispatch.
"""
import hashlib
import json
import secrets

from ...application.commands import parse_command
from ...platform_types import EndpointKey

OPERATIONS = frozenset({'stop','start','kill'})


def state(cursor, endpoint):
    row = cursor.execute('SELECT stopped,stop_generation FROM conversation_settings WHERE scope_key=?',
        (endpoint.scope.key,)).fetchone()
    return dict(row) if row else {'stopped':False,'stop_generation':0}


def permitted(cursor, endpoint, *, canonical=None, generation=None, commands=False):
    current = state(cursor,endpoint)
    source = cursor.execute('''SELECT conversation_generation,conversation_stopped,turn_text FROM qq_official_inbound_events
        WHERE endpoint_key=? AND canonical_message_id=?''',(endpoint.key,canonical)).fetchone() if canonical is not None else None
    if commands and source and source['turn_text'].lstrip().startswith(('/', '!')):
        return True
    if current['stopped'] or generation is not None and generation != current['stop_generation']:
        return False
    # Persisted child tasks/artifacts carry their own creation generation; a
    # future scheduled occurrence may legitimately cite an older chat message.
    return generation is not None or source is None or (not source['conversation_stopped'] and source['conversation_generation']==current['stop_generation'])


def change(cursor, endpoint, action, *, now, actor, authorized):
    """Trusted host callback only; chat never supplies the authorized argument."""
    if not isinstance(endpoint,EndpointKey) or action not in OPERATIONS:
        raise ValueError('invalid_conversation_control')
    cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
    if cursor.execute('SELECT 1 FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=?',(endpoint.key,endpoint.account.key)).fetchone() is None:
        raise ValueError('conversation_endpoint_missing')
    result = {'action':action,'status':'denied','text':'只有本群群主、管理员或宿主 owner 可以控制本会话。','cancelled_jobs':0}
    if authorized:
        cursor.execute('INSERT INTO conversation_settings(scope_key) VALUES (?) ON CONFLICT DO NOTHING',(endpoint.scope.key,))
        current = cursor.execute('SELECT * FROM conversation_settings WHERE scope_key=? FOR UPDATE',(endpoint.scope.key,)).fetchone()
        if action == 'stop' and not current['stopped']:
            cursor.execute('UPDATE conversation_settings SET stopped=TRUE,stop_generation=stop_generation+1 WHERE scope_key=?',(endpoint.scope.key,))
        elif action == 'start':
            cursor.execute('UPDATE conversation_settings SET stopped=FALSE WHERE scope_key=?',(endpoint.scope.key,))
        if action in {'stop','kill'}:
            jobs = cursor.execute('''UPDATE background_jobs SET state='cancelled',finished_at=?,error='会话控制命令取消'
                WHERE endpoint_key=? AND state IN ('queued','running') RETURNING job_id''',(now,endpoint.key)).fetchall()
            for job in jobs:
                cursor.execute('''UPDATE durable_jobs SET status='cancelled',lease_owner='',lease_until=NULL,finished_at=?,updated_at=?
                    WHERE job_id=? AND status IN ('pending','running')''',(now,now,job['job_id']))
            result['cancelled_jobs'] = len(jobs)
            result['cancelled_job_ids'] = [job['job_id'] for job in jobs]
        if action == 'stop':
            cursor.execute('''UPDATE background_jobs SET delivered_at=?,error=concat_ws('；',NULLIF(error,''),'会话已停止，结果通知已取消')
                WHERE endpoint_key=? AND delivered_at IS NULL''',(now,endpoint.key))
        text = {'stop':'本会话已停止：消息仍记录，但不再回复普通消息；用 /start 恢复。',
            'start':'本会话已恢复；不会补答停止期间的消息。','kill':'已请求取消本会话的全部后台任务；停止状态不变。'}[action]
        if result['cancelled_jobs']:
            text += f"\n已请求取消 {result['cancelled_jobs']} 个后台任务。"
        result.update(status='ok',text=text,generation=state(cursor,endpoint)['stop_generation'])
    detail = json.dumps({'actor_ref':hashlib.sha256(actor.encode()).hexdigest(),'outcome':result['status'],
        'cancelled_jobs':result['cancelled_jobs']},separators=(',',':'))
    cursor.execute('''INSERT INTO admin_security_audit(audit_id,account_id,action,target,detail,created_at) VALUES (?,'',?,?,?,?)''',
        (secrets.token_hex(16),'qq.conversation.'+action,hashlib.sha256(endpoint.key.encode()).hexdigest(),detail,now))
    return result


def ingest(cursor, *, inbox_id, decoded, text, now, authority=None):
    """After normal admission and ledger insertion, within the same TxB.

    Preserve a control receipt before acknowledgement. Duplicate inbox processing
    must not re-apply a stop/start/kill or repeat the audit. Never infer a role
    from a mention, display name, forwarded element, or a canonical scope alias.
    """
    row = cursor.execute('''SELECT i.conversation_control,m.sender_actor_key FROM qq_official_inbound_events i
        JOIN messages m ON m.canonical_message_id=i.canonical_message_id
        WHERE i.inbox_id=? AND i.account_key=? AND i.endpoint_key=? AND i.native_message_id=? FOR UPDATE OF i''',
        (inbox_id,decoded.endpoint.account.key,decoded.endpoint.key,decoded.native_message_id)).fetchone()
    if row is None or row['sender_actor_key'] != decoded.actor.key:
        raise ValueError('control_inbox_missing')
    receipt = row['conversation_control']
    parsed = parse_command(text)
    if receipt is None and parsed is not None and parsed.name in OPERATIONS:
        if not parsed.valid or parsed.argument:
            receipt = {'action':parsed.name,'status':'syntax','text':f'用法：/{parsed.name}','cancelled_jobs':0}
        elif now-decoded.occurred_at > 300:
            receipt = {'action':parsed.name,'status':'denied','text':'这条控制命令已过期，请重新发送。','cancelled_jobs':0}
        else:
            owner = authority is not None and authority.owner_for_actor(cursor,decoded.endpoint,decoded.actor) is not None
            authorized = (decoded.endpoint.kind=='private' or decoded.platform_role in {'owner','admin'} or owner)
            receipt = change(cursor,decoded.endpoint,parsed.name,now=now,actor=decoded.actor.key,authorized=authorized)
    current = state(cursor,decoded.endpoint)
    cursor.execute('''UPDATE qq_official_inbound_events SET conversation_generation=?,conversation_stopped=?,conversation_control=?
        WHERE inbox_id=?''',(current['stop_generation'],current['stopped'],json.dumps(receipt) if receipt else None,inbox_id))
    return receipt,current
