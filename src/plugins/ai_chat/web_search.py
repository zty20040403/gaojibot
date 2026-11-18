from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from itertools import zip_longest
import logging
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

_logger = logging.getLogger(__name__)
_QUERY_SLOTS = asyncio.Semaphore(4)
_bocha_exhausted = False
_STRICT_QUERY_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "iesdouyin.com",
    "xiaohongshu.com",
    "weibo.com",
    "tiktok.com",
)
_STRIP_QUERY_PARAMS = frozenset({"spm", "from", "fbclid", "gclid", "vd_source"})
_CODE_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
_INLINE_MARKER = re.compile(r"(?<!`)(`{3,})(?!`)")
_CITATION_RE = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    date: str = ""
    provider: str = ""


class SearchError(RuntimeError):
    pass


class _ProviderError(Exception):
    """A fixed diagnostic safe to log without upstream bodies or credentials."""


def _log_attempt(provider: str, outcome: str, count: int = 0, fallback_reason: str = "none") -> None:
    _logger.log(
        logging.INFO if outcome in {"success", "empty"} else logging.WARNING,
        "search provider=%s fallback_reason=%s outcome=%s result_count=%d",
        provider, fallback_reason, outcome, count,
        extra={"provider": provider, "fallback_reason": fallback_reason,
               "outcome": outcome, "result_count": count},
    )


async def search_web(
    queries: list[str], *, max_results: int = 10, timeout_seconds: int = 10,
    api_key: str = "", transport: httpx.AsyncBaseTransport | None = None,
    all_results: list[SearchResult] | None = None,
) -> list[SearchResult]:
    async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
        batches = await asyncio.gather(*(
            _search_query(client, query, max_results, timeout_seconds, api_key) for query in queries
        ))
    unique = {}
    for group in zip_longest(*(batch for batch in batches if batch)):
        for result in group:
            if result is not None:
                unique.setdefault(result.url, result)
    if not unique and None in batches:
        raise SearchError("联网搜索失败。")
    if all_results is not None:
        all_results.extend(unique.values())
    return list(unique.values())[:max_results]


async def _search_query(client: httpx.AsyncClient, query: str, limit: int,
                        timeout: int, api_key: str) -> list[SearchResult] | None:
    previous = "none"
    async with _QUERY_SLOTS:
        for provider in ("exa", "bocha"):
            if provider == "bocha" and (not api_key or _bocha_exhausted):
                break
            results = []
            try:
                async with asyncio.timeout(timeout):
                    results = (await _search_exa(client, query, limit) if provider == "exa" else
                               await _search_bocha(client, query, limit, api_key))
                outcome = "success" if results else "empty"
            except TimeoutError:
                outcome = "timeout"
            except _ProviderError as exc:
                outcome = str(exc)
            _log_attempt(provider, outcome, len(results), previous)
            if results:
                return results
            previous = outcome
    return [] if previous == "empty" else None


async def _post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    try:
        return await client.post(url, **kwargs)
    except httpx.TimeoutException:
        raise _ProviderError("timeout") from None
    except httpx.HTTPError:
        raise _ProviderError("network_error") from None


def _rpc(response: httpx.Response, request_id: int) -> dict:
    if not response.is_success:
        raise _ProviderError("rate_limit" if response.status_code == 429 else "http_error")
    try:
        if "text/event-stream" in response.headers.get("content-type", ""):
            data = "\n".join(line[5:].lstrip() for line in response.text.splitlines()
                             if line.startswith("data:"))
            payload = json.loads(data)
        else:
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
            raise ValueError
        if "error" in payload:
            raise _ProviderError("rpc_error")
        if not isinstance(payload.get("result"), dict):
            raise ValueError
        return payload["result"]
    except (ValueError, TypeError):
        raise _ProviderError("schema_error") from None


async def _search_exa(client: httpx.AsyncClient, query: str, limit: int) -> list[SearchResult]:
    url = "https://mcp.exa.ai/mcp"
    headers = {"Accept": "application/json, text/event-stream"}
    response = await _post(client, url, headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "gaoji", "version": "1"},
        },
    })
    init = _rpc(response, 1)
    session = response.headers.get("mcp-session-id")
    if not session or init.get("protocolVersion") != "2025-03-26":
        raise _ProviderError("schema_error")
    headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-03-26"})
    response = await _post(client, url, headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    if response.status_code != 202:
        raise _ProviderError("rate_limit" if response.status_code == 429 else "http_error")
    response = await _post(client, url, headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "web_search_exa", "arguments": {
            "query": query, "objective": query, "numResults": limit,
        }},
    })
    result = _rpc(response, 2)
    if result.get("isError"):
        raise _ProviderError("tool_error")
    content = result.get("content")
    if not isinstance(content, list):
        raise _ProviderError("schema_error")
    results = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(text := item.get("text"), str):
            raise _ProviderError("schema_error")
        if not text.strip() or text.strip().lower().startswith("no results found"):
            continue
        for block in re.split(r"\n[ \t]*\n---[ \t]*\n[ \t]*\n(?=Title:)", text.strip().replace("\r\n", "\n")):
            parts = re.split(r"(?m)^(?:Highlights|Text):[ \t]*", block, maxsplit=1)
            fields = dict(line.split(":", 1) for line in parts[0].splitlines() if ":" in line)
            title, link = fields.get("Title", ""), fields.get("URL", "")
            results.append(_result(title, link, parts[1] if len(parts) > 1 else "",
                                   fields.get("Published", ""), "exa"))
            if len(results) == limit:
                return results
    return results


