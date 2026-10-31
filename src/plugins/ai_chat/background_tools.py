"""Background tools run commands in the conversation VM, never on the host."""
from dataclasses import asdict
import base64
import hashlib
import json
from types import SimpleNamespace

from .message_lowering import OutboundCapabilities
from .platform_types import PlatformFeatures
from .tools import neutral_registry_for
from .tools.background import definition
from .tools.state import ToolContext, TurnState
from .tool_images import ToolImage


def workspace_owner(endpoint):
    return 'qqo:conversation:' + hashlib.sha256(endpoint.key.encode()).hexdigest()


class BackgroundPlatform:
    features = PlatformFeatures()
    capabilities = OutboundCapabilities(text=False)
    progress_enabled = stream_enabled = False

    def __init__(self, endpoint):
        self.endpoint, self.account = endpoint, endpoint.account


VM_TOOLS = (
    definition('shell', '在当前会话的持久 Linux VM 执行命令，工作目录 /work。软件可直接安装；宿主凭据不会传入。',
        {'command': {'type': 'string'}, 'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300},
         'packages': {'type': 'array', 'items': {'type': 'string'}, 'description': '执行前安装的 Debian 软件包。'}}, ('command',)),
    definition('read_file', '读取当前 VM 的 UTF-8 文件。', {'path': {'type': 'string'}}, ('path',)),
    definition('write_file', '写入当前 VM 的 UTF-8 文件。',
        {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ('path', 'content')),
    definition('ask_main', '向前台提问，让它根据上下文回答或询问用户。最多等十分钟；没有答复时记录假设并继续。',
        {'question': {'type': 'string'}}, ('question',)),
    definition('say', '记录任务进展到报告中；不会直接向群里发送消息。', {'text': {'type': 'string'}}, ('text',)),
    definition('view_sandbox_image', '查看 /work 中的 PNG/JPEG，例如网页截图、图表、视频帧；图片会进入你的视觉上下文，不发送到群里。',
        {'path': {'type': 'string'}}, ('path',)),
)


class BackgroundToolSession:
    def __init__(self, app, inbound, *, profile, prompt, question, current):
        self.app, self.inbound, self.profile, self.prompt = app, inbound, profile, prompt
        self.question, self.current = question, current
        self.owner = workspace_owner(inbound.endpoint)
        self.sandbox_id = None
        capabilities = {'self', 'context'}
        if app.settings.search_enabled:
            capabilities.add('web')
        context = ToolContext(SimpleNamespace(context=app), inbound=inbound,
            platform=BackgroundPlatform(inbound.endpoint), capabilities=frozenset(capabilities))
        self.turn = TurnState(context)
        self.registry = neutral_registry_for(context)
        self.tools = tuple(item for item in self.registry.definitions(context)
            if item['function']['name'] in {'history', 'web_search', 'web_fetch', 'use_skill'}) + VM_TOOLS

    async def workspace(self):
        await self.current()
        if self.sandbox_id is None:
            item = await self.app.sandbox_manager.ensure_default(self.owner)
            self.sandbox_id = item['sandbox_id']
        return self.sandbox_id

    async def execute_tool(self, name, arguments):
        await self.current()
        if name == 'ops_read' and any(item['function']['name'] == name for item in self.tools):
            from .ops_read import read
            return await read(self.app, self.inbound, arguments)
        manager = self.app.sandbox_manager
        if name == 'view_sandbox_image':
            content = await manager.read_file(self.owner, await self.workspace(), str(arguments['path']), max_bytes=8*1024*1024)
            from .adapters.qq_official.image_formats import detect_image
            mime = detect_image(content, str(arguments['path']), max_bytes=8*1024*1024)
            return ToolImage('沙盒图片：' + str(arguments['path']), 'data:' + mime + ';base64,' + base64.b64encode(content).decode())
        if name in {'shell', 'read_file', 'write_file'}:
            sandbox = await self.workspace()
            if name == 'shell':
                result = await manager.exec(self.owner, sandbox, str(arguments['command']),
                    timeout_seconds=int(arguments.get('timeout_seconds', 300)), packages=arguments.get('packages'))
                return json.dumps(asdict(result), ensure_ascii=False)
            if name == 'read_file':
                result = await manager.read_file(self.owner, sandbox, str(arguments['path']))
                return json.dumps({'text': result.decode('utf-8', errors='replace')}, ensure_ascii=False)
            await manager.write_file(self.owner, sandbox, str(arguments['path']), str(arguments['content']).encode())
            return '{"ok":true}'
        if name == 'ask_main':
            return json.dumps({'answer': await self.question(str(arguments['question']))}, ensure_ascii=False)
        if name == 'say':
            return '{"ok":true,"recorded_in_report":true}'
        if name not in {item['function']['name'] for item in self.tools}:
            raise ValueError('后台工具不可用')
        return await self.registry.execute(self.turn, name, arguments)

    async def close(self):
        # The conversation keeps its VM and /work across independent jobs.
        pass
