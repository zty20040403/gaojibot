"""Scoped history, chapters, pins and group identity tools."""

from __future__ import annotations

import json
from functools import partial

import httpx
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.exception import NetworkError

from ..ai_tools import (
    CONTEXT_EXPAND_TOOL,
    CONTEXT_SEARCH_TOOL,
    GROUP_MEMBERS_TOOL,
    PIN_MESSAGE_TOOL,
    UNPIN_MESSAGE_TOOL,
    UNPIN_MESSAGE_TOOL_NAME,
)
from ..onebot_codec import scope_from_event
from ..onebot_model_output import decode_group_members
from .registry import FunctionTool
from .state import ToolContext, TurnState


def history_enabled(context: ToolContext) -> bool:
    return "context" in context.capabilities


def pins_enabled(context: ToolContext) -> bool:
    return (
        "pins" in context.capabilities
        and context.app.pin_store is not None
        and context.app.message_ledger is not None
    )


def members_enabled(context: ToolContext) -> bool:
    return "members" in context.capabilities and isinstance(
        context.event, GroupMessageEvent
    )


async def change_pin(
    turn: TurnState, arguments: dict[str, object], *, name: str
) -> str:
    if turn.context.app.pin_store is None or turn.context.app.message_ledger is None:
        return json.dumps(
            {"ok": False, "error": "固定消息存储暂时不可用。"},
            ensure_ascii=False,
        )
    message_id = turn.context.services.commands._canonical_message_id(
        arguments.get("message_handle")
    )
    if message_id is None:
        return json.dumps(
            {"ok": False, "error": "message_handle 格式无效。"},
            ensure_ascii=False,
        )
    scope = scope_from_event(turn.context.event)
    if name == UNPIN_MESSAGE_TOOL_NAME:
        removed = await turn.context.app.db_call(
            turn.context.app.pin_store.unpin, scope, message_id
        )
        return json.dumps(
            {
                "ok": removed,
                "message_handle": f"msg#{message_id}",
                "removed": removed,
                "error": None if removed else "这条消息没有被固定。",
            },
            ensure_ascii=False,
        )
    principal_id = await turn.context.app.db_call(
        turn.context.app.message_ledger.principal_id_for_native,
        scope.platform,
        turn.context.event.user_id,
    )
    try:
        pinned, created = await turn.context.app.db_call(
            turn.context.app.pin_store.pin,
            turn.context.app.message_ledger,
            scope,
            message_id,
            pinned_by_principal_id=principal_id,
        )
    except ValueError as exc:
        return json.dumps(
            {"ok": False, "error": str(exc)},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "message_handle": f"msg#{pinned.canonical_message_id}",
            "created": created,
        },
        ensure_ascii=False,
    )


