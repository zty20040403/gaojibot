"""Foreground controls for simple background agents."""
from functools import partial
import json

from .registry import FunctionTool


def definition(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': list(required)}}}


def service(context):
    return getattr(getattr(context.app, 'official_runtime', None), 'agent_service', None)


def enabled(context):
    return context.inbound is not None and service(context) is not None and hasattr(context.platform, 'execution')


async def execute(turn, arguments, *, name):
    try:
        result = await service(turn.context).control(turn, name, arguments)
        return json.dumps({'ok': True, **result}, ensure_ascii=False)
    except (ValueError, PermissionError) as error:
        return json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False)


JOB = {'type': 'string', 'description': 'agent 返回的 job#编号。'}
DEFINITIONS = (
    definition('agent', '把多步研究、写代码、做文件或视频分析交给后台。立即返回 job#，完成后由你向用户回复。最多同时三个，每个两小时；先用 say 告诉用户已开始。',
        {'task': {'type': 'string'}, 'title': {'type': 'string'}}, ('task',)),
    definition('agent_steer', '给正在运行的后台任务补充要求，也用于回答任务提出的问题。',
        {'job': JOB, 'message': {'type': 'string'}}, ('job', 'message')),
    definition('agent_status', '查看当前会话中自己发起的后台任务、耗时和最近工具。', {'job': JOB}),
    definition('agent_cancel', '停止自己发起的后台任务；已生成的文件保留。', {'job': JOB}, ('job',)),
)
HANDLERS = tuple(FunctionTool(item, enabled, partial(execute, name=item['function']['name']),
                             platform_neutral=True) for item in DEFINITIONS)
