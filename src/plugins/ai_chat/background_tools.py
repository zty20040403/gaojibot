"""Unified host tool session for container CLI, run_code, and background Pi jobs."""
from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import httpx
from jsonschema import Draft202012Validator

from .message_ir import MessageBody
from .message_lowering import OutboundCapabilities
from .platform_types import ActorKey, InboundMessage, PlatformFeatures
from .tools import neutral_registry_for
from .tools.background import definition
from .tools.state import ToolContext, TurnState

SHARED_TOOL_NAMES = ('history', 'web_search', 'web_fetch', 'use_skill')


def workspace_owner(endpoint):
    return 'qqo:conversation:' + hashlib.sha256(endpoint.key.encode()).hexdigest()


class BackgroundPlatform:
    features = PlatformFeatures()
    capabilities = OutboundCapabilities(text=False)
    progress_enabled = stream_enabled = False

    def __init__(self, endpoint):
        self.endpoint, self.account = endpoint, endpoint.account


ASK_MAIN = definition(
    'ask_main',
    '向前台提问，让它根据聊天记录回答或询问用户。最多等两小时，但不超过任务剩余时间；没有答复时自己判断继续，并在报告里写明假设，任务截止则保存结果并结束。',
    {'question': {'type': 'string'}},
    ('question',),
)

