"""Bounded official host tools and existing read-only Fleet projections; the shared registry and LLM loop remain.

This factory is host-only. Merely adding a capability string or changing task
text cannot construct the bound authority or enable these handlers.
"""
import hashlib
import json
import re
from functools import partial
from urllib.parse import quote

from src.bot_security.store import SecurityError, canonical
from src.plugins.ai_chat.fleet_client import FleetControlError
from src.plugins.ai_chat.fleet_tools import fleet_overview, inspect_host, model_status
from src.plugins.ai_chat.ai_tools import (FLEET_OVERVIEW_TOOL, HOST_INSPECT_TOOL,
    SERVICE_INSPECT_TOOL, SERVICE_LOGS_TOOL, MODEL_STATUS_TOOL)
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tool_policy import ToolApproval
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.registry import FunctionTool, ToolRegistry
from src.plugins.ai_chat.tools.state import structured_tool_source

HOST_READ_TOOLS = frozenset({'fleet_overview', 'host_inspect', 'service_inspect', 'service_logs', 'model_status'})
HOST_TOOLS = frozenset({'operation_prepare','operation_status','operation_approve'}) | HOST_READ_TOOLS

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

HOST_DEFINITIONS += (FLEET_OVERVIEW_TOOL, HOST_INSPECT_TOOL, SERVICE_INSPECT_TOOL, SERVICE_LOGS_TOOL, MODEL_STATUS_TOOL)


class _ReadFleet:
    """Only fixed GET routes, fenced before each RPC and before returning facts.

    Shared projections use this small view instead of the legacy Event handler.
    The existing operation/external journal remains exclusive to mutations.
    """
    def __init__(self, owner, app):
        self.owner, self.app = owner, app
        self.services = owner.bridge.authority.services
        self.hosts = {host for host, _ in self.services}

    def current(self):
        source = self.owner.bridge.guard(self.owner.source.task_id)
        if source != self.owner.source:
            raise SecurityError('official_host_source_changed')
        with self.owner.bridge.transaction_factory() as cursor:
            account, _ = self.owner.bridge.authority._identity(cursor, source)
        return 'admin:' + account['username'], self.owner.bridge.authority.origin(source)

    async def _get(self, path):
        actor, origin = await self.app.db_call(self.current)
        result = await self.owner.client._raw_request('GET', path, actor=actor, origin=origin)
        await self.app.db_call(self.current)
        return result

    def host_id(self, host):
        if not isinstance(host, str) or host not in self.hosts:
            raise SecurityError('official_host_target_unavailable')
        return quote(host, safe='')

    def unit_id(self, host, unit):
        self.host_id(host)
        if not isinstance(unit, str) or (host, unit) not in self.services:
            raise SecurityError('official_host_target_unavailable')
        return quote(unit, safe='')

    async def fleet(self):
        raw = await self._get('/v1/fleet')
        result = dict(raw)
        result['inventory'] = [item for item in raw.get('inventory', []) if item.get('host_id') in self.hosts]
        data = dict(raw.get('data') or {})
        data['hosts'] = [item for item in data.get('hosts', []) if item.get('host') in self.hosts]
        result['data'] = data
        failed = dict(raw.get('failed_units') or {})
        failures = dict(failed.get('data') or {})
        failures['hosts'] = [{**item, 'units': [unit for unit in item.get('units', [])
            if (item.get('host'), unit.get('unit')) in self.services]}
            for item in failures.get('hosts', []) if item.get('host') in self.hosts]
        failed['data'] = failures
        result['failed_units'] = failed
        alerts = dict(raw.get('active_alerts') or {})
        alert_data = dict(alerts.get('data') or {})
        alert_data['alerts'] = [item for item in alert_data.get('alerts', [])
            if (item.get('labels') or {}).get('instance') in self.hosts]
        alerts['data'] = alert_data
        result['active_alerts'] = alerts
        return result

    async def workers(self):
        raw = await self._get('/v1/workers')
        return {**raw, 'items': [item for item in raw.get('items', []) if item.get('host_id') in self.hosts]}

    async def resource_policies(self):
        return await self._get('/v1/resource-policies')

    async def host(self, host):
        return await self._get('/v1/hosts/' + self.host_id(host))

    async def host_metrics(self, host):
        return await self._get('/v1/hosts/' + self.host_id(host) + '/metrics')

    async def unit(self, host, unit):
        unit = self.unit_id(host, unit)
        return await self._get('/v1/hosts/' + self.host_id(host) + '/units/' + unit)

    async def logs(self, host, unit, *, lines, since_seconds):
        unit = self.unit_id(host, unit)
        return await self._get('/v1/hosts/' + self.host_id(host) + '/units/' + unit
            + '/logs?lines=' + str(lines) + '&since_seconds=' + str(since_seconds))


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
            if name in HOST_READ_TOOLS:
                result = await self._read(name, turn, arguments)
            elif name=='operation_prepare':
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

    async def _read(self, name, turn, arguments):
        app = turn.context.app
        client = _ReadFleet(self, app)
        fields = {'fleet_overview': set(), 'host_inspect': {'host_id'},
            'service_inspect': {'host_id', 'unit'},
            'service_logs': {'host_id', 'unit', 'lines', 'since_seconds'}, 'model_status': {'profile'}}
        if set(arguments) - fields[name]:
            raise SecurityError('official_host_arguments_invalid')
        if name == 'fleet_overview':
            return await fleet_overview(client)
        if name == 'host_inspect':
            client.host_id(arguments.get('host_id'))
            return await inspect_host(client, arguments['host_id'])
        if name == 'service_inspect':
            return await client.unit(arguments.get('host_id'), arguments.get('unit'))
        if name == 'service_logs':
            lines, since = arguments.get('lines', 50), arguments.get('since_seconds', 3600)
            if type(lines) is not int or type(since) is not int:
                raise SecurityError('official_host_arguments_invalid')
            return await client.logs(arguments.get('host_id'), arguments.get('unit'),
                lines=min(max(lines, 1), 200), since_seconds=min(max(since, 1), 86400))
        profile = arguments.get('profile', '')
        if not isinstance(profile, str) or len(profile) > 128:
            raise SecurityError('official_host_arguments_invalid')
        await app.db_call(client.current)
        result = await model_status(app, profile)
        await app.db_call(client.current)
        return result

    def registry(self,context):
        replacements={item['function']['name']:FunctionTool(item,self.available,partial(self.run,item['function']['name']),
            platform_neutral=True) for item in HOST_DEFINITIONS}
        handlers=[]
        for handler in neutral_registry_for(context).handlers:
            name=handler.definition['function']['name']
            handlers.append(replacements.pop(name,handler))
        return ToolRegistry((*handlers,*replacements.values()))
