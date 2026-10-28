"""Scoped long-term memory tools; group editing and provenance stay host-owned."""

from __future__ import annotations

import json
import re

from ..ai_tools import MEMORY_ADD_TOOL, MEMORY_LIST_TOOL, MEMORY_REMOVE_TOOL
from ..long_term_memory import LongTermMemoryError
from ..memory_scopes import memory_scopes_for_inbound
from ..platform_types import InboundMessage
from .registry import FunctionTool
from .state import ToolContext, TurnState


def enabled(context: ToolContext) -> bool:
    return (supports_context(context) and "memory" in context.capabilities
            and context.app.long_term_memory is not None)


def supports_context(context: ToolContext) -> bool:
    """Only the migrated official path and existing OneBot path are implemented."""
    if context.inbound is None:
        return context.platform is None and context.event is not None
    if context.inbound.endpoint.account.platform == "onebot-v11":
        return context.event is not None
    return (context.inbound.endpoint.account.platform == "qq-official" and context.platform is not None
            and context.platform.account == context.inbound.endpoint.account)


def official_inbound(context: ToolContext) -> InboundMessage | None:
    inbound = context.inbound
    if inbound is None or inbound.endpoint.account.platform == "onebot-v11":
        return None
    if (inbound.endpoint.account.platform != "qq-official" or context.platform is None
            or context.platform.account != inbound.endpoint.account):
        raise ValueError("structured memory requires its actual supported platform")
    return inbound


def memory_scope_keys(context: ToolContext, requested: str = "all") -> list[str]:
    inbound = official_inbound(context)
    if inbound is None:
        if requested == "all":
            return context.services.commands._memory_scope_keys(context.event)
        return context.services.commands._memory_scope_keys(context.event, requested)
    group, user = memory_scopes_for_inbound(inbound)
    if requested == "user":
        return [user]
    if requested == "group":
        return [group] if group is not None else []
    return [scope for scope in (group, user) if scope is not None]


def memory_entry_payload(context: ToolContext, entry) -> dict[str, object]:
    if official_inbound(context) is None:
        return context.services.commands._memory_entry_payload(entry)
    return {
        "id": entry.id, "scope": entry.scope_type, "content": entry.content,
        "version": entry.version,
        "source_message": f"msg#{entry.source_message_id}" if entry.source_message_id is not None else None,
        "created_at": entry.created_at,
    }


def official_memory_provenance(context: ToolContext) -> dict[str, object]:
    """Run through db_call; history routing never selects the mutation actor."""
    inbound = official_inbound(context)
    if inbound is None:
        raise ValueError("official memory provenance requires an official inbound")
    ledger = context.app.message_ledger
    principal_id, source_message_id = 0, None
    if ledger is not None:
        principal_id = ledger.ensure_actor_identity(
            inbound.actor, inbound.sender_display, seen_at=inbound.occurred_at,
        )
        source_message_id = ledger.canonical_id_for_native(
            inbound.scope, inbound.native_message_id, endpoint=inbound.endpoint,
        )
        if source_message_id is not None:
            source = ledger.get_any_in_scope(inbound.scope, source_message_id)
            if source is None or source.sender_actor_key != inbound.actor.key:
                source_message_id = None
    return {"actor_user_id": None, "actor_key": inbound.actor.key,
            "actor_principal_id": principal_id, "source_message_id": source_message_id}


def _error(message: str, *, code: str = "invalid_arguments") -> str:
    return json.dumps({"ok": False, "error": message, "code": code}, ensure_ascii=False)


def _looks_like_secret(content: str) -> bool:
    return bool(re.search(r"\bsk-[A-Za-z0-9_-]{10,}\b", content) or re.search(
        r"(?i)(?:api[_ -]?key|access[_ -]?token|password|secret|密码|验证码)"
        r"\s*[:=：]\s*\S+", content,
    ))


async def _official_add(turn: TurnState, arguments: dict[str, object]) -> str:
    if set(arguments) - {"scope", "content"}:
        return _error("不能指定其他账号、身份或记忆地址。")
    scope_type = str(arguments.get("scope", "user")).strip().lower()
    if scope_type == "group":
        # Platform role is descriptive metadata, never local administration.
        return _error("当前平台尚未授予修改群记忆的本地权限。", code="memory_permission_denied")
    if scope_type != "user":
        return _error("记忆范围必须是 user 或 group。")
    content = str(arguments.get("content", "")).strip()
    if _looks_like_secret(content):
        return _error("检测到可能的密码、Token 或密钥，拒绝保存。")
    context = turn.context
    provenance = await context.app.db_call(official_memory_provenance, context)
    try:
        entry, created = await context.app.db_call(
            context.app.long_term_memory.add, memory_scope_keys(context, "user")[0], "user", content,
            creator_user_id=None, creator_actor_key=provenance["actor_key"],
            creator_principal_id=provenance["actor_principal_id"],
            source_message_id=provenance["source_message_id"], reason="model memory_add tool",
        )
    except LongTermMemoryError as exc:
        return _error(str(exc))
    return json.dumps({"ok": True, "created": created,
                       "memory": memory_entry_payload(context, entry)}, ensure_ascii=False)


