"""A neutral local-ledger query; no platform member-list capability is claimed."""
import json
from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from src.plugins.ai_chat.platform_types import InboundMessage
from src.plugins.ai_chat.tools.registry import FunctionTool
from src.plugins.ai_chat.adapters.qq_official.recent_speakers import recent_speakers

DEFINITION={'type':'function','function':{'name':'recent_speakers',
    'description':'查询当前实际会话的近期发言者。只扫描指定时间窗内最多500条可见消息，不是群成员名单，也不授予成员权限。',
    'parameters':{'type':'object','properties':{'limit':{'type':'integer','minimum':1,'maximum':50},
        'max_age_seconds':{'type':'integer','minimum':1,'maximum':604800}},'additionalProperties':False}}}


def enabled(context):
    return (isinstance(context.inbound,InboundMessage) and context.inbound.endpoint.account.platform=='qq-official'
        and 'context' in context.capabilities and getattr(context.platform,'execution',None) is not None)


def query(context,arguments):
    if not enabled(context) or set(arguments)-{'limit','max_age_seconds'}:raise PermissionError('recent_speakers_unavailable')
    platform=context.platform
    binding=platform.execution
    inbound=context.inbound
    if binding.handle.endpoint!=inbound.endpoint or binding.handle.actor!=inbound.actor:
        raise PermissionError('recent_speakers_unavailable')
    now=int(platform._clock())
    with platform.outbox.deliveries.transaction() as c:
        def current(cursor,endpoint):
            row=execution.assert_execution_current(cursor,token=binding.token,handle=binding.handle,job=binding.job,
                admission=binding.admission,now=now)
            return row['state']=='executing' and endpoint==inbound.endpoint and now<row['deadline_at']
        result=recent_speakers(c,endpoint=inbound.endpoint,scope_key=binding.handle.scope_key,now=now,current=current,
            limit=arguments.get('limit',20),max_age_seconds=arguments.get('max_age_seconds',86400))
    return {'ok':True,'is_membership_list':False,'since':result.since,'until':result.until,
        'scanned_message_limit':result.scanned_message_limit,'count_scope':'visible_message_sample',
        'speakers':[{'handle':f'mention#{s.principal_id}' if s.principal_id is not None else None,
            'display':s.display,'last_seen_at':s.last_seen_at,'sample_message_count':s.message_count} for s in result.speakers]}


async def run(turn,arguments):
    try:return json.dumps(await turn.context.app.db_call(query,turn.context,arguments),ensure_ascii=False)
    except (ValueError,PermissionError,execution.ExecutionFenceLost):return json.dumps({'ok':False,'code':'recent_speakers_unavailable'})


HANDLERS=(FunctionTool(DEFINITION,enabled,run,platform_neutral=True),)
