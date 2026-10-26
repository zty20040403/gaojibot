"""Per-turn delegation limits and coordinator dispatch entrypoints."""

from __future__ import annotations

import json

from ..ai_tools import DELEGATE_AGENT_TOOL, RUN_SUBAGENTS_TOOL, RESUME_SUBAGENT_TOOL
from .registry import FunctionTool
from .state import ToolContext, TurnState


def enabled(context: ToolContext) -> bool:
    return "subagents" in context.capabilities


async def delegate(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.subagent_delegations >= 3:
        return json.dumps(
            {
                "ok": False,
                "error": "本轮单专家委派已达到 3 次；更多协作请使用 run_subagents。",
            },
            ensure_ascii=False,
        )
    role = str(arguments.get("role") or "").strip()
    objective = str(arguments.get("objective") or "").strip()
    if not objective:
        return json.dumps(
            {"ok": False, "error": "委派目标不能为空。"},
            ensure_ascii=False,
        )
    if turn.context.app.subagent_coordinator is None or turn.subagent_actions is None:
        return json.dumps(
            {"ok": False, "error": "Sub-Agent 任务模式暂时没有开启。"},
            ensure_ascii=False,
        )
    turn.subagent_delegations += 1
    try:
        result = await turn.subagent_actions.delegate(role, objective)
    except ValueError as exc:
        return json.dumps(
            {"ok": False, "error": str(exc)},
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, **result}, ensure_ascii=False)


async def run(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.subagent_task_started:
        return json.dumps(
            {
                "ok": False,
                "error": "本轮已经启动过一次 Sub-Agent 任务，请使用现有结果。",
            },
            ensure_ascii=False,
        )
    goal = str(arguments.get("goal") or "").strip()
    if not goal:
        return json.dumps(
            {"ok": False, "error": "Sub-Agent 任务目标不能为空。"},
            ensure_ascii=False,
        )
    if turn.context.app.subagent_coordinator is None or turn.subagent_actions is None:
        return json.dumps(
            {"ok": False, "error": "Sub-Agent 任务模式暂时没有开启。"},
            ensure_ascii=False,
        )
    turn.subagent_task_started = True
    result = await turn.subagent_actions.run(goal)
    return json.dumps(
        {"ok": True, "result": result},
        ensure_ascii=False,
    )


async def resume(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.subagent_task_started:
        return json.dumps(
            {"ok": False, "error": "本轮已经启动或恢复过一个 Sub-Agent 任务。"},
            ensure_ascii=False,
        )
    try:
        task_id = int(arguments.get("task_id") or 0)
    except (TypeError, ValueError):
        task_id = 0
    if task_id <= 0:
        return json.dumps(
            {"ok": False, "error": "task_id 必须是正整数。"},
            ensure_ascii=False,
        )
    if turn.context.app.subagent_coordinator is None or turn.subagent_actions is None:
        return json.dumps(
            {"ok": False, "error": "Sub-Agent 任务模式暂时没有开启。"},
            ensure_ascii=False,
        )
    turn.subagent_task_started = True
    try:
        result = await turn.subagent_actions.resume(task_id)
    except (RuntimeError, ValueError) as exc:
        return json.dumps(
            {"ok": False, "error": str(exc)},
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, "result": result}, ensure_ascii=False)


HANDLERS = (
    FunctionTool(DELEGATE_AGENT_TOOL, enabled, delegate),
    FunctionTool(RUN_SUBAGENTS_TOOL, enabled, run),
    FunctionTool(RESUME_SUBAGENT_TOOL, enabled, resume),
)