async def _official_list(turn: TurnState, arguments: dict[str, object]) -> str:
    if set(arguments) - {"scope"}:
        return _error("不能指定其他账号、身份或记忆地址。")
    requested = str(arguments.get("scope", "all")).strip().lower()
    if requested not in {"user", "group", "all"}:
        return _error("记忆范围必须是 user、group 或 all。")
    entries = await turn.context.app.db_call(
        turn.context.app.long_term_memory.list_entries, memory_scope_keys(turn.context, requested),
    )
    return json.dumps({"ok": True, "memories": [memory_entry_payload(turn.context, entry) for entry in entries]},
                      ensure_ascii=False)


async def _official_remove(turn: TurnState, arguments: dict[str, object]) -> str:
    if set(arguments) - {"memory_id"}:
        return _error("不能指定其他账号、身份或记忆地址。")
    raw_id = arguments.get("memory_id")
    if type(raw_id) is not int or raw_id <= 0:
        return _error("memory_id 必须是正整数。")
    context = turn.context
    entries = await context.app.db_call(context.app.long_term_memory.list_entries, memory_scope_keys(context))
    entry = next((item for item in entries if item.id == raw_id), None)
    if entry is None:
        return _error("当前会话中找不到这条记忆。", code="memory_not_found")
    if entry.scope_type != "user" or entry.scope_key != context.inbound.actor.key:
        return _error("当前平台尚未授予修改群记忆的本地权限。", code="memory_permission_denied")
    removed = await context.app.db_call(
        context.app.long_term_memory.remove, raw_id, memory_scope_keys(context, "user"),
        **(await context.app.db_call(official_memory_provenance, context)),
        reason="model memory_remove tool",
    )
    return json.dumps({"ok": removed, "memory_id": raw_id,
                       "error": None if removed else "当前会话中找不到这条记忆。"}, ensure_ascii=False)


async def add(turn: TurnState, arguments: dict[str, object]) -> str:
    if official_inbound(turn.context) is not None:
        return await _official_add(turn, arguments)
    commands, event = turn.context.services.commands, turn.context.event
    scope_type = str(arguments.get("scope", "user")).strip().lower()
    content = str(arguments.get("content", "")).strip()
    group_scope, user_scope = commands._memory_scopes(event)
    if scope_type == "group":
        if group_scope is None:
            return json.dumps(
                {"ok": False, "error": "私聊中没有群记忆范围。"}, ensure_ascii=False
            )
        if not commands._can_edit_group_memory(event):
            return json.dumps(
                {"ok": False, "error": "只有群管理员或机器人授权用户可以修改群记忆。"},
                ensure_ascii=False,
            )
        scope_key = group_scope
    elif scope_type == "user":
        scope_key = user_scope
    else:
        return json.dumps(
            {"ok": False, "error": "记忆范围必须是 user 或 group。"}, ensure_ascii=False
        )
    if commands._looks_like_secret(content):
        return json.dumps(
            {"ok": False, "error": "检测到可能的密码、Token 或密钥，拒绝保存。"},
            ensure_ascii=False,
        )
    provenance = await turn.context.app.db_call(commands._memory_provenance, event)
    try:
        entry, created = await turn.context.app.db_call(
            turn.context.app.long_term_memory.add,
            scope_key,
            scope_type,
            content,
            creator_user_id=event.user_id,
            creator_principal_id=int(provenance["actor_principal_id"] or 0),
            source_message_id=provenance["source_message_id"],
            reason="model memory_add tool",
        )
    except LongTermMemoryError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    return json.dumps(
        {
            "ok": True,
            "created": created,
            "memory": commands._memory_entry_payload(entry),
        },
        ensure_ascii=False,
    )


async def list_entries(turn: TurnState, arguments: dict[str, object]) -> str:
    if official_inbound(turn.context) is not None:
        return await _official_list(turn, arguments)
    commands, event = turn.context.services.commands, turn.context.event
    requested_scope = str(arguments.get("scope", "all")).strip().lower()
    if requested_scope not in {"user", "group", "all"}:
        requested_scope = "all"
    entries = await turn.context.app.db_call(
        turn.context.app.long_term_memory.list_entries,
        commands._memory_scope_keys(event, requested_scope),
    )
    return json.dumps(
        {
            "ok": True,
            "memories": [commands._memory_entry_payload(entry) for entry in entries],
        },
        ensure_ascii=False,
    )


async def remove(turn: TurnState, arguments: dict[str, object]) -> str:
    if official_inbound(turn.context) is not None:
        return await _official_remove(turn, arguments)
    commands, event = turn.context.services.commands, turn.context.event
    try:
        memory_id = int(arguments.get("memory_id") or 0)
    except (TypeError, ValueError):
        memory_id = 0
    entry = await turn.context.app.db_call(
        commands._find_visible_memory, event, memory_id
    )
    if (
        entry is not None
        and entry.scope_type == "group"
        and not commands._can_edit_group_memory(event)
    ):
        return json.dumps(
            {"ok": False, "error": "你没有修改群记忆的权限。"}, ensure_ascii=False
        )
    removed = await turn.context.app.db_call(
        turn.context.app.long_term_memory.remove,
        memory_id,
        commands._memory_scope_keys(event),
        **(await turn.context.app.db_call(commands._memory_provenance, event)),
        reason="model memory_remove tool",
    )
    return json.dumps(
        {
            "ok": removed,
            "memory_id": memory_id,
            "error": None if removed else "当前会话中找不到这条记忆。",
        },
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(MEMORY_ADD_TOOL, enabled, add, platform_neutral=True),
    FunctionTool(MEMORY_LIST_TOOL, enabled, list_entries, platform_neutral=True),
    FunctionTool(MEMORY_REMOVE_TOOL, enabled, remove, platform_neutral=True),
)
