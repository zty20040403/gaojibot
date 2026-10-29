"""Front-end composition port: adds tools to the existing shared LLM loop."""
from __future__ import annotations

from dataclasses import dataclass, replace

from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.registry import AgentRegistry, AGENT_SPECS
from src.plugins.ai_chat.subagents import SubAgentCoordinator
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import SubAgentActions
from src.plugins.ai_chat.tools.subagents import HANDLERS

from .subagent_runtime import TEXT_TOOLS, submit


def text_coordinator(store, model_catalog, *, logger, **options):
    specs = {role: replace(AGENT_SPECS[role], allowed_tools=AGENT_SPECS[role].allowed_tools & TEXT_TOOLS)
             for role in ('supervisor', 'researcher', 'analyst')}
    return SubAgentCoordinator(store, model_catalog, logger=logger,
        registry=AgentRegistry(specs), **options)


def _project(definition, context):
    function = definition['function']
    if function['name'] == 'delegate_agent':
        function['description'] = '提交一个独立文字研究或分析任务，完成后由持久队列把结果送回当前会话。'
        function['parameters']['properties']['role']['enum'] = ['researcher', 'analyst']
    else:
        function['description'] = '提交有多个步骤的文字研究或分析任务；暂不支持文件、项目、宿主操作。'


def task_registry_for(context):
    neutral = neutral_registry_for(context)
    handlers = {handler.definition['function']['name']: replace(handler,
        platform_neutral=True, project=_project) for handler in HANDLERS
        if handler.definition['function']['name'] in {'delegate_agent', 'run_subagents'}}
    return ToolRegistry(tuple(handlers.get(handler.definition['function']['name'], handler)
                              for handler in neutral.handlers))


@dataclass(frozen=True)
class SubmissionBinding:
    """Created by the runtime, never parsed from a tool argument or saved JSON."""
    token: object
    source_api: object
    source_handle: object
    source_job: object
    transaction: object
    database_calls: object
    admission: object
    coordinator: object
    jobs: object
    execution_deadline: int
    result_wait_seconds: int = 86400
    clock: object = None

    def actions_for(self, turn, ports, selected_profile, prepared, parent_trace=None):
        """Call once after prepare_shared_turn_context, before the same LLM loop.

        Before catalog generation the trusted service advertises 'subagents'
        only when this bound factory is installed. It never changes task_mode or
        resume_task_id guards. Runtime owns every captured authority object.
        """
        handle = self.source_handle
        inbound = turn.context.inbound
        if (inbound is None or inbound.endpoint != handle.endpoint or inbound.actor != handle.actor
                or inbound.native_message_id != handle.source_native_message_id
                or inbound.scope.key != handle.scope_key or inbound.route_epoch != handle.route_epoch):
            raise ValueError('subagent_action_source_mismatch')

        async def queue(objective, role=None):
            if role is not None and role not in {'researcher', 'analyst'}:
                raise ValueError('当前平台只开放文字研究与分析 Sub-Agent。')
            call_id = active_tool_call_id.get()
            if not call_id:
                raise ValueError('subagent_requires_host_tool_call')
            packet = ContextPacket(handle.scope_key, ports.conversation_id, None,
                handle.canonical_message_id, objective, endpoint=handle.endpoint,
                requester_actor=handle.actor, supporting_context=prepared.turn_context,
                constraints=('仅文字研究与分析；不得执行宿主/项目/文件操作。',))
            decision = await self.coordinator.prepare_entry(packet, selected_profile,
                role=role, parent_trace=parent_trace)
            if decision.contract.delivery_required or decision.task_type == 'project':
                raise ValueError('当前官方 Sub-Agent 尚不支持文件或项目交付。')
            if any(step['agent'] not in {'researcher', 'analyst'}
                   or not set(step.get('required_tools', ())) <= TEXT_TOOLS for step in decision.steps):
                raise ValueError('subagent_tool_profile_denied')

            def commit():
                with self.transaction() as cursor:
                    return submit(cursor, token=self.token, source_api=self.source_api,
                        source_handle=handle, source_job=self.source_job, tool_call_id=call_id,
                        tool_name='delegate_agent' if role else 'run_subagents', packet=packet,
                        decision=decision, profile=selected_profile.name, now=int(self.clock()),
                        execution_deadline=self.execution_deadline,
                        result_wait_seconds=self.result_wait_seconds, admission=self.admission,
                        coordinator=self.coordinator, jobs=self.jobs)
            return await self.database_calls(commit)

        async def delegate(role, objective):
            task = await queue(objective, role)
            return {'task_id': task.task_id, 'task_handle': task.handle, 'status': 'queued',
                    'message': f'{task.handle} 已进入后台执行，完成后等待合法发送机会。'}

        async def run(objective):
            task = await queue(objective)
            return f'{task.handle} 已进入后台执行，完成后等待合法发送机会。'

        async def resume(task_id):
            raise ValueError('当前平台暂不开放任务修订；可查询或取消本人任务。')

        return SubAgentActions(delegate, run, resume)
