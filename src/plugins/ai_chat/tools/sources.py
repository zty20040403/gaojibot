"""Read-only web search, installed skills and packaged source inspection."""

from __future__ import annotations

import json

from ..ai_tools import INSPECT_SOURCE_TOOL, USE_SKILL_TOOL, WEB_SEARCH_TOOL
from ..web_search import SearchError, search_freshness, search_web
from .registry import FunctionTool
from .state import ToolContext, TurnState


def self_tools_enabled(context: ToolContext) -> bool:
    return "self" in context.capabilities


def search_enabled(context: ToolContext) -> bool:
    return "web" in context.capabilities


async def use_skill(turn: TurnState, arguments: dict[str, object]) -> str:
    requested = str(arguments.get("name") or "").strip()
    skill = turn.context.app.skill_registry.get(requested)
    if skill is None:
        return json.dumps(
            {
                "ok": False,
                "error": "技能不存在。",
                "available": [
                    item.name for item in turn.context.app.skill_registry.list()
                ],
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "name": skill.name,
            "title": skill.title,
            "instructions": skill.body,
        },
        ensure_ascii=False,
    )


async def inspect_source(turn: TurnState, arguments: dict[str, object]) -> str:
    action = str(arguments.get("action") or "").strip()
    try:
        if action == "list":
            paths, truncated = turn.context.app.self_source.paths(
                str(arguments.get("path") or ""),
                limit=int(arguments.get("limit") or 100),
            )
            payload: dict[str, object] = {
                "ok": True,
                "paths": paths,
                "truncated": truncated,
            }
        elif action == "search":
            matches = turn.context.app.self_source.search(
                str(arguments.get("query") or ""),
                path_prefix=str(arguments.get("path") or ""),
                limit=int(arguments.get("limit") or 20),
            )
            payload = {
                "ok": True,
                "matches": [
                    {
                        "path": match.path,
                        "line": match.line,
                        "text": match.text,
                    }
                    for match in matches
                ],
            }
        elif action == "read":
            payload = {
                "ok": True,
                "slice": turn.context.app.self_source.read(
                    str(arguments.get("path") or ""),
                    start_line=int(arguments.get("start_line") or 1),
                    end_line=int(arguments.get("end_line") or 120),
                ),
            }
        elif action == "identity":
            payload = {"ok": True, "snapshot": turn.context.app.self_source.identity()}
        else:
            payload = {"ok": False, "error": "未知的源码自查动作。"}
    except (OSError, TypeError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
    return json.dumps(payload, ensure_ascii=False)


async def web_search(turn: TurnState, arguments: dict[str, object]) -> str:
    query = str(arguments.get("query", "")).strip() or turn.user_text
    query = query[: turn.context.app.settings.max_input_chars]
    requested_freshness = {
        "day": "d",
        "week": "w",
        "month": "m",
        "year": "y",
    }.get(str(arguments.get("freshness", "auto")))
    freshness = search_freshness(query) or requested_freshness
    try:
        results = await search_web(
            query,
            max_results=turn.context.app.settings.search_max_results,
            timeout_seconds=turn.context.app.settings.search_timeout_seconds,
            freshness=freshness,
        )
    except SearchError as exc:
        turn.context.app.logger.warning(f"Web search tool failed: {exc}")
        return json.dumps(
            {"ok": False, "error": "联网搜索暂时失败。"},
            ensure_ascii=False,
        )

    known_urls = {result.url for result in turn.search_results}
    turn.search_results.extend(
        result for result in results if result.url not in known_urls
    )
    return json.dumps(
        {
            "ok": True,
            "query": query,
            "freshness": freshness or "all",
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                }
                for result in results
            ],
        },
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(USE_SKILL_TOOL, self_tools_enabled, use_skill, platform_neutral=True),
    FunctionTool(INSPECT_SOURCE_TOOL, self_tools_enabled, inspect_source, platform_neutral=True),
    FunctionTool(WEB_SEARCH_TOOL, search_enabled, web_search, platform_neutral=True),
)
