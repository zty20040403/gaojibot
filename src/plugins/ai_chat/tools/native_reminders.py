"""Neutral official reminder tools over the existing reminder and job tables."""
from datetime import datetime
from functools import partial
import json
import re
from jsonschema import validate,ValidationError
from src.plugins.ai_chat.ai_tools import REMINDER_SET_TOOL,REMINDER_LIST_TOOL,REMINDER_CANCEL_TOOL
from src.plugins.ai_chat.tools.registry import FunctionTool
from src.plugins.ai_chat.platform_types import InboundMessage
from src.plugins.ai_chat.adapters.qq_official.execution import ExecutionFenceLost
from src.plugins.ai_chat.adapters.qq_official.native_schedules import NativeCommandSource,NativeScheduleDenied,canonical,digest

DEFINITIONS=(REMINDER_SET_TOOL,REMINDER_LIST_TOOL,REMINDER_CANCEL_TOOL)


def enabled(context):
    service=getattr(context.platform,'native_background',None)
    return (isinstance(context.inbound,InboundMessage) and context.inbound.endpoint.account.platform=='qq-official'
        and 'reminders' in context.capabilities and service is not None and service.domain is not None
        and getattr(context.platform,'execution',None) is not None)


async def execute(turn,arguments,*,name):
    context=turn.context
    if not enabled(context):return json.dumps({'ok':False,'code':'reminders_unavailable'})
    definition=next(item for item in DEFINITIONS if item['function']['name']==name)
    try:
        validate(arguments,definition['function']['parameters'])
        platform=context.platform;binding=platform.execution;service=platform.native_background
        if binding.token is not service.token:raise NativeScheduleDenied('reminder_account_changed')
        source=NativeCommandSource(binding.handle,binding.job,context.inbound)
        if name=='reminder_list':
            rows=await service.runtime._calls(service.domain.list_reminders,source)
            return json.dumps({'ok':True,'reminders':[{'handle':f"reminder#{row['reminder_id']}",
                'message':row['message'],'scheduled_for':row['scheduled_for'],'status':row['status']} for row in rows]},ensure_ascii=False)
        if name=='reminder_cancel':
            handle=arguments['reminder_handle']
            if not re.fullmatch(r'reminder#[1-9][0-9]*',handle):raise ValueError('invalid_handle')
            result=await service.runtime._calls(service.domain.cancel,source,kind='reminder',identifier=int(handle[9:]))
            return json.dumps({'ok':result.status=='cancelled','cancelled':result.status=='cancelled','handle':handle})
        due=datetime.fromisoformat(arguments['due_at'])
        if due.tzinfo is None or due.utcoffset() is None:raise ValueError('timezone_required')
        request_key=digest(canonical(['native-reminder:v1',binding.handle.execution_id,arguments]))
        result=await service.runtime._calls(service.domain.create_reminder,source,request_key=request_key,
            text=arguments['message'],scheduled_for=int(due.timestamp()))
        return json.dumps({'ok':result.status=='created','reminder':{'handle':f'reminder#{result.identifier}',
            'scheduled_for':result.next_due_at,'authorization_until':result.authorization_until,
            'delivery_seconds':result.delivery_seconds,'status':'pending'}},ensure_ascii=False)
    except (NativeScheduleDenied,ExecutionFenceLost,ValidationError,ValueError,OverflowError):
        return json.dumps({'ok':False,'code':'reminder_source_or_contract_unavailable'})


HANDLERS=tuple(FunctionTool(d,enabled,partial(execute,name=d['function']['name']),platform_neutral=True) for d in DEFINITIONS)
