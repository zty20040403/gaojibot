"""Read-only web search, installed skills and packaged source inspection."""

from __future__ import annotations

import json

import httpx

from ..ai_tools import INSPECT_SOURCE_TOOL, USE_SKILL_TOOL, WEB_FETCH_TOOL, WEB_SEARCH_TOOL
from ..web_fetch import BLOCKED_STATUSES, BROWSER_HEADERS, FetchError, fetch_page
from ..web_search import SearchError, SearchResult, clean_source_url, search_web
from .registry import FunctionTool
from .state import ToolContext, TurnState

PAGE_CHARS = 8000


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


# One reply may search this many times. On 11-18 a reply identifying a toy
# from a photo searched 12 times in a row (about a minute) for a name that
# had no page online yet; stop and answer with what is known instead.
MAX_SEARCHES_PER_REPLY = 4


async def web_search(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.web_searches >= MAX_SEARCHES_PER_REPLY:
        return json.dumps({
            "ok": False, "code": "search_limit",
            "error": f"这条回复已经搜了 {MAX_SEARCHES_PER_REPLY} 次。用已有结果回答；"
                     "查不到确切答案就说出最可能的情况，并说明不确定。",
        }, ensure_ascii=False)
    turn.web_searches += 1
    settings = turn.context.app.settings
    query = str(arguments.get("query", "")).strip() or turn.user_text
    query = query[: settings.max_input_chars]
    # "auto" means no time filter: a one-day window hides nearly every price,
    # spec and how-to page, so only an explicit choice narrows it.
    freshness = {
        "day": "d",
        "week": "w",
        "month": "m",
        "year": "y",
    }.get(str(arguments.get("freshness", "auto")))
    options = {
        "max_results": settings.search_max_results,
        "timeout_seconds": settings.search_timeout_seconds,
        "provider": settings.search_provider,
        "api_key": settings.search_api_key,
    }
    try:
        results = await search_web(query, freshness=freshness, **options)
        if not results and freshness is not None:
            results = await search_web(query, freshness=None, **options)
            freshness = None
    except SearchError as exc:
        turn.context.app.logger.warning(f"Web search tool failed: {exc}")
        return json.dumps(
            {"ok": False, "error": "联网搜索暂时失败。"},
            ensure_ascii=False,
        )

    if not results:
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "freshness": freshness or "all",
                "results": [],
                "note": "搜索引擎没有返回和这个词相关的结果，换一个说法再搜。",
            },
            ensure_ascii=False,
        )

    id_by_url = {result.url: index for index, result in enumerate(turn.search_results, start=1)}
    payload_results: list[dict[str, object]] = []
    for result in results:
        cleaned_url = clean_source_url(result.url)
        cleaned = SearchResult(result.title, cleaned_url, result.snippet)
        source_id = id_by_url.get(cleaned_url)
        if source_id is None:
            turn.search_results.append(cleaned)
            source_id = len(turn.search_results)
            id_by_url[cleaned_url] = source_id
        payload_results.append(
            {
                "id": source_id,
                "title": cleaned.title,
                "url": cleaned.url,
                "snippet": cleaned.snippet,
            }
        )
    return json.dumps(
        {
            "ok": True,
            "query": query,
            "freshness": freshness or "all",
            "results": payload_results,
        },
        ensure_ascii=False,
    )


async def web_fetch(turn: TurnState, arguments: dict[str, object]) -> str:
    url = str(arguments.get("url") or "").strip()
    try:
        offset = max(int(arguments.get("offset") or 0), 0)
        timeout = max(turn.context.app.settings.search_timeout_seconds, 15)
        try:
            page = await fetch_page(url, timeout_seconds=timeout)
        except FetchError as exc:
            if exc.status not in BLOCKED_STATUSES:
                raise
            # Some sites only reject the short header set: try once as a browser would.
            page = await fetch_page(url, timeout_seconds=timeout, headers=BROWSER_HEADERS)
    except (FetchError, ValueError) as exc:
        if isinstance(exc, FetchError) and exc.status in BLOCKED_STATUSES:
            cleaned_url = clean_source_url(url)
            for index, result in enumerate(turn.search_results, start=1):
                if result.url == cleaned_url and result.snippet:
                    return json.dumps({
                        "ok": True, "partial": True, "id": index, "url": cleaned_url,
                        "title": result.title, "text": result.snippet,
                        "note": "网页拒绝访问，这里只有搜索摘要。",
                    }, ensure_ascii=False)
        return json.dumps({"ok": False, "url": url, "error": str(exc)}, ensure_ascii=False)
    except httpx.HTTPError as exc:
        turn.context.app.logger.warning(f"Web fetch failed for {url}: {exc!r}")
        return json.dumps(
            {"ok": False, "url": url, "error": "网页读取失败：连接超时或被拒绝。"},
            ensure_ascii=False,
        )
    # Keep existing citation IDs stable across searches and page fetches.
    cleaned_url = clean_source_url(page.url)
    source_id = next(
        (index for index, result in enumerate(turn.search_results, start=1) if result.url == cleaned_url),
        None,
    )
    if source_id is None:
        turn.search_results.append(SearchResult(page.title or cleaned_url, cleaned_url, ""))
        source_id = len(turn.search_results)
    end = offset + PAGE_CHARS
    return json.dumps(
        {
            "ok": True,
            "id": source_id,
            "url": cleaned_url,
            "title": page.title,
            "total_chars": len(page.text),
            "next_offset": end if end < len(page.text) else None,
            "text": page.text[offset:end] or "（这一段没有文字，可能需要脚本渲染。）",
        },
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(USE_SKILL_TOOL, self_tools_enabled, use_skill, platform_neutral=True),
    FunctionTool(INSPECT_SOURCE_TOOL, self_tools_enabled, inspect_source, platform_neutral=True),
    FunctionTool(WEB_SEARCH_TOOL, search_enabled, web_search, platform_neutral=True),
    FunctionTool(WEB_FETCH_TOOL, search_enabled, web_fetch, platform_neutral=True),
)
