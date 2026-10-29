"""Bounded host profile around the existing dispatcher/Coordinator session.

The trusted runtime supplies route resolution and profile admission separately.
No configured factory or no exact native host profile means no host tools.
"""
import sys
from dataclasses import replace
from types import SimpleNamespace

from src.bot_security.store import SecurityError
from src.plugins.ai_chat.agent.registry import AGENT_SPECS, AgentRegistry
from src.plugins.ai_chat.agent.external import poll_external
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from src.plugins.ai_chat.subagents import AgentExecutionHooks, SubAgentCoordinator
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from .host_tools import HOST_TOOLS, OfficialHostTools
from .subagent_composition import TaskPlatform
from .subagent_runtime import TEXT_TOOLS, FILE_TOOLS, FILE_ROLES, TaskToolSession


def host_coordinator(store, model_catalog, *, logger, files_enabled=False, **options):
    allowed=TEXT_TOOLS|FILE_TOOLS if files_enabled else TEXT_TOOLS
    roles=('supervisor',*sorted(FILE_ROLES)) if files_enabled else ('supervisor','researcher','analyst')
    specs={role:replace(AGENT_SPECS[role], allowed_tools=
        (AGENT_SPECS[role].allowed_tools & allowed) | (HOST_TOOLS if role in {'researcher','analyst'} else frozenset()))
        for role in roles}
    return SubAgentCoordinator(store,model_catalog,logger=logger,registry=AgentRegistry(specs),**options)


class HostTaskSessionFactory:
    def __init__(self, *, bridge, client, app, route_epoch, base_factory=None):
        if (not callable(route_epoch) or not bridge.authority.services
                or getattr(getattr(client,'authorization',None),'official_authority',None) is not bridge.authority):
            raise SecurityError('official_host_route_unavailable')
        self.bridge,self.client,self.app,self.route_epoch=bridge,client,app,route_epoch
        self.base_factory=base_factory

    async def __call__(self,job,task,files_enabled):
        def profile():
            with self.bridge.transaction_factory() as cursor:
                row=cursor.execute('SELECT tool_profile FROM qq_official_subagent_dispatches WHERE task_id=?',
                                   (task.task_id,)).fetchone()
                return row['tool_profile'] if row else None
        selected=await self.app.db_call(profile)
        if selected!='official-host-v1':
            if self.base_factory is None:
                raise JobDeferred('official_task_context_unavailable',3)
            return await self.base_factory(job,task,files_enabled)
        if files_enabled:
            raise SecurityError('official_host_profile_conflict')
        def epoch():
            with self.bridge.transaction_factory() as cursor:
                return self.route_epoch(cursor,task)
        logical_epoch=await self.app.db_call(epoch)
        cm=self.bridge.bind(task,job,route_epoch=logical_epoch)
        # ContextVars must be entered/exited by this same dispatcher task; DB
        # calls made by its descendants inherit these exact trusted bindings.
        source=cm.__enter__()
        try:
            if any(item['status']=='pending' for item in self.bridge.store.external_calls(task.task_id)):
                if not await poll_external(self.bridge.store,task,self.client):
                    raise JobDeferred('official_host_operation_pending',3)
            meta=BackgroundToolSource(task.endpoint,task.requester_actor,'subagent',
                                      'task:'+str(task.task_id),source.revision)
            capabilities={'self','context','host'}
            if getattr(self.app,'long_term_memory',None) is not None:capabilities.add('memory')
            if getattr(self.app.settings,'search_enabled',False):capabilities.add('web')
            context=ToolContext(SimpleNamespace(context=self.app),event=None,capabilities=frozenset(capabilities),
                                background_source=meta,platform=TaskPlatform(meta.endpoint))
            turn=TurnState(context,user_text=task.objective)
            host_tools=OfficialHostTools(bridge=self.bridge,client=self.client,source=source)
            registry=host_tools.registry(context)
            allowed=TEXT_TOOLS|HOST_TOOLS
            definitions=tuple(item for item in await self.app.db_call(registry.definitions,context)
                              if item['function']['name'] in allowed)
            async def execute(name,arguments):
                self.bridge.guard(task.task_id)
                if name not in allowed:return '{"ok":false,"code":"tool_unavailable"}'
                return await registry.execute(turn,name,arguments)
            async def approval(policy,name,args):
                return await self.app.db_call(host_tools.approval,policy,name,args)
            async def close():cm.__exit__(None,None,None)
            return TaskToolSession(definitions,execute,hooks=AgentExecutionHooks(approval_checker=approval),
                                   close=close,host_enabled=True)
        except BaseException:
            cm.__exit__(*sys.exc_info())
            raise
