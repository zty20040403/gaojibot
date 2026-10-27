"""Creator-scoped routine management, shared by chat tools and slash commands."""

from __future__ import annotations

from datetime import datetime
from functools import partial
import json
from zoneinfo import ZoneInfo

from jsonschema import ValidationError, validate
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from ..ai_tools import ROUTINE_SET_TOOL, ROUTINE_LIST_TOOL, ROUTINE_CANCEL_TOOL
from ..onebot_codec import scope_from_event
from ..routines import Routine
from .registry import FunctionTool
from .state import ToolContext, TurnState

DEFINITIONS = (ROUTINE_SET_TOOL, ROUTINE_LIST_TOOL, ROUTINE_CANCEL_TOOL)


def enabled(context: ToolContext) -> bool:
    return (
        "routines" in context.capabilities
        and context.app.routine_store is not None
        and not context.routine_run_id
        and (
            not isinstance(context.event, GroupMessageEvent)
            or context.services.group_enabled(context.event.group_id)
        )
    )


def payload(routine: Routine) -> dict[str, object]:
    return {
        "handle": routine.handle,
        "name": routine.name,
        "prompt": routine.prompt,
        "cron": routine.cron,
        "timezone": routine.timezone,
        "enabled": routine.enabled,
        "version": routine.version,
        "next_due_at": datetime.fromtimestamp(
            routine.next_due_at, ZoneInfo(routine.timezone)
        ).isoformat(),
        "max_tokens": routine.max_tokens,
        "daily_tokens": routine.daily_tokens,
        "daily_runs": routine.daily_runs,
    }


async def execute(turn: TurnState, arguments: dict[str, object], *, name: str) -> str:
    context = turn.context
    scope = scope_from_event(context.event)
    store = context.app.routine_store
    definition = next(item for item in DEFINITIONS if item["function"]["name"] == name)
    try:
        validate(arguments, definition["function"]["parameters"])
    except ValidationError:
        return json.dumps(
            {"ok": False, "error": "例行任务参数不符合格式；请检查句柄、版本和预算。"},
            ensure_ascii=False,
        )
    try:
        if name == "routine_list":
            items = []
            for routine in await context.app.db_call(store.list_owned, scope):
                item = payload(routine)
                item["budget"] = await context.app.db_call(
                    store.usage.routine_status,
                    routine.routine_id,
                    scope_key=scope.key,
                    bot_id=scope.bot_native_user_id,
                    creator_id=scope.actor_native_user_id,
                )
                items.append(item)
            result = {"ok": True, "routines": items}
        elif name == "routine_cancel":
            routine_id = int(str(arguments["routine_handle"]).split("#")[1])
            cancelled = await context.app.db_call(
                store.cancel,
                scope,
                routine_id,
                expected_version=arguments["expected_version"],
            )
            result = {
                "ok": cancelled,
                "cancelled": cancelled,
                "error": None
                if cancelled
                else "当前会话没有这个由你创建的启用中任务。",
            }
        else:
            routine = await context.app.db_call(store.set, scope, **arguments)
            result = {"ok": True, "routine": payload(routine)}
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


HANDLERS = tuple(
    FunctionTool(
        definition, enabled, partial(execute, name=definition["function"]["name"])
    )
    for definition in DEFINITIONS
)
