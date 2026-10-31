"""Only the narrow owned-sandbox catalog; no legacy project/browser router."""
from __future__ import annotations

from copy import deepcopy
from functools import partial

from src.plugins.ai_chat.ai_tools import SANDBOX_TOOLS
from src.plugins.ai_chat.tools.registry import FunctionTool

from ..application.owned_sandbox import OwnedSandboxExecutor

_NAMES = frozenset({'sandbox_read_file', 'send_file_from_sandbox'})


def enabled(context):
    executor = context.agent_executor
    return ('sandbox' in context.capabilities and isinstance(executor, OwnedSandboxExecutor)
            and executor.generation_enabled and executor.matches(context.inbound or context.background_source))


async def execute(turn, arguments, *, name):
    executor = turn.context.agent_executor
    if not enabled(turn.context):
        return '{"ok":false,"code":"generation_unavailable","message_accepted":false}'
    return await executor.execute(name, arguments)


def project(definition, context):
    function = definition['function']
    name = function['name']
    function['description'] = {
        'sandbox_read_file': '读取本会话工作区里的文本，例如后台报告全文。生成、修改文件请交给 agent。',
        'send_file_from_sandbox': '从本会话工作区发送文件，最多 100 MB。后台完成后用报告给出的路径发送，以 message_accepted 判断是否确认送达。',
    }[name]
    function['parameters']['properties'].pop('sandbox_id')
    function['parameters']['required'] = ['path']
    function['parameters']['properties']['path']['description'] = '工作区文件的相对路径，或 /work/、/workspace/ 开头的绝对路径。'
    if name == 'send_file_from_sandbox':
        function['parameters']['properties']['filename']['description'] = '显示文件名，保留实际扩展名。'


HANDLERS = tuple(FunctionTool(deepcopy(definition), enabled,
    partial(execute, name=definition['function']['name']), project=project,
    platform_neutral=True, required_features=frozenset())
    for definition in SANDBOX_TOOLS if definition['function']['name'] in _NAMES)