GITHUB_TOOL = definition(
    'github',
    '由宿主代持凭据访问 GitHub REST API（api.github.com），密钥不进容器。',
    {
        'method': {'type': 'string', 'enum': ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']},
        'path': {'type': 'string', 'description': '以 / 开头的 API 路径，例如 /repos/owner/repo/issues'},
        'query': {'type': 'object', 'additionalProperties': {'type': ['string', 'number', 'boolean']}},
        'body': {'type': 'object'},
    },
    ('path',),
)

VM_ACTION_TOOLS = (
    definition(
        'say',
        '向当前会话发送一条简短消息（用于告知进度或中间结论）。',
        {
            'text': {'type': 'string'},
            'request_id': {'type': 'string'},
        },
        ('text',),
    ),
    definition(
        'send_file',
        '把容器 /work 下的文件发送到当前会话。',
        {
            'path': {'type': 'string'},
            'filename': {'type': 'string'},
            'request_id': {'type': 'string'},
        },
        ('path',),
    ),
    definition(
        'send_image',
        '把容器 /work 下的图片（PNG/JPEG/GIF）直接发到当前会话。',
        {
            'path': {'type': 'string'},
            'filename': {'type': 'string'},
            'request_id': {'type': 'string'},
        },
        ('path',),
    ),
    definition(
        'action_status',
        '查询一次 say / send_file / send_image 投递动作的真实状态。',
        {
            'request_id': {'type': 'string'},
            'action_id': {'type': 'string'},
        },
        (),
    ),
    definition(
        'agent_ask',
        '后台任务向前台提问（由容器内 gaoji agent ask 调用）。',
        {
            'question': {'type': 'string'},
            'job_id': {'type': 'integer'},
        },
        ('question',),
    ),
    definition(
        'units_keepalive',
        '登记容器内 /work/.gaoji/systemd 常驻单元数量（由 gaoji units apply 调用）。',
        {
            'units': {'type': 'integer', 'minimum': 0},
        },
        ('units',),
    ),
    definition(
        'units_wake',
        '后台命令或定时器向本会话前台发出完成/唤醒通知（由 gaoji units notify / gaoji wake 调用）。',
        {
            'text': {'type': 'string'},
            'unit': {'type': 'string'},
        },
        ('text',),
    ),
)


def build_shared_context(app, endpoint, *, inbound=None):
    if inbound is None:
        actor = (
            ActorKey(endpoint.account, 'c2c_user', '', endpoint.native_conversation_id)
            if endpoint.kind == 'private'
            else ActorKey(endpoint.account, 'group_member', endpoint.native_conversation_id, 'container')
        )
        inbound = InboundMessage(
            endpoint=endpoint,
            actor=actor,
            event_id=None,
            native_message_id='container',
            sender_display='container',
            platform_role='member',
            body=MessageBody(()),
            mentions_bot=False,
            reply_to_native_message_id=None,
            occurred_at=0,
            reply_credential=None,
        )
    capabilities = {'self', 'context'}
    if getattr(app.settings, 'search_enabled', True):
        capabilities.add('web')
    context = ToolContext(
        SimpleNamespace(context=app),
        inbound=inbound,
        platform=BackgroundPlatform(endpoint),
        capabilities=frozenset(capabilities),
    )
    registry = neutral_registry_for(context)
    tools = tuple(
        item for item in registry.definitions(context)
        if item['function']['name'] in SHARED_TOOL_NAMES
    )
    return context, TurnState(context), registry, tools


async def execute_github_api(arguments: dict) -> str:
    token = os.environ.get('AI_GITHUB_TOKEN', '').strip()
    if not token:
        return json.dumps({
            'ok': False,
            'error': 'github_token_not_configured',
            'message': '宿主尚未配置 AI_GITHUB_TOKEN；owner 配好后可直接通过 gaoji github 调用。',
        }, ensure_ascii=False)
    path = str(arguments.get('path') or '').strip()
    if not path.startswith('/') or path.startswith('//') or '..' in path or '?' in path or '#' in path:
        raise ValueError('GitHub API path 必须是以单斜杠开头的相对路径')
    method = str(arguments.get('method') or 'GET').upper()
    if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
        raise ValueError('不支持的 HTTP 方法')
    query = arguments.get('query')
    if query is not None and not isinstance(query, dict):
        raise ValueError('query 必须是对象')
    body = arguments.get('body')
    if body is not None and not isinstance(body, dict):
        raise ValueError('body 必须是对象')
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'gaojibot-host-relay',
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
        response = await client.request(
            method,
            'https://api.github.com' + path,
            params=query or None,
            json=body if method != 'GET' and body is not None else None,
            headers=headers,
        )
    text = response.text.replace(token, '[REDACTED]')
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = text[:4000]
    return json.dumps({
        'ok': 200 <= response.status_code < 300,
        'status': response.status_code,
        'data': data,
    }, ensure_ascii=False)


class BackgroundToolSession:
    """The host's tools for one job; the background agent runs commands and edits files in the container itself."""

    def __init__(self, app, inbound, *, prompt, question, current, ask_notify=None, units_handler=None, sandbox_id=None):
        self.app, self.inbound, self.prompt = app, inbound, prompt
        self.question, self.current = question, current
        self.ask_notify = ask_notify
        self.units_handler = units_handler
        self.sandbox_id = sandbox_id
        self.billing_context = None
        _, self.turn, self.registry, shared = build_shared_context(app, inbound.endpoint, inbound=inbound)
        self.tools = shared + (GITHUB_TOOL,)

    async def execute_tool(self, name, arguments):
        await self.current()
        if name == 'agent_ask':
            q = str((arguments or {}).get('question') or '').strip()
            if not q:
                raise ValueError('提问内容不能为空。')
            if self.ask_notify is not None:
                await self.ask_notify(q)
                return json.dumps({
                    'ok': True,
                    'said': '已向前台发出提问，回答到达后会作为新消息出现在对话里；若还有别的事可先继续做，若已无别的事可停下等待（最多等 10 分钟，超时请自行判断并在报告中写明假设）。',
                }, ensure_ascii=False)
            return json.dumps({'ok': True, 'answer': await self.question(q)}, ensure_ascii=False)
        if name in {'units_keepalive', 'units_wake'} and self.units_handler is not None:
            scope = SimpleNamespace(
                identity=SimpleNamespace(endpoint=self.inbound.endpoint, sandbox_id=self.sandbox_id or ''),
                current=self.current,
            )
            if name == 'units_keepalive':
                res = await self.units_handler.keepalive(scope, int((arguments or {}).get('units') or 0))
            else:
                res = await self.units_handler.wake(
                    scope, str((arguments or {}).get('text') or ''), unit=str((arguments or {}).get('unit') or '')
                )
            return json.dumps(res, ensure_ascii=False)
        allowed = {item['function']['name']: item['function']['parameters'] for item in self.tools}
        if name not in allowed:
            raise ValueError('后台工具不可用')
        if name in {'analyze_video', 'record_video_summary'}:
            return json.dumps(await self.video.execute(name, arguments), ensure_ascii=False)
        if name == 'github':
            Draft202012Validator(allowed[name]).validate(arguments)
            return await execute_github_api(arguments)
        if name == 'history' and not any(arguments.get(k) for k in ('query', 'who', 'from', 'to', 'around')):
            arguments = {**arguments, 'to': '#9223372036854775807'}
        from .billing_context import active_billing, billing_scope
        with billing_scope(self.billing_context or active_billing()):
            return await self.registry.execute(self.turn, name, arguments)

    async def close(self):
        # The conversation keeps its container and /work across independent jobs.
        pass


class ContainerToolSession:
    """Unified /job/tool session for container-scoped gvm_ credentials (gaoji CLI, shell, run_code, units)."""

    def __init__(self, app, scope, vm_http, *, ask_handler=None, units_handler=None):
        self.app, self.scope, self.vm_http = app, scope, vm_http
        self.ask_handler = ask_handler
        self.units_handler = units_handler
        self.endpoint = scope.identity.endpoint
        _, self.turn, self.registry, shared = build_shared_context(app, self.endpoint)
        self.tools = shared + (GITHUB_TOOL,) + VM_ACTION_TOOLS

    async def execute_tool(self, name, arguments):
        await self.scope.current()
        schemas = {item['function']['name']: item['function']['parameters'] for item in self.tools}
        if name not in schemas:
            raise ValueError(f'未知工具：{name}')
        if name == 'history' and not any(arguments.get(k) for k in ('query', 'who', 'from', 'to', 'around')):
            arguments = {**arguments, 'to': '#9223372036854775807'}
        if not Draft202012Validator(schemas[name]).is_valid(arguments):
            raise ValueError(f'工具 {name} 参数不合法')
        if name == 'github':
            return await execute_github_api(arguments)
        if name in {'say', 'send_file', 'send_image', 'action_status'}:
            result = await self.vm_http.execute_tool_action(self.scope, name, arguments)
            return json.dumps(result, ensure_ascii=False)
        if name == 'agent_ask':
            if self.ask_handler is None:
                raise RuntimeError('当前环境未启用后台提问通道')
            result = await self.ask_handler(self.endpoint, arguments)
            return json.dumps(result, ensure_ascii=False)
        if name == 'units_keepalive':
            if self.units_handler is None:
                raise RuntimeError('当前环境未启用常驻单元管理')
            result = await self.units_handler.keepalive(self.scope, int(arguments['units']))
            return json.dumps(result, ensure_ascii=False)
        if name == 'units_wake':
            if self.units_handler is None:
                raise RuntimeError('当前环境未启用唤醒通道')
            result = await self.units_handler.wake(self.scope, str(arguments['text']), unit=str(arguments.get('unit') or ''))
            return json.dumps(result, ensure_ascii=False)
        from .adapters.qq_official.billing import scoped
        with scoped(self.app, self.endpoint, 'semantic_index', current=self.scope.current):
            return await self.registry.execute(self.turn, name, arguments)
