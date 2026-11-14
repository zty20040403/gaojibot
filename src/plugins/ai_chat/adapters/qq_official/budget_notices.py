"""One physical-day bot notice, separate from humans, programs and reply windows."""
from ...daily_budgets import interval,read
from ...message_ir import MessageBody,TextNode
from ...model_billing_views import usd
from ...platform_types import ActorKey,EndpointKey
from .conversation_control import permitted
from .identity_store import snapshot
from .lease import assert_epoch
from .renderer import render_plan
from .send_types import OutboxInvariantError,digest,stable_json

TTL = 300


def deadline(row):return min(row['first_exceeded_at']+TTL,interval(row['day_start'])[1])


def rendered(row):
    endpoint = EndpointKey.parse(row['endpoint_key'])
    direct = '群内只响应 @' if endpoint.kind=='group' else '仍可直接私聊提问'
    text = (f"已达到本会话今日模型预算 ${usd(row['budget_usd'])}；当时已记录费用至少 ${usd(row['spent_usd'])}。\n"
        f"{direct}（回复仍会产生费用）；停止主动接话和程序唤醒。\n"
        '北京时间零点恢复新自动动作，不补跑旧请求，不清除 /stop。\n'
        '这是自动动作的软预算，不是供应商硬限额；本通知每天只尝试一次。')
    return render_plan(MessageBody((TextNode(0,text),)),endpoint,markdown=False,max_parts=1)


def check(cursor,token,row,*,now,admission=None):
    assert_epoch(cursor,token)
    endpoint = EndpointKey.parse(row['endpoint_key'])
    route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(endpoint.key,)).fetchone()
    current = read(cursor,endpoint,now=now)
    if (endpoint.account.key!=token.account_key or row['scope_key']!=endpoint.scope.key or row['account_epoch']!=token.epoch
            or row['conversation_generation'] is None or row['notice_budget_generation'] is None
            or row['day_start']!=current.day_start or not row['first_exceeded_at']<=now<deadline(row)
            or row['notice_state'] not in {'pending','sending'} or not current.blocked
            or current.generation!=row['notice_budget_generation']
            or route is None or (route['generation'],route['route_epoch'])!=(row['endpoint_generation'],row['route_epoch'])
            or route['membership_state']!='present' or route['binding_state']=='revoked'
            or endpoint.kind=='group' and route['binding_state']!='active'
            or not permitted(cursor,endpoint,generation=row['conversation_generation'])):
        raise PermissionError('budget_notice_source_unavailable')
    if endpoint.kind=='private':
        # Endpoint access only, like VM admission; the requester remains bot and
        # never inherits this peer's owner or operations privileges.
        peer = ActorKey(endpoint.account,'c2c_user','',endpoint.native_conversation_id)
        if not callable(admission) or not admission(cursor,endpoint,peer):
            raise PermissionError('budget_notice_source_unavailable')
    author = snapshot(cursor,endpoint.account)
    if author is None:raise PermissionError('budget_notice_identity_unavailable')
    return endpoint,author.bot_actor


def load(cursor,endpoint,day):
    if not isinstance(endpoint,EndpointKey) or type(day) is not int or day<0:
        raise OutboxInvariantError('invalid_budget_notice')
    row = cursor.execute('SELECT * FROM conversation_budget_days WHERE scope_key=? AND day_start=?',
        (endpoint.scope.key,day)).fetchone()
    if row is None:raise OutboxInvariantError('budget_notice_source_unavailable')
    return dict(row)


def validate(cursor,*,token,day,plan,requester,logical_key,source_scope_key,source_native_message_id,
        source_canonical_message_id,turn_id,purpose,expires_at,source_generation,now,admission=None):
    row = load(cursor,plan.endpoint,day)
    _,bot = check(cursor,token,row,now=now,admission=admission)
    if (plan.to_json()!=rendered(row).to_json() or requester!=bot or logical_key!='budget:'+str(day)
            or source_scope_key!=row['scope_key'] or source_native_message_id or source_canonical_message_id is not None
            or turn_id is not None or purpose!='unsolicited' or expires_at!=deadline(row)
            or source_generation!=row['conversation_generation']):
        raise OutboxInvariantError('budget_notice_plan_mismatch')


def matches(cursor,row,plan):
    author = snapshot(cursor,EndpointKey.parse(row['endpoint_key']).account)
    expected = stable_json(dict(v=1,rendered=rendered(row).to_json(),control=None,source_native_message_id=''))
    return (author is not None and plan['endpoint_key']==row['endpoint_key']
        and plan['account_key']==author.bot_actor.account.key and plan['requester_actor_key']==author.bot_actor.key
        and plan['source_scope_key']==row['scope_key'] and plan['source_canonical_message_id'] is None and plan['turn_id'] is None
        and plan['purpose']=='unsolicited' and plan['payload_mode']=='persistent' and plan['part_count']==1
        and plan['endpoint_generation']==row['endpoint_generation'] and plan['route_epoch']==row['route_epoch']
        and plan['conversation_generation']==row['conversation_generation'] and plan['expires_at']==deadline(row)
        and plan['manifest_json']==expected and plan['manifest_digest']==digest(expected))


def bind(cursor,endpoint,day,plan):
    row = load(cursor,endpoint,day)
    if row['notice_plan_id'] not in (None,plan['plan_id']) or not matches(cursor,row,plan):
        raise OutboxInvariantError('budget_notice_plan_mismatch')
    cursor.execute("UPDATE conversation_budget_days SET notice_plan_id=?,notice_state='sending' WHERE scope_key=? AND day_start=?",
        (plan['plan_id'],endpoint.scope.key,day))


def enqueue(cursor,token,row,*,admission,now):
    from .plans import enqueue_plan
    endpoint,bot = check(cursor,token,row,now=now,admission=admission)
    return enqueue_plan(cursor,token=token,rendered=rendered(row),requester=bot,
        logical_key='budget:'+str(row['day_start']),source_scope_key=endpoint.scope.key,
        expires_at=deadline(row),now=now,purpose='unsolicited',admission=admission,
        source_generation=row['conversation_generation'],budget_day=row['day_start'])


def denial(cursor,*,token,delivery,now,admission=None):
    row = cursor.execute('SELECT * FROM conversation_budget_days WHERE notice_plan_id=?',(delivery['plan_id'],)).fetchone()
    if row is None:return 'budget_notice_source_unavailable'
    try:
        check(cursor,token,row,now=now,admission=admission)
        plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(delivery['plan_id'],)).fetchone()
        return None if plan is not None and matches(cursor,row,plan) else 'budget_notice_plan_mismatch'
    except PermissionError as error:return str(error)
