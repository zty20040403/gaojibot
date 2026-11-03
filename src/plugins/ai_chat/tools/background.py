"""Foreground controls for simple background agents."""
from functools import partial
import json

from ..prompt_context import quote_evidence
from .registry import FunctionTool


def definition(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': list(required)}}}


def service(context):
    return getattr(getattr(context.app, 'official_runtime', None), 'agent_service', None)


def enabled(context):
    return context.inbound is not None and service(context) is not None and hasattr(context.platform, 'execution')


async def task_context(turn):
    if not enabled(turn.context):
        return ''
    result = await service(turn.context).control(turn, 'agent_status', {})
    jobs = [{**row, 'title': row['title'][:160]} for row in result['jobs'][:8]]
    return ('[后台任务]\n当前会话中这位用户的任务快照（活动任务优先，最多八项）；'
            '追问或追加要求先用 agent_status 刷新，不能根据聊天里没提到任务就认为没开工。\n'
            + quote_evidence(json.dumps(jobs, ensure_ascii=False), 'background_jobs'))


async def execute(turn, arguments, *, name):
    try:
        result = await service(turn.context).control(turn, name, arguments)
        return json.dumps({'ok': True, **result}, ensure_ascii=False)
    except (ValueError, PermissionError) as error:
        return json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False)


JOB = {'type': 'string', 'description': 'agent 返回的 job#编号。'}
DEFINITIONS = (
    definition('agent', '仅为新的独立工作启动后台任务，不用于催进度、继续、修改或补充已有任务。先看后台任务快照；已有任务时先 agent_status，相关补充用 agent_steer。立即返回 job#，最多同时三个，每个两小时；先用 say 说明将开始，调用成功才说已启动。',
        {'task': {'type': 'string'}, 'title': {'type': 'string'},
         'independent': {'type': 'boolean', 'description': '仅当用户确实要求另外独立做一件事时设 true，以允许和已有任务并行；追问、质疑是否开始或修改要求不能设 true。'}}, ('task',)),
    definition('agent_steer', '给明确选中的 queued/running 任务补充或修改要求，也用于回答任务提出的问题。先 agent_status 核对；多个任务且指代不清先问用户，不向全部任务广播。成功只表示补充已入队，不表示已经执行完。',
        {'job': JOB, 'message': {'type': 'string'}}, ('job', 'message')),
    definition('agent_status', '用户问进度、是否开始、结果在哪或追加要求时先调用。查看当前会话中这位用户的任务、耗时、最近工具和结果摘要；省略 job 列表查询。没有报告或最近工具不代表没开工，不要编造完成百分比。', {'job': JOB}),
    definition('agent_cancel', '停止自己发起的后台任务；已生成的文件保留。', {'job': JOB}, ('job',)),
)
HANDLERS = tuple(FunctionTool(item, enabled, partial(execute, name=item['function']['name']),
                             platform_neutral=True) for item in DEFINITIONS)
