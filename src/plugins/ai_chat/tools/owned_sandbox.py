"""Only the narrow owned-sandbox catalog; no legacy project/browser router."""
from __future__ import annotations

from copy import deepcopy
from functools import partial

from src.plugins.ai_chat.ai_tools import SANDBOX_TOOLS
from src.plugins.ai_chat.tools.registry import FunctionTool

from ..application.owned_sandbox import OwnedSandboxExecutor

_NAMES = frozenset({'sandbox_create', 'sandbox_list', 'sandbox_destroy', 'sandbox_exec',
                    'sandbox_write_file', 'sandbox_read_file', 'send_file_from_sandbox'})


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
    descriptions = {
        'sandbox_create': '创建本次来源独占的临时隔离工作区，不提供项目或宿主访问。',
        'sandbox_list': '列出本次执行实际创建的工作区。',
        'sandbox_destroy': '删除本次执行的工作区；正式产物须已登记后再清理。',
        'sandbox_exec': '在本次隔离工作区中生成或检验文件。不可执行宿主操作或分离后台命令。',
        'sandbox_write_file': '写入本次工作区内的 UTF-8 文件。',
        'sandbox_read_file': '有界读取本次工作区内的普通文件。',
        'send_file_from_sandbox': '从工作区导出任意格式文件并发送。以 message_accepted 判断是否确认送达。',
    }
    function['description'] = descriptions[name]
    if name in {'sandbox_write_file', 'sandbox_read_file', 'send_file_from_sandbox'}:
        function['parameters']['properties']['path']['description'] = '工作区内文件路径，支持相对路径或 /workspace/ 开头的绝对路径。'
    if name == 'sandbox_create':
        function['description'] += 'Python 已预装 reportlab、pypdf；PDF 可用 gaoji-pdf。先直接使用，不必 pip install。'
    if name == 'sandbox_exec':
        function['parameters']['properties'].pop('background', None)
        function['parameters']['properties']['packages']['description'] = (
            '本次运行所需的 Debian 软件包名。' if getattr(context.agent_executor.sandbox_manager, 'backend', 'oci') == 'vm'
            else '本次运行所需的 Nix 属性名。')
    if name == 'send_file_from_sandbox':
        function['parameters']['properties']['filename']['description'] = '显示文件名，保留实际扩展名。'


HANDLERS = tuple(FunctionTool(deepcopy(definition), enabled,
    partial(execute, name=definition['function']['name']), project=project,
    platform_neutral=True, required_features=frozenset())
    for definition in SANDBOX_TOOLS if definition['function']['name'] in _NAMES)

HANDLERS += (FunctionTool({'type': 'function', 'function': {
    'name': 'send_file', 'description': '发送当前会话沙盒中的文件。支持任意格式，图片包括 GIF 原样发送，单个文件最多 100 MB。',
    'parameters': {'type': 'object', 'properties': {
        'path': {'type': 'string', 'description': '文件的相对路径或工作区内绝对路径。'},
        'name': {'type': 'string', 'description': '可选的显示文件名。'},
    }, 'required': ['path']}}}, enabled, partial(execute, name='send_file'),
    platform_neutral=True, required_features=frozenset()),)
