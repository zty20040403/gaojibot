"""Per-group person impression cards (/work/.gaoji/self/people/*.md)."""

from __future__ import annotations

import json

from ..ai_tools import ToolDefinition
from ..background_tools import workspace_owner
from .registry import FunctionTool
from .state import ToolContext, TurnState, structured_tool_source

IMPRESSION_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "impression",
        "description": (
            "读取、更新或忘记当前会话里某位群友的印象卡（按群独立保存，绝不跨群串信息）。"
            "对方问“你记了我啥”用 action=read；说“忘掉我”用 action=forget；"
            "记下对某人的印象用 action=note（300-500字内：怎么称呼、擅长什么、聊过的梗、上次翻过的车、熟不熟）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "note", "forget"]},
                "who": {"type": "string", "maxLength": 50},
                "note": {"type": "string", "maxLength": 600},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def enabled(context: ToolContext) -> bool:
    return "memory" in context.capabilities and getattr(context.app, "self_files", None) is not None


def _result(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def impression(turn: TurnState, arguments: dict[str, object]) -> str:
    source = structured_tool_source(turn.context)
    if source is None:
        return _result({"ok": False, "error": "当前没有可操作的会话。"})
    store = getattr(turn.context.app, "self_files", None)
    if store is None:
        return _result({"ok": False, "error": "印象卡存储未启用。"})

    action = str(arguments.get("action") or "").strip()
    who = str(arguments.get("who") or "").strip()
    inbound = turn.context.inbound
    target_actor = inbound.actor.key if (not who or who == inbound.sender_display) else ""
    target_name = who or inbound.sender_display
    scope_key = source.scope.key

    if action == "read":
        card = store.read_person(scope_key, actor_key=target_actor, name=target_name)
        return _result({"ok": True, "who": target_name, "card": card or "（暂时还没记对这个人的印象。）"})

    owner = workspace_owner(inbound.endpoint)
    sandbox_manager = getattr(turn.context.app, "sandbox_manager", None)

    if action == "forget":
        if who and who != inbound.sender_display:
            target_actor = ""
        rel = store.forget_person(scope_key, actor_key=target_actor, name=target_name)
        if rel:
            store.schedule_sync(
                sandbox_manager,
                owner,
                deletes=[rel],
                commit_message=f"forget {target_name}",
            )
            return _result({"ok": True, "forgotten": True, "who": target_name})
        return _result({"ok": True, "forgotten": False, "who": target_name})

    if action == "note":
        note = str(arguments.get("note") or "").strip()
        if not target_actor:
            target_actor = f"name:{target_name}"
        try:
            rel, content = store.write_person(
                scope_key,
                actor_key=target_actor,
                display=target_name,
                body=note,
            )
        except ValueError as exc:
            return _result({"ok": False, "error": str(exc)})
        store.schedule_sync(
            sandbox_manager,
            owner,
            writes={rel: content},
            commit_message=f"impression {target_name}",
        )
        return _result({"ok": True, "who": target_name, "chars": len(content)})

    return _result({"ok": False, "error": "未知的 impression 操作。"})


HANDLERS = (
    FunctionTool(IMPRESSION_TOOL, enabled, impression, platform_neutral=True),
)