async def group_members(turn: TurnState, arguments: dict[str, object]) -> str:
    if not isinstance(turn.context.event, GroupMessageEvent):
        return json.dumps(
            {"ok": False, "error": "私聊中没有群成员名单。"},
            ensure_ascii=False,
        )
    query = str(arguments.get("query") or "").strip().casefold()
    try:
        limit = min(max(int(arguments.get("limit") or 50), 1), 100)
        raw_members = await turn.bot.get_group_member_list(
            group_id=turn.context.event.group_id,
        )
    except (
        ActionFailed,
        NetworkError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        turn.context.app.logger.warning(f"Fetching the QQ group roster failed: {exc}")
        return json.dumps(
            {"ok": False, "error": "读取当前群成员失败。"},
            ensure_ascii=False,
        )
    decoded_members = decode_group_members(raw_members)
    principal_ids = (
        (
            await turn.context.app.db_call(
                turn.context.app.message_ledger.ensure_principal_identities,
                "onebot-v11",
                [(member.native_user_id, member.display) for member in decoded_members],
            )
        )
        if turn.context.app.message_ledger is not None
        else {}
    )
    members: list[dict[str, object]] = []
    for member in decoded_members:
        display = member.display
        if query and query not in display.casefold():
            continue
        principal_id = principal_ids.get(member.native_user_id)
        members.append(
            {
                "principal": (
                    f"[mention#{principal_id}]" if principal_id is not None else None
                ),
                "display_name": display,
                "role": member.role,
                "title": member.title or None,
            }
        )
        if len(members) >= limit:
            break
    return json.dumps(
        {"ok": True, "members": members, "count": len(members)},
        ensure_ascii=False,
    )


async def expand(turn: TurnState, arguments: dict[str, object]) -> str:
    target = str(arguments.get("target") or "").strip()
    if target.startswith("episode#"):
        if (
            turn.context.app.context_store is None
            or turn.context.app.message_ledger is None
        ):
            return json.dumps(
                {"ok": False, "error": "分层上下文暂时没有开启。"},
                ensure_ascii=False,
            )
        handle = target.removeprefix("episode#").strip()
        expanded_episode = await turn.context.app.db_call(
            turn.context.app.context_store.expand,
            turn.context.app.message_ledger,
            scope_from_event(turn.context.event),
            handle,
            max_chars=turn.context.app.settings.turn_expand_max_chars,
        )
        if expanded_episode is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "当前会话可见范围内找不到或无法验证这个 episode#。",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "target": f"episode#{handle}",
                "record": expanded_episode,
            },
            ensure_ascii=False,
        )
    if turn.context.app.turn_journal is None:
        return json.dumps(
            {"ok": False, "error": "Turn Journal 暂时没有开启。"},
            ensure_ascii=False,
        )
    try:
        requested_turn = int(
            target.removeprefix("t#")
            if target.startswith("t#")
            else arguments.get("turn_id") or 0
        )
    except (TypeError, ValueError):
        requested_turn = 0
    expanded = await turn.context.app.db_call(
        turn.context.app.turn_journal.render_turn,
        scope_from_event(turn.context.event),
        requested_turn,
        max_chars=turn.context.app.settings.turn_expand_max_chars,
    )
    if expanded is None:
        return json.dumps(
            {
                "ok": False,
                "error": "当前会话可见范围内找不到这个 t#。",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {"ok": True, "turn": f"t#{requested_turn}", "record": expanded},
        ensure_ascii=False,
    )


async def search(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.context.app.message_ledger is None:
        return json.dumps(
            {"ok": False, "error": "规范消息账本暂时没有开启。"},
            ensure_ascii=False,
        )
    query = str(arguments.get("query") or "").strip()
    try:
        limit = min(max(int(arguments.get("limit") or 5), 1), 10)
    except (TypeError, ValueError):
        limit = 5
    if not query:
        return json.dumps(
            {"ok": False, "error": "检索词不能为空。"},
            ensure_ascii=False,
        )
    scope = scope_from_event(turn.context.event)
    messages = await turn.context.app.db_call(
        turn.context.app.message_ledger.search_in_scope, scope, query, limit
    )
    episodes = (
        (
            await turn.context.app.db_call(
                turn.context.app.context_store.search, scope, query, limit=limit
            )
        )
        if turn.context.app.context_store is not None
        else []
    )
    pinned_matches = (
        (
            await turn.context.app.db_call(
                turn.context.app.pin_store.search,
                turn.context.app.message_ledger,
                scope,
                query,
                limit=limit,
            )
        )
        if turn.context.app.pin_store is not None
        else []
    )
    folded_query = query.casefold()
    memory_matches = [
        entry
        for entry in (
            await turn.context.app.db_call(
                turn.context.app.long_term_memory.list_entries,
                turn.context.services.commands._memory_scope_keys(turn.context.event),
            )
        )
        if folded_query in entry.content.casefold()
    ][:limit]
    semantic_hits = []
    allowed_context_scopes = {
        scope.key,
        *turn.context.services.commands._memory_scope_keys(turn.context.event),
    }
    if turn.context.app.semantic_recall is not None:
        try:
            semantic_hits = await turn.context.app.semantic_recall.search(
                sorted(allowed_context_scopes),
                query,
                limit=limit * 2,
            )
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            httpx.HTTPError,
        ) as exc:
            turn.context.app.logger.warning(f"Semantic recall failed softly: {exc}")
    lexical_handles = {
        *(f"msg#{message.canonical_message_id}" for message in messages),
        *(f"episode#{episode.expand_handle}" for episode in episodes),
        *(f"msg#{message.canonical_message_id}" for _pin, message in pinned_matches),
        *(f"memory#{entry.id}" for entry in memory_matches),
    }
    return json.dumps(
        {
            "ok": True,
            "messages": [
                {
                    "handle": f"msg#{message.canonical_message_id}",
                    "sender": (
                        f"[mention#{message.sender_principal_id}] {message.sender_display}"
                        if message.sender_principal_id is not None
                        else message.sender_display
                    ),
                    "text": message.rendered_text[:500],
                }
                for message in messages
            ],
            "episodes": [
                {
                    "handle": f"episode#{episode.expand_handle}",
                    "range": (
                        f"msg#{episode.start_message_id}..msg#{episode.end_message_id}"
                    ),
                    "summary": episode.summary_p2,
                }
                for episode in episodes
            ],
            "pins": [
                {
                    "handle": f"msg#{message.canonical_message_id}",
                    "sender": (
                        f"[mention#{message.sender_principal_id}] {message.sender_display}"
                        if message.sender_principal_id is not None
                        else message.sender_display
                    ),
                    "text": message.rendered_text[:500],
                }
                for _pin, message in pinned_matches
            ],
            "memories": [
                turn.context.services.commands._memory_entry_payload(entry)
                for entry in memory_matches
            ],
            "semantic": [
                {
                    "handle": hit.source_handle,
                    "type": hit.source_type,
                    "text": hit.content[:500],
                    "score": round(hit.score, 4),
                    "metadata": hit.metadata,
                }
                for hit in semantic_hits
                if hit.source_handle not in lexical_handles
                and str(hit.scope_key) in allowed_context_scopes
            ][:limit],
        },
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(CONTEXT_EXPAND_TOOL, history_enabled, expand),
    FunctionTool(CONTEXT_SEARCH_TOOL, history_enabled, search),
    FunctionTool(GROUP_MEMBERS_TOOL, members_enabled, group_members),
    FunctionTool(
        PIN_MESSAGE_TOOL,
        pins_enabled,
        partial(change_pin, name=PIN_MESSAGE_TOOL["function"]["name"]),
    ),
    FunctionTool(
        UNPIN_MESSAGE_TOOL,
        pins_enabled,
        partial(change_pin, name=UNPIN_MESSAGE_TOOL["function"]["name"]),
    ),
)
