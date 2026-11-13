"""Explicit, read-only audit projection; never expose approval commitments or keys."""
import json
import re


def snapshot(database,*,limit,scope_key='',plan='',state='',before=''):
    conditions,params = ['TRUE'],[]
    for column,value in (('scope_key',scope_key),('plan_name',plan),('state',state)):
        if value:
            conditions.append('a.'+column+'=?'); params.append(value)
    where = ' AND '.join(conditions)
    page,seek = '',[]
    if before:
        match = re.fullmatch(r'([0-9]{1,18}):([a-f0-9]{32})',before)
        if match is None:raise ValueError('invalid_ops_audit_cursor')
        page,seek = ' AND (a.created_at,a.action_id)<(?,?)',[int(match[1]),match[2]]
    connection = database.store_connection()
    try:
        cursor = connection.cursor()
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        rows = cursor.execute('''SELECT a.action_id,a.job_id,a.request_id,a.scope_key,a.plan_name,a.parameters_json,a.state,
            a.created_at,a.updated_at,a.expires_at,a.approved_at,a.started_at,a.finished_at,a.remote_id,a.error_code,a.output_summary,
            a.approved_actor_key=a.actor_key AS approved_requester,m.sender_principal_id,m.sender_display,d.status AS notice_status
            FROM ops_actions a JOIN background_jobs j ON j.job_id=a.job_id
            LEFT JOIN messages m ON m.canonical_message_id=j.canonical_message_id
            LEFT JOIN deliveries d ON d.plan_id=a.notice_plan_id AND d.part_index=0
            WHERE '''+where+page+' ORDER BY a.created_at DESC,a.action_id DESC LIMIT ?',(*params,*seek,limit+1)).fetchall()
        counts = cursor.execute('SELECT a.state,count(*) AS count FROM ops_actions a WHERE '+where+' GROUP BY a.state',params).fetchall()
        connection.commit()
    except BaseException:
        connection.rollback(); raise
    finally:connection.close()
    items = []
    for raw in rows[:limit]:
        row = dict(raw)
        row['parameters'] = json.loads(row.pop('parameters_json'))
        row['requester'] = {'person':row.pop('sender_principal_id'),'display':row.pop('sender_display') or '未知'}
        row['approver'] = row['requester'] if row.pop('approved_requester') else None
        row['job'] = 'job#'+str(row.pop('job_id'))
        items.append(row)
    last = items[-1] if items else None
    return {'items':items,'counts':{row['state']:row['count'] for row in counts},
        'next_cursor':f"{last['created_at']}:{last['action_id']}" if len(rows)>limit else None}
