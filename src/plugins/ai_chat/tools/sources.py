"""Read-only web search, installed skills and packaged source inspection."""

from __future__ import annotations

from functools import partial
import hashlib
import json
import logging
import re
import time
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from jsonschema import Draft202012Validator

from ..ai_tools import INSPECT_SOURCE_TOOL, USE_SKILL_TOOL, WEB_FETCH_TOOL, WEB_SEARCH_TOOL
from ..web_fetch import BLOCKED_STATUSES, BROWSER_HEADERS, FetchError, fetch_page
from ..web_search import SearchError, SearchResult, clean_source_url, search_web
from .registry import FunctionTool
from .state import ToolContext, TurnState, structured_tool_source

PAGE_CHARS = 8000
STOP_SEARCH = "没有新信息了。用已知的回答，或者问用户要背景（哪个圈子、做什么的）。"
_SECRET = re.compile(r"(?i)api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization|bearer|密钥|密码|凭证|\bkey\s*[=:]")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{32,}|\b(?:sk|ghp|github_pat)[-_][A-Za-z0-9_-]{12,}", re.I)


def evidence_url(url: str) -> str:
    """Keep only a public URL without userinfo, query, fragment, or likely path secrets."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        path = unquote(parts.path)
        if (parts.scheme not in {'http', 'https'} or not host or _SECRET.search(path)
                or _OPAQUE.search(path) or len(url) > 2048):
            return ''
        netloc = f'[{host}]' if ':' in host else host
        if parts.port is not None:
            netloc += f':{parts.port}'
        safe = urlunsplit((parts.scheme, netloc, parts.path or '/', '', ''))
        return safe if len(safe) <= 500 else ''
    except (ValueError, UnicodeError):
        return ''


def search_key(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        return (host, parts.path or '/') if parts.scheme in {'http', 'https'} and host else None
    except (ValueError, UnicodeError):
        return None


def remember(items: list[str], value: str, limit: int = 200) -> None:
    if value and value not in items and len(items) < limit:
        items.append(value)


def evidence_term(query: str) -> str:
    query = re.sub(r'https?://\S+', '[链接已省略]', query, flags=re.I)
    if _SECRET.search(query) or _OPAQUE.search(query):
        return ''
    return ' '.join(query.split())[:200]


def progress(turn: TurnState, new: int) -> dict[str, str]:
    payload = {'progress': f'[本轮第 {turn.search_count} 次搜索 · 已用 {int(turn.search_seconds)} 秒 · 新链接 {new} 条]'}
    if not turn.search_background and turn.search_seconds >= 30:
        payload['handoff'] = '先用 say 告知已知进展，再用 agent 交接；仅在任务实际启动后用 promise 安排回访，不要声称尚未成功的操作已成功。'
    return payload


def stopped(turn):
    reason = 'no_new_links' if turn.search_no_new >= (3 if turn.search_background else 2) else 'hard_limit'
    logging.getLogger(__name__).info('search_stop reason=%s background=%s count=%d', reason, turn.search_background, turn.search_count)
    return json.dumps({'ok': False, 'note': STOP_SEARCH, **progress(turn, 0)}, ensure_ascii=False)


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
    settings = turn.context.app.settings
    schema = WEB_SEARCH_TOOL["function"]["parameters"]
    if not Draft202012Validator(schema).is_valid(arguments):
        return json.dumps({"ok": False, "error": "搜索参数不合法。"}, ensure_ascii=False)
    queries = [query.strip()[:settings.max_input_chars] for query in arguments["queries"]]
    max_results = min(int(arguments.get("max_results", 10)), settings.search_max_results)
    source = structured_tool_source(turn.context)
    browser = None
    if source is not None:
        from ..background_tools import workspace_owner
        from ..conversation_search import search_browser
        browser = partial(search_browser, turn.context.app.sandbox_manager, workspace_owner(source.endpoint))
    async with turn.search_lock:
        if turn.search_budget:
            def reserve(budget):
                count, no_new = budget.get('count', 0), budget.get('no_new', 0)
                allowed = count < 40 and no_new < 3
                budget['count'] = count + int(allowed)
                return allowed, budget['count'], budget.get('seconds', 0), no_new
            allowed, turn.search_count, turn.search_seconds, turn.search_no_new = await turn.search_budget(reserve)
            if not allowed:
                return stopped(turn)
        else:
            if turn.search_count >= (40 if turn.search_background else 15) or turn.search_no_new >= (3 if turn.search_background else 2):
                return stopped(turn)
            turn.search_count += 1
        for query in queries:
            remember(turn.search_terms, evidence_term(query), 60)
        all_results: list[SearchResult] = []
        started = time.monotonic()
        failed = True
        try:
            results = await search_web(
                queries, max_results=max_results, timeout_seconds=settings.search_timeout_seconds,
                api_key=settings.search_api_key, all_results=all_results, browser=browser,
            )
            failed = False
        except SearchError:
            pass
        finally:
            elapsed = time.monotonic() - started
            keys = {key for result in all_results if (key := search_key(result.url)) is not None} if not failed else set()
            if turn.search_budget:
                hashes = {hashlib.sha256(json.dumps(key, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                          for key in keys}
                def finish(budget):
                    seen = set(budget.get('seen', []))
                    new = len(hashes - seen)
                    budget['seen'] = sorted(seen | hashes)
                    budget['seconds'] = budget.get('seconds', 0) + elapsed
                    budget['no_new'] = 0 if new else budget.get('no_new', 0) + 1
                    return budget['count'], budget['seconds'], budget['no_new'], new
                turn.search_count, turn.search_seconds, turn.search_no_new, new = await turn.search_budget(finish)
            else:
                turn.search_seconds += elapsed
                new = len(keys - turn.search_seen)
                turn.search_no_new = 0 if new else turn.search_no_new + 1
            turn.search_seen.update(keys)
        if failed:
            return json.dumps({"ok": False, "error": "联网搜索暂时失败。", **progress(turn, 0)}, ensure_ascii=False)
        for result in all_results:
            remember(turn.search_found, evidence_url(result.url))
        id_by_url = {result.url: index for index, result in enumerate(turn.search_results, start=1)}
        payload_results: list[dict[str, object]] = []
        for result in results:
            cleaned_url = clean_source_url(result.url)
            cleaned = SearchResult(result.title, cleaned_url, result.snippet[:800], result.date, result.provider)
            source_id = id_by_url.get(cleaned_url)
            if source_id is None:
                turn.search_results.append(cleaned)
                source_id = len(turn.search_results)
                id_by_url[cleaned_url] = source_id
            payload_results.append({"id": source_id, "title": cleaned.title, "url": cleaned.url,
                                    "snippet": cleaned.snippet, "date": cleaned.date, "provider": cleaned.provider})
        return json.dumps({"ok": True, "queries": queries, "results": payload_results,
                           **progress(turn, new)}, ensure_ascii=False)


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
    if page.text[offset:end]:
        remember(turn.search_read, evidence_url(page.url))
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
