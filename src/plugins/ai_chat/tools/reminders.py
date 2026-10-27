"""Scoped durable reminder tools; scheduling remains in ReminderStore."""

from __future__ import annotations

import json
from functools import partial

from ..ai_tools import (
    REMINDER_SET_TOOL,
    REMINDER_LIST_TOOL,
    REMINDER_CANCEL_TOOL,
    REMINDER_LIST_TOOL_NAME,
    REMINDER_CANCEL_TOOL_NAME,
)
from ..onebot_codec import scope_from_event
from .registry import FunctionTool
from .state import ToolContext, TurnState


def enabled(context: ToolContext) -> bool:
    return (
        "reminders" in context.capabilities and context.app.reminder_store is not None
    )


async def execute(turn: TurnState, arguments: dict[str, object], *, name: str) -> str:
    if turn.context.app.reminder_store is None:
        return json.dumps(
            {"ok": False, "error": "持久提醒功能暂时不可用。"},
            ensure_ascii=False,
        )
    scope = scope_from_event(turn.context.event)
    if name == REMINDER_LIST_TOOL_NAME:
        return json.dumps(
            {
                "ok": True,
                "reminders": [
                    turn.context.services.commands._reminder_payload(item)
                    for item in (
                        await turn.context.app.db_call(
                            turn.context.app.reminder_store.list_pending, scope
                        )
                    )
                ],
            },
            ensure_ascii=False,
        )
    if name == REMINDER_CANCEL_TOOL_NAME:
        reminder_id = turn.context.services.commands._reminder_id(
            arguments.get("reminder_handle")
        )
        if reminder_id is None:
            return json.dumps(
                {"ok": False, "error": "reminder_handle 格式无效。"},
                ensure_ascii=False,
            )
        removed = await turn.context.app.db_call(
            turn.context.app.reminder_store.cancel, scope, reminder_id
        )
        return json.dumps(
            {
                "ok": removed,
                "handle": f"reminder#{reminder_id}",
                "cancelled": removed,
                "error": None if removed else "当前会话没有这个待触发提醒。",
            },
            ensure_ascii=False,
        )
    try:
        due_at = turn.context.services.commands._parse_reminder_due_at(
            arguments.get("due_at")
        )
        reminder = await turn.context.app.db_call(
            turn.context.app.reminder_store.create,
            scope,
            creator_native_user_id=turn.context.event.user_id,
            creator_principal_id=(
                (
                    await turn.context.app.db_call(
                        turn.context.app.message_ledger.principal_id_for_native,
                        scope.platform,
                        turn.context.event.user_id,
                    )
                )
                if turn.context.app.message_ledger is not None
                else None
            ),
            message=str(arguments.get("message") or ""),
            scheduled_for=due_at,
        )
    except ValueError as exc:
        return json.dumps(
            {"ok": False, "error": str(exc)},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "reminder": turn.context.services.commands._reminder_payload(reminder),
        },
        ensure_ascii=False,
    )


HANDLERS = tuple(
    FunctionTool(
        definition, enabled, partial(execute, name=definition["function"]["name"])
    )
    for definition in (REMINDER_SET_TOOL, REMINDER_LIST_TOOL, REMINDER_CANCEL_TOOL)
)
