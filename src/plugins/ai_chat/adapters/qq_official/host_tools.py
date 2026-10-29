"""Three bounded official host tools; the shared registry and LLM loop remain.

This factory is host-only. Merely adding a capability string or changing task
text cannot construct the bound authority or enable these handlers.
"""
import hashlib
import json
import re
from functools import partial

from src.bot_security.store import SecurityError, canonical
from src.plugins.ai_chat.fleet_client import FleetControlError
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tool_policy import ToolApproval
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.registry import FunctionTool, ToolRegistry
from src.plugins.ai_chat.tools.state import structured_tool_source

HOST_TOOLS = frozenset({'operation_prepare','operation_status','operation_approve'})

def definition(name,description,properties,required):
    return {'type':'function','function':{'name':name,'description':description,
        'parameters':{'type':'object','additionalProperties':False,'properties':properties,'required':required}}}

HOST_DEFINITIONS = (
    definition('operation_prepare','准备精确服务操作并申请本任务的两人审批；准备成功不表示已执行。',
        {'host_id':{'type':'string'},'resource_ref':{'type':'string'},
         'operation':{'type':'string','enum':['service.start','service.stop','service.restart']},
         'ttl_seconds':{'type':'integer','minimum':1,'maximum':300}},['host_id','resource_ref','operation']),
    definition('operation_status','只读取本任务已准备的原操作状态，不提交动作。',
        {'operation_id':{'type':'string'}},['operation_id']),
    definition('operation_approve','在本任务精确合同已获两人批准后提交一次既有服务操作；不接受新目标或新参数。',
        {'operation_id':{'type':'string'},'contract_hash':{'type':'string'},
         'resource_version':{'type':'integer','minimum':1}},['operation_id','contract_hash','resource_version']),
)

class OfficialHostTools:
    def __init__(self, *, bridge, client, source):
        self.bridge,self.client,self.source=bridge,client,source

    def available(self,context):
        meta=structured_tool_source(context)
        if (not isinstance(meta,BackgroundToolSource) or meta.source_kind!='subagent'
                or meta.source_ref!='task:'+str(self.source.task_id) or meta.source_revision!=self.source.revision
                or meta.endpoint!=self.source.endpoint or meta.actor!=self.source.actor
                or 'host' not in context.capabilities):
            return False
        try:
            return self.bridge.guard(self.source.task_id)==self.source
        except (SecurityError,PermissionError):
            return False

    def approval(self, policy, name, arguments):
        try:
            if (name!='operation_approve' or type(arguments)is not dict
                    or set(arguments)!={'operation_id','contract_hash','resource_version'}
                    or type(arguments['resource_version'])is not int):
                raise SecurityError('official_host_approval_unavailable')
            self.bridge.guard(self.source.task_id)
            for item in self.bridge.store.external_calls(self.source.task_id):
                evidence=self.bridge.validate_item(self.source,item)
                if evidence is None:continue
                record,context,contract=evidence
                if (arguments=={'operation_id':record['operation_id'],'contract_hash':contract['contract_hash'],
                        'resource_version':contract['resource_version']}
                        and self.bridge.granted(context,contract)):
                    return ToolApproval(True,source='official-task-quorum')
        except (SecurityError,PermissionError,ValueError,TypeError):
            pass
        return ToolApproval(False,source='official-task-quorum',reason='official_host_approval_unavailable')

    async def run(self,name,turn,arguments):
        if not self.available(turn.context) or type(arguments) is not dict:
            return json.dumps({'ok':False,'code':'tool_unavailable'})
        try:
            if name=='operation_prepare':
                if set(arguments)-{'host_id','resource_ref','operation','ttl_seconds'}:
                    raise SecurityError('official_host_arguments_invalid')
                ttl=arguments.get('ttl_seconds',300)
                call_id=active_tool_call_id.get()
                if type(ttl)is not int or not 1<=ttl<=300 or not isinstance(call_id,str) or not call_id:
                    raise SecurityError('official_host_arguments_invalid')
                body={key:arguments.get(key) for key in ('host_id','resource_ref','operation')}
                body['deadline_at']=min(self.source.deadline_at,self.bridge.authority.now()+ttl)
                body['idempotency_key']='official:'+hashlib.sha256(canonical([self.source.data(),call_id]).encode()).hexdigest()
                result=await self.client.prepare_operation(body,actor='official',origin=self.source.scope.key)
            else:
                expected={'operation_id'} if name=='operation_status' else {'operation_id','contract_hash','resource_version'}
                if (set(arguments)!=expected or not isinstance(arguments.get('operation_id'),str)
                        or re.fullmatch(r'op_[a-f0-9]{32}',arguments['operation_id']) is None):
                    raise SecurityError('official_host_arguments_invalid')
                if name=='operation_status':
                    result=await self.client.operation(arguments['operation_id'],actor='official',origin=self.source.scope.key)
                elif name=='operation_approve':
                    if (not isinstance(arguments['contract_hash'],str)
                            or re.fullmatch('[a-f0-9]{64}',arguments['contract_hash']) is None
                            or type(arguments['resource_version'])is not int or arguments['resource_version']<1):
                        raise SecurityError('official_host_arguments_invalid')
                    result=await self.client.approve_operation(arguments['operation_id'],arguments['contract_hash'],
                        arguments['resource_version'],actor='official',origin=self.source.scope.key)
                else:
                    raise SecurityError('official_host_arguments_invalid')
            return json.dumps(result,ensure_ascii=False)
        except (SecurityError,FleetControlError,ValueError,TypeError):
            return json.dumps({'ok':False,'code':'official_host_operation_unavailable'})

    def registry(self,context):
        replacements={item['function']['name']:FunctionTool(item,self.available,partial(self.run,item['function']['name']),
            platform_neutral=True) for item in HOST_DEFINITIONS}
        handlers=[]
        for handler in neutral_registry_for(context).handlers:
            name=handler.definition['function']['name']
            handlers.append(replacements.pop(name,handler))
        return ToolRegistry((*handlers,*replacements.values()))
