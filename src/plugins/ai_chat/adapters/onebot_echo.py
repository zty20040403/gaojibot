"""Historical self-message receipts for managed OneBot deliveries.

No route/source grant is issued and no user handler, model or bridge runs. A
real persisted physical attempt and one exact content match are required.
"""
from nonebot.adapters.onebot.v11 import MessageEvent

from src.plugins.ai_chat.delivery import body_fingerprint
from src.plugins.ai_chat.onebot_codec import decode_onebot_message
from src.plugins.ai_chat.platform_types import EndpointKey
from .qq_official.migration_types import MigrationDenied


def settle_self_echo(runtime,deliveries,bot,event):
    if (not isinstance(event,MessageEvent) or str(event.self_id)!=str(bot.self_id)
            or event.user_id!=event.self_id):
        raise MigrationDenied('authenticated_self_echo_required')
    baseline=runtime._baseline(str(bot.self_id))
    group=getattr(event,'group_id',None)
    # A private self echo often has no recipient in the event. Never infer that
    # missing physical target from a pending delivery or bot's own user ID.
    if group is None:
        return None
    endpoint=EndpointKey(baseline.account,'group',str(group))
    native=str(event.message_id)
    timestamp,now=int(event.time),runtime.sources._now()
    if not 1<=len(native)<=256 or timestamp<0 or timestamp>now+60:
        raise MigrationDenied('invalid_self_echo')
    decoded=decode_onebot_message(event.original_message)
    fingerprint=body_fingerprint(decoded.body,str(decoded.reply_to_native_message_id or ''))
    with deliveries.transaction() as cursor:
        rows=cursor.execute('''SELECT d.delivery_id,a.activity_id FROM migration_dispatch_activities a
            JOIN migration_work_sources s ON s.source_id=a.source_id
            JOIN delivery_attempts t ON t.attempt_id=a.attempt_id
            JOIN deliveries d ON d.delivery_id=t.delivery_id AND d.delivery_id=s.delivery_id
            WHERE a.operation='send' AND a.source_account_key=? AND a.target_endpoint_key=?
              AND s.source_kind='delivery' AND s.account_key=a.source_account_key
              AND d.account_key IS NULL AND d.target_platform='onebot-v11' AND d.target_kind='group'
              AND d.target_native_conversation_id=? AND d.content_fingerprint=?
              AND d.idempotency_key NOT LIKE ?
              AND t.attempt=d.attempts AND d.status IN ('sending','ambiguous','committed')
              AND (d.status<>'committed' OR d.native_message_id=?)
              AND a.state IN ('dispatched','unknown','accepted')
              AND a.dispatched_at BETWEEN ? AND ? ORDER BY a.activity_id LIMIT 2''',
            (baseline.account.key,endpoint.key,str(group),fingerprint,'project-file:%',native,timestamp-600,timestamp+5)).fetchall()
        if len(rows)!=1:
            return None
        delivery_id,activity_id=rows[0]['delivery_id'],rows[0]['activity_id']
        activity=cursor.execute('SELECT * FROM migration_dispatch_activities WHERE activity_id=? FOR UPDATE',
                                (activity_id,)).fetchone()
        delivery=cursor.execute('SELECT * FROM deliveries WHERE delivery_id=? FOR UPDATE',(delivery_id,)).fetchone()
        attempt=cursor.execute('SELECT * FROM delivery_attempts WHERE attempt_id=? FOR UPDATE',
                               (activity['attempt_id'],)).fetchone()
        if (attempt['delivery_id']!=delivery_id or attempt['attempt']!=delivery['attempts']
                or attempt['account_key'] is not None or attempt['state'] not in {'sending','ambiguous','committed'}
                or delivery['content_fingerprint']!=fingerprint or delivery['status'] not in {'sending','ambiguous','committed'}
                or (delivery['status']=='committed' and delivery['native_message_id']!=native)):
            raise MigrationDenied('self_echo_source_changed')
        # The same physical receipt cannot settle two unrelated sends.
        if cursor.execute('''SELECT 1 FROM migration_dispatch_activities WHERE source_account_key=?
                AND target_endpoint_key=? AND activity_id<>? AND state='accepted'
                AND receipt_json::jsonb->>'native_id'=? LIMIT 1''',
                (baseline.account.key,endpoint.key,activity_id,native)).fetchone():
            raise MigrationDenied('self_echo_receipt_reused')
        runtime.routes._record_dispatch_observation(cursor,activity,status='accepted',native_id=native,now=now)
        state=cursor.execute('SELECT state FROM migration_dispatch_activities WHERE activity_id=?',(activity_id,)).fetchone()
        if state['state']!='accepted':
            return None
        cursor.execute('''UPDATE deliveries SET status='committed',native_message_id=?,lease_until=NULL,
            last_error='',updated_at=? WHERE delivery_id=?''',(native,now,delivery_id))
        # A transport timeout already finished this exact physical attempt as
        # ambiguous. Resolve its outcome without erasing the original finish
        # time; the append-only activity observation records this later fact.
        cursor.execute('''UPDATE delivery_attempts SET state='committed',
            detail='reconciled by authenticated self echo',finished_at=COALESCE(finished_at,?)
            WHERE attempt_id=? AND delivery_id=? AND attempt=? AND account_key IS NULL
              AND state IN ('sending','ambiguous','committed')''',
            (now,attempt['attempt_id'],delivery_id,delivery['attempts']))
        if cursor.rowcount!=1:
            raise MigrationDenied('self_echo_attempt_changed')
        return delivery_id
