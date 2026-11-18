"""Per-conversation /persona command: group owner, group admin, or host owner may change; default is tomboy PERSONA."""
from __future__ import annotations

from ...application.commands import CommandResult
from ...prompt import PERSONA
from .message_roles import source_role

MAX_PERSONA_CHARS = 2000
_PERSONA_RESETS = frozenset({"default", "reset", "clear", "默认", "重置", "清空"})


def command(runtime, life, app):
    async def invoke(inbound, request):
        text = (request.argument or "").strip()

        def execute():
            with life.effect_scope(inbound) as cursor:
                scope = inbound.endpoint.scope
                store = app.model_preferences
                if not text:
                    current = store.get_scope_persona(scope) if store is not None else None
                    if current:
                        return CommandResult("ok", f"当前会话人设（自定义）：\n{current}")
                    default_p = (app.settings.system_prompt or PERSONA).strip()
                    return CommandResult("ok", f"当前会话人设：默认\n{default_p}")
                if store is None:
                    return CommandResult("unavailable", "人设存储尚未配置。")
                now = int(runtime.store.clock())
                role = source_role(cursor, inbound.endpoint, inbound.actor, life.handle.canonical_message_id, now=now)
                owner = runtime.store.authority.owner_for_actor(cursor, inbound.endpoint, inbound.actor)
                if inbound.endpoint.kind != "private" and role not in {"owner", "admin"} and owner is None:
                    return CommandResult("denied", "只有本群群主、管理员或宿主 owner 能改本群人设；普通成员可用 /persona 查看。")
                if text.casefold() in _PERSONA_RESETS:
                    store.clear_scope_persona(scope)
                    return CommandResult("ok", "已恢复默认人设（毒舌假小子），下一轮回复生效。")
                if len(text) > MAX_PERSONA_CHARS:
                    return CommandResult("syntax", f"人设最长 {MAX_PERSONA_CHARS} 个字符，请精简后再试。")
                store.set_scope_persona(scope, text)
                return CommandResult("ok", "已更新本会话人设，下一轮回复生效。")

        return await runtime._calls(execute)

    return invoke
