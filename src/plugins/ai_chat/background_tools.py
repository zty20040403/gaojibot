"""Host tools for background jobs; the job's own commands run in the conversation VM."""
import hashlib
import json
from types import SimpleNamespace

from .message_lowering import OutboundCapabilities
from .platform_types import PlatformFeatures
from .tools import neutral_registry_for
from .tools.background import definition
from .tools.state import ToolContext, TurnState


def workspace_owner(endpoint):
    return 'qqo:conversation:' + hashlib.sha256(endpoint.key.encode()).hexdigest()


class BackgroundPlatform:
    features = PlatformFeatures()
    capabilities = OutboundCapabilities(text=False)
    progress_enabled = stream_enabled = False

    def __init__(self, endpoint):
        self.endpoint, self.account = endpoint, endpoint.account


ASK_MAIN = definition('ask_main', '向前台提问，让它根据聊天记录回答或询问用户。最多等十分钟；没有答复时自己判断继续，并在报告里写明假设。',
    {'question': {'type': 'string'}}, ('question',))


class BackgroundToolSession:
    """The host's tools for one job; Codex runs commands and edits files in the VM itself."""

    def __init__(self, app, inbound, *, prompt, question, current):
        self.app, self.inbound, self.prompt = app, inbound, prompt
        self.question, self.current = question, current
        capabilities = {'self', 'context'}
        if app.settings.search_enabled:
            capabilities.add('web')
        context = ToolContext(SimpleNamespace(context=app), inbound=inbound,
            platform=BackgroundPlatform(inbound.endpoint), capabilities=frozenset(capabilities))
        self.turn = TurnState(context)
        self.registry = neutral_registry_for(context)
        self.tools = tuple(item for item in self.registry.definitions(context)
            if item['function']['name'] in {'history', 'web_search', 'web_fetch'}) + (ASK_MAIN,)

    async def execute_tool(self, name, arguments):
        await self.current()
        if name not in {item['function']['name'] for item in self.tools}:
            raise ValueError('后台工具不可用')
        if name == 'ops_read':
            from .ops_read import read
            return await read(self.app, self.inbound, arguments)
        if name == 'ask_main':
            return json.dumps({'answer': await self.question(str(arguments['question']))}, ensure_ascii=False)
        return await self.registry.execute(self.turn, name, arguments)

    async def close(self):
        # The conversation keeps its VM and /work across independent jobs.
        pass
