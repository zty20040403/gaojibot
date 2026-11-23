"""/bgmodel: the background model chain and effort, the same settings the console edits.

Anyone may read them; only the host owner may change them.
"""
import re

from ...application.commands import CommandResult
from ...pi_engine import EFFORTS, parse_model_chain

USAGE = ('用法：/bgmodel 查看；/bgmodel <模型1>,<模型2> 设置模型链（按顺序回退）；'
         '/bgmodel effort <强度> 设置推理强度；/bgmodel default 恢复宿主配置')
_EFFORT_WORDS = frozenset({'effort'})
_RESETS = frozenset({'default', 'reset'})


def _describe(settings) -> str:
    _, effort = settings.get()
    chain = settings.chain()
    return (f'后台模型链：{" → ".join(chain)}\n推理强度：{effort}（Gemini 4 的档位由型号决定，不受此影响）\n'
            f'可选强度：{"/".join(EFFORTS)}')


def command(runtime, life, app, agents):
    settings = agents.model_settings
    catalog = app.model_catalog

    async def invoke(inbound, request):
        words = request.argument.strip()

        def is_owner():
            with life.effect_scope(inbound) as cursor:
                return runtime.store.authority.owner_for_actor(cursor, inbound.endpoint, inbound.actor) is not None

        if not words:
            return CommandResult('ok', await runtime._calls(lambda: _describe(settings)))
        if not await runtime._calls(is_owner):
            return CommandResult('denied', '后台模型只有宿主 owner 能修改，其他人只能查看。')
        head, _, rest = words.partition(' ')
        if head.casefold() in _RESETS and not rest.strip():
            def reset():
                settings.reset()
                return '已恢复宿主配置。\n' + _describe(settings)
            return CommandResult('ok', await runtime._calls(reset))
        if head.casefold() in _EFFORT_WORDS:
            effort = rest.strip().casefold()
            if effort not in EFFORTS:
                return CommandResult('syntax', '推理强度只能是 ' + '/'.join(EFFORTS) + '。')

            def set_effort():
                settings.set(', '.join(settings.chain()), effort)
                return '已设置后台推理强度。\n' + _describe(settings)
            return CommandResult('ok', await runtime._calls(set_effort))
        names = [item for item in re.split(r'[,，\s]+', words) if item]
        # Aliases (gemini4, gpt62, ...) become the profile they name; other
        # entries such as rewind/... pass through to the default upstream.
        resolved = []
        for name in names:
            profile = catalog.try_resolve(name)
            resolved.append(profile.name if profile is not None else name)
        try:
            parse_model_chain(', '.join(resolved))
        except ValueError:
            return CommandResult('syntax', '模型链最多 8 个，每个不超过 100 字。\n' + USAGE)

        def set_chain():
            settings.set(', '.join(resolved), settings.get()[1])
            return '已设置后台模型链。\n' + _describe(settings)
        return CommandResult('ok', await runtime._calls(set_chain))
    return invoke
