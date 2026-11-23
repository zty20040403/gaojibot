"""Issue a host-owner call link only through the bound private QQ sender."""
from . import progress
from .registry import FunctionTool


def enabled(context):
    if context.inbound is None or context.inbound.endpoint.kind != 'private' or not progress.enabled(context):
        return False
    settings = context.app.settings
    return bool(settings.voice_app_id and settings.voice_access_key) and settings.admin_origin.startswith('https://')


def is_owner(context):
    store, source = context.app.official_runtime.store, context.inbound
    with store.transaction() as cursor:
        return store.authority.owner_for_actor(cursor, source.endpoint, source.actor) is not None


async def execute(turn, arguments):
    context = turn.context
    if not await context.app.db_call(is_owner, context):
        return '{"ok":false,"error":"语音通话仅限宿主 owner 在私聊使用。"}'
    token = context.app.voice_calls.issue(context.inbound)
    link = f'{context.app.settings.admin_origin}/voice-call#{token}'
    if len(link) > 200:  # The QQ progress sender truncates at 200 characters.
        return '{"ok":false,"error":"通话链接超出消息长度限制。"}'
    return await progress.execute(turn, {'text': link})


HANDLERS = (FunctionTool({'type': 'function', 'function': {
    'name': 'voice_call', 'description': '宿主 owner 在私聊中要求打电话、实时语音聊天时，向当前私聊发送一次性通话链接（10 分钟内打开）。',
    'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
}}, enabled, execute, platform_neutral=True),)
