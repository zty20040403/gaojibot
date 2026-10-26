"""Scoped long-term memory tools; group editing and provenance stay host-owned."""

from __future__ import annotations

import json

from ..ai_tools import MEMORY_ADD_TOOL, MEMORY_LIST_TOOL, MEMORY_REMOVE_TOOL
from ..long_term_memory import LongTermMemoryError
from .registry import FunctionTool
from .state import ToolContext, TurnState


def enabled(context: ToolContext) -> bool:
    return "memory" in context.capabilities and context.app.long_term_memory is not None


async def add(turn: TurnState, arguments: dict[str, object]) -> str:
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
    provenance = commands._memory_provenance(event)
    try:
        entry, created = turn.context.app.long_term_memory.add(
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
    commands, event = turn.context.services.commands, turn.context.event
    requested_scope = str(arguments.get("scope", "all")).strip().lower()
    if requested_scope not in {"user", "group", "all"}:
        requested_scope = "all"
    entries = turn.context.app.long_term_memory.list_entries(
        commands._memory_scope_keys(event, requested_scope)
    )
    return json.dumps(
        {
            "ok": True,
            "memories": [commands._memory_entry_payload(entry) for entry in entries],
        },
        ensure_ascii=False,
    )


async def remove(turn: TurnState, arguments: dict[str, object]) -> str:
    commands, event = turn.context.services.commands, turn.context.event
    try:
        memory_id = int(arguments.get("memory_id") or 0)
    except (TypeError, ValueError):
        memory_id = 0
    entry = commands._find_visible_memory(event, memory_id)
    if (
        entry is not None
        and entry.scope_type == "group"
        and not commands._can_edit_group_memory(event)
    ):
        return json.dumps(
            {"ok": False, "error": "你没有修改群记忆的权限。"}, ensure_ascii=False
        )
    removed = turn.context.app.long_term_memory.remove(
        memory_id,
        commands._memory_scope_keys(event),
        **commands._memory_provenance(event),
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
    FunctionTool(MEMORY_ADD_TOOL, enabled, add),
    FunctionTool(MEMORY_LIST_TOOL, enabled, list_entries),
    FunctionTool(MEMORY_REMOVE_TOOL, enabled, remove),
)
