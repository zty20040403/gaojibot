"""Command adapter for the real native scheduled-work domain, not migration."""
from __future__ import annotations

import hashlib
import inspect
import json
import re

from ...application.commands import CommandResult
from ...application.routine_commands import command_result
from .command_fence import CommandEffectDenied
from .native_schedules import NativeCommandSource, NativeScheduleDenied


class RoutineCommands:
    def __init__(self,*,domain,life,inbound,database_calls):
        if (life.handle.endpoint!=inbound.endpoint or life.handle.actor!=inbound.actor
                or life.handle.source_native_message_id!=inbound.native_message_id):
            raise CommandEffectDenied('routine_command_source_changed')
        self.domain,self.inbound,self.calls=domain,inbound,database_calls
        self.source=NativeCommandSource(life.handle,life.job,inbound)

    async def _call(self,method,*args,**kwargs):
        result=self.calls(method,*args,**kwargs)
        return await result if inspect.isawaitable(result) else result

    async def __call__(self,inbound,parsed):
        if inbound is not self.inbound:raise CommandEffectDenied('routine_command_source_changed')
        async def call(name,args):
            try:
                if name=='routine_list':
                    rows=await self._call(self.domain.list_owned,self.source)
                    return {'ok':True,'routines':[dict(row,handle=f"routine#{row['routine_id']}") for row in rows]}
                if name=='routine_cancel':
                    handle=args['routine_handle']
                    if not re.fullmatch('routine#[1-9][0-9]*',handle):raise ValueError('invalid target')
                    result=await self._call(self.domain.cancel,self.source,kind='routine',identifier=int(handle[8:]),
                        expected_version=args['expected_version'])
                else:
                    payload=dict(args)
                    target=payload.pop('routine_handle',None)
                    if target is not None:
                        if not re.fullmatch('routine#[1-9][0-9]*',target):raise ValueError('invalid target')
                        payload['routine_id']=int(target[8:])
                    if 'expected_version' in payload and 'routine_id' not in payload:
                        raise CommandEffectDenied('routine_update_requires_actual_id')
                    payload.setdefault('timezone','Asia/Shanghai')
                    key=hashlib.sha256(json.dumps(['qq-routine-command:v1',self.source.handle.execution_id,
                        name,payload],sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
                    result=await self._call(self.domain.set_routine,self.source,request_key=key,**payload)
                if result.status not in {'created','updated','cancelled'}:
                    return {'ok':False,'error':{'not_found':'当前入口没有这个由你创建的任务。',
                        'conflict':'任务版本已变化，请重新查看 /例行 列表。'}.get(result.status,'例行服务暂不可用。')}
                if result.status=='cancelled':return {'ok':True}
                return {'ok':True,'routine':dict(handle=f'routine#{result.identifier}',version=result.revision,
                    name=args['name'],next_due_at=result.next_due_at,authorization_until=result.authorization_until,
                    execution_seconds=result.execution_seconds,delivery_seconds=result.delivery_seconds)}
            except NativeScheduleDenied:
                return {'ok':False,'error':'例行来源、执行策略或授权期限不可用。'}
        return CommandResult('ok',await command_result(parsed.argument,call,explicit_targets=True))
