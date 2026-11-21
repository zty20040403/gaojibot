"""Deliver the bearer link via the bound QQ port, not the model or guest."""
from ..adapters.qq_official.message_roles import source_role
from ..browser_takeover import current_url
from . import owned_sandbox, progress
from .registry import FunctionTool


def enabled(context):
    return (context.inbound is not None and context.inbound.endpoint.kind in {'private', 'group'}
            and owned_sandbox.enabled(context) and progress.enabled(context)
            and getattr(context.platform, 'execution', None) is not None)


def group_login_allowed(context):
    store = context.app.official_runtime.store
    source = context.inbound
    canonical = context.platform.execution.handle.canonical_message_id
    with store.transaction() as cursor:
        role = source_role(cursor, source.endpoint, source.actor, canonical, now=int(store.clock()))
        return role in {'owner', 'admin'} or store.authority.owner_for_actor(cursor, source.endpoint, source.actor) is not None


async def execute(turn, arguments):
    context = turn.context
    if not enabled(context):
        return '{"ok":false,"error":"当前会话不能接管浏览器。"}'
    executor = context.agent_executor
    settings = context.app.settings
    if not settings.admin_origin.startswith('https://'):
        return '{"ok":false,"error":"尚未配置接管页面的 HTTPS 入口。"}'
    group = context.inbound.endpoint.kind == 'group'
    if group and not await context.app.db_call(group_login_allowed, context):
        return '{"ok":false,"error":"群接管仅限宿主 owner、本群群主或管理员要求登录 bot 公共小号。"}'
    items = await executor._call(executor.sandbox_manager.list, executor.owner)
    if not items or not items[0]['status'].startswith('Up '):
        return '{"ok":false,"error":"请先在本会话打开要接管的网页。"}'
    sid = items[0]['sandbox_id']
    if await current_url(executor.sandbox_manager, sid) in (None, 'about:blank'):
        return '{"ok":false,"error":"浏览器未运行或当前是空白页，请先在本会话打开要接管的网页再发链接。"}'
    token = context.app.browser_takeovers.issue(executor.owner, sid)
    link = f'{settings.admin_origin}/browser-takeover#{token}'
    message = '这个登录全群可用。仅用于 bot 公共小号：\n' + link if group else link
    if len(message) > 200:  # say truncates progress messages to 200 characters.
        return '{"ok":false,"error":"接管链接超出消息长度限制。"}'
    return await progress.execute(turn, {'text': message})


HANDLERS = (FunctionTool({'type': 'function', 'function': {
    'name': 'browser_takeover', 'description': '向当前会话发送一次性浏览器接管链接（10 分钟），由用户亲自验证或扫码。群里只用于 owner、群主或管理员明确要求的 bot 公共小号；个人账号只在私聊。',
    'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
}}, enabled, execute, platform_neutral=True),)