def _result(title: str, url: str, snippet: str, date: str, provider: str) -> SearchResult:
    if not all(isinstance(value, str) for value in (title, url, snippet, date)):
        raise _ProviderError("schema_error")
    url = url.strip()
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and len(url) <= 2048
    except ValueError:
        valid = False
    if not valid or not title.strip():
        raise _ProviderError("schema_error")
    return SearchResult(_clean_text(title)[:300], url, _clean_text(snippet)[:800],
                        _clean_text(date)[:50], provider)


def _balance_error(status: int, payload: object) -> bool:
    if status == 402:
        return True
    if not isinstance(payload, dict):
        return False
    code = str(payload.get("code", "")).lower()
    message = str(payload.get("msg") or payload.get("message") or "").lower()
    return code in {"402", "insufficient_balance", "balance_insufficient"} or any(
        phrase in message for phrase in ("余额不足", "账户欠费", "insufficient balance", "balance exhausted")
    )


async def _search_bocha(client: httpx.AsyncClient, query: str, limit: int, api_key: str) -> list[SearchResult]:
    global _bocha_exhausted
    response = await _post(client, "https://api.bochaai.com/v1/web-search",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"query": query, "count": limit, "summary": True, "freshness": "noLimit"})
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if _balance_error(response.status_code, payload):
        if not _bocha_exhausted:
            _bocha_exhausted = True
            _logger.warning("search provider=bocha fallback_reason=balance_exhausted disabled=true",
                            extra={"provider": "bocha", "fallback_reason": "balance_exhausted"})
        raise _ProviderError("balance_exhausted")
    if not response.is_success:
        raise _ProviderError("rate_limit" if response.status_code == 429 else "http_error")
    if not isinstance(payload, dict):
        raise _ProviderError("schema_error")
    if payload.get("code") is not None and str(payload["code"]) != "200":
        raise _ProviderError("rate_limit" if str(payload["code"]) == "429" else "api_error")
    try:
        pages = payload["data"]["webPages"]["value"]
    except (KeyError, TypeError):
        raise _ProviderError("schema_error") from None
    if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages[:limit]):
        raise _ProviderError("schema_error")
    return [_result(page.get("name"), page.get("url"), page.get("summary") or page.get("snippet") or "",
                    page.get("datePublished") or page.get("dateLastCrawled") or "", "bocha")
            for page in pages[:limit]]


def clean_source_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = unquote(parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError):
        return url
    if parsed.scheme not in {"http", "https"} or not host:
        return url
    if not parsed.query:
        return url
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(host == domain or host.endswith("." + domain) for domain in _STRICT_QUERY_DOMAINS):
        kept = [(key, value) for key, value in pairs if key in {"p", "t"}]
    else:
        kept = [
            (key, value)
            for key, value in pairs
            if not key.lower().startswith(("utm_", "share_"))
            and key.lower() not in _STRIP_QUERY_PARAMS
        ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(kept), parsed.fragment))


def _code_fences(text: str):
    """Yield complete or unterminated code spans without changing their text."""
    start = offset = 0
    opening = None
    block = False
    for line in text.splitlines(keepends=True):
        match = _CODE_FENCE_LINE.fullmatch(line.rstrip("\r\n"))
        if block:
            if match:
                marker, suffix = match.groups()
                if marker[0] == opening[0] and len(marker) >= len(opening) and not suffix.strip():
                    yield start, offset + len(line)
                    opening, block = None, False
        elif opening is None and match and (match.group(1)[0] != "`" or "`" not in match.group(2)):
            start, opening, block = offset, match.group(1), True
        else:
            for marker in _INLINE_MARKER.finditer(line):
                if opening is None:
                    start, opening = offset + marker.start(), marker.group(1)
                elif marker.group(1) == opening:
                    yield start, offset + marker.end()
                    opening = None
        offset += len(line)
    if opening is not None:
        yield start, len(text)


def _citation_index(digits: str, maximum: int) -> int:
    index = 0
    for digit in digits:
        index = index * 10 + int(digit)
        if index > maximum:
            return 0
    return index


def render_search_sources(
    results: list[SearchResult], answer: str = "", max_sources: int = 5
) -> tuple[str, str]:
    if not results or not answer:
        return answer, ""

    ordered: list[int] = []
    mapping: dict[int, int] = {}

    def _collect(segment: str) -> None:
        for match in _CITATION_RE.finditer(segment):
            old_index = _citation_index(match.group(1), len(results))
            if 1 <= old_index <= len(results) and old_index not in mapping:
                if len(ordered) >= max_sources:
                    continue
                ordered.append(old_index)
                mapping[old_index] = len(ordered)

    fences = list(_code_fences(answer))
    pos = 0
    for start, end in fences:
        _collect(answer[pos:start])
        pos = end
    _collect(answer[pos:])

    if not ordered:
        return answer, ""

    def _replace_segment(segment: str) -> str:
        def replace(match: re.Match) -> str:
            index = _citation_index(match.group(1), len(results))
            if index in mapping:
                return f"[{mapping[index]}]"
            # An omitted valid citation must not accidentally name another
            # source after renumbering; preserve genuinely invalid markers.
            return "" if 1 <= index <= len(results) else match.group(0)

        return _CITATION_RE.sub(replace, segment)

    parts: list[str] = []
    pos = 0
    for start, end in fences:
        parts.append(_replace_segment(answer[pos:start]))
        parts.append(answer[start:end])
        pos = end
    parts.append(_replace_segment(answer[pos:]))

    lines = ["来源："]
    for new_index, old_index in enumerate(ordered, start=1):
        result = results[old_index - 1]
        lines.append(f"{new_index}. {result.title}\n{result.url}")
    return "".join(parts), "\n".join(lines)


def _clean_text(text: str) -> str:
    return " ".join(text.split())
