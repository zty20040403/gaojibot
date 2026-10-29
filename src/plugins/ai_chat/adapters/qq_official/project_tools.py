"""Narrow official project IO over existing handlers, never legacy fallback."""
from copy import deepcopy
import json

from src.plugins.ai_chat.project_access import projects_available
from src.plugins.ai_chat.project_tool_executor import execute_project_tool,project_parameters,routes_to_project
from src.plugins.ai_chat.tools.registry import FunctionTool,ToolRegistry

_NAMES=frozenset({'sandbox_exec','sandbox_read_file','sandbox_write_file','send_file_from_sandbox'})


def project_registry(registry,context):
    if not projects_available(context):return registry
    def wrap(original):
        name=original.definition['function']['name']
        def available(ctx):return projects_available(ctx) or original.enabled(ctx)
        def definition(value,ctx):
            if not projects_available(ctx):
                if original.project is not None:original.project(value,ctx)
                return
            value['function']['parameters']=project_parameters(value)
            value['function']['description']+=' 省略 sandbox_id 时操作当前已授权项目；提供时仅操作本来源独占的临时工作区。'
        async def run(turn,args):
            if routes_to_project(turn.context,name,args):
                return await execute_project_tool(turn.context,name,args)
            if original.enabled(turn.context):return await original.run(turn,args)
            return json.dumps({'ok':False,'code':'tool_unavailable'})
        return FunctionTool(deepcopy(original.definition),available,run,project=definition,platform_neutral=True)
    return ToolRegistry(wrap(handler) if handler.definition['function']['name'] in _NAMES else handler
                        for handler in registry.handlers)
