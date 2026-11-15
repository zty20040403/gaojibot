from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import logging
import re
import time
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

_logger = logging.getLogger(__name__)
SEARCH_PROVIDERS = frozenset({"scrape", "tavily", "bocha"})
_BOCHA_FRESHNESS = {"d": "oneDay", "w": "oneWeek", "m": "oneMonth", "y": "oneYear"}
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
_DDG_BLOCK_TTL_SECONDS = 3600.0
_ddg_blocked_until = 0.0


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchError(RuntimeError):
    pass


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._capture_title = False
        self._capture_snippet = False
        self._current_title: list[str] = []
        self._current_snippet: list[str] = []
        self._current_url = ""
        self._pending_title = ""
        self._pending_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name: value or "" for name, value in attrs}
        classes = set(attr_map.get("class", "").split())

        if tag == "a" and "result__a" in classes:
            self._push_pending_result("")
            self._capture_title = True
            self._current_title = []
            self._current_url = _clean_duckduckgo_url(attr_map.get("href", ""))
            return

        if "result__snippet" in classes or "result-snippet" in classes:
            self._capture_snippet = True
            self._current_snippet = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._current_title.append(data)
        if self._capture_snippet:
            self._current_snippet.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            self._capture_title = False
            self._pending_title = _clean_text("".join(self._current_title))
            self._pending_url = self._current_url
            return

        if self._capture_snippet and tag in {"a", "div", "td"}:
            self._capture_snippet = False
            snippet = _clean_text("".join(self._current_snippet))
            self._push_pending_result(snippet)

    def close(self) -> None:
        super().close()
        self._push_pending_result("")

    def _push_pending_result(self, snippet: str) -> None:
        if not self._pending_title or not self._pending_url:
            return

        self.results.append(
            SearchResult(
                title=self._pending_title,
                url=self._pending_url,
                snippet=snippet,
            )
        )
        self._pending_title = ""
        self._pending_url = ""


def should_search(text: str) -> bool:
    normalized = text.strip().lower()
    return any(
        keyword in normalized
        for keyword in (
            "搜索",
            "搜一下",
            "查一下",
            "查查",
            "联网",
            "网上",
            "最新",
            "今天",
            "现在",
            "刚刚",
            "新闻",
            "价格",
            "官网",
            "发布",
            "更新",
        )
    )


async def search_web(
    query: str,
    max_results: int = 5,
    timeout_seconds: int = 10,
    freshness: str | None = None,
    provider: str = "scrape",
    api_key: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[SearchResult]:
    global _ddg_blocked_until
    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(
        timeout=timeout_seconds, follow_redirects=True, headers=headers, transport=transport
    ) as client:
        if provider in {"tavily", "bocha"} and api_key:
            try:
                return _unique_results(
                    await _search_api(client, provider, api_key, cleaned_query, max_results, freshness),
                    max_results,
                )
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                # A paid API that is out of credit should not take search down with it.
                _logger.warning("%s search failed, falling back to scraping: %s", provider, exc)
        now = time.monotonic()
        duckduckgo_results: list[SearchResult] = []
        if now >= _ddg_blocked_until:
            try:
                params = {"q": cleaned_query}
                if freshness in {"d", "w", "m", "y"}:
                    params["df"] = freshness
                response = await client.get(
                    "https://duckduckgo.com/html/",
                    params=params,
                )
                duckduckgo_results = _parse_duckduckgo_results(response.text)
                # Several searches may already be in flight when the first
                # challenge arrives; only its response starts the cooldown.
                now = time.monotonic()
                if (not duckduckgo_results and "anomaly" in response.text.lower()
                        and now >= _ddg_blocked_until):
                    _ddg_blocked_until = now + _DDG_BLOCK_TTL_SECONDS
                    _logger.info(
                        "DuckDuckGo HTML search returned anomaly challenge; skipping DuckDuckGo for %d seconds.",
                        int(_DDG_BLOCK_TTL_SECONDS),
                    )
                response.raise_for_status()
            except httpx.HTTPError:
                duckduckgo_results = []

        if duckduckgo_results:
            matches = _unique_results(relevant(cleaned_query, duckduckgo_results), max_results)
            if matches:
                return matches

        try:
            response = await client.get(
                "https://www.bing.com/search",
                params={
                    "q": cleaned_query,
                    "format": "rss",
                    "setlang": "zh-hans",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            if duckduckgo_results:
                return []
            raise SearchError("联网搜索失败。") from exc

    return _unique_results(
        relevant(cleaned_query, _parse_bing_rss_results(response.text)),
        max_results,
    )


async def _search_api(
    client: httpx.AsyncClient,
    provider: str,
    api_key: str,
    query: str,
    max_results: int,
    freshness: str | None,
) -> list[SearchResult]:
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "tavily":
        body: dict[str, object] = {"query": query, "max_results": min(max_results, 20)}
        if freshness:
            body["time_range"] = freshness
        response = await client.post("https://api.tavily.com/search", json=body, headers=headers)
        response.raise_for_status()
        return [
            SearchResult(_clean_text(row.get("title") or ""), str(row.get("url") or ""),
                         _clean_text(row.get("content") or ""))
            for row in response.json().get("results") or []
        ]
    body = {
        "query": query,
        "count": min(max_results, 50),
        "summary": True,
        "freshness": _BOCHA_FRESHNESS.get(freshness or "", "noLimit"),
    }
    response = await client.post("https://api.bochaai.com/v1/web-search", json=body, headers=headers)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") is not None and str(payload["code"]) != "200":
        # Never log the provider's error body; it may echo the API key.
        raise ValueError("Bocha search returned an error")
    pages = ((payload.get("data") or {}).get("webPages") or {}).get("value") or []
    results = []
    for page in pages:
        # The publish date is what tells a reader whether a quoted price is current.
        date = str(page.get("datePublished") or page.get("dateLastCrawled") or "")[:10]
        snippet = _clean_text(page.get("summary") or page.get("snippet") or "")
        results.append(SearchResult(_clean_text(page.get("name") or ""), str(page.get("url") or ""),
                                    f"[{date}] {snippet}" if date else snippet))
    return results


def _parse_duckduckgo_results(html: str) -> list[SearchResult]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.results


def _parse_bing_rss_results(xml: str) -> list[SearchResult]:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    results: list[SearchResult] = []
    for item in root.findall("./channel/item"):
        title = _clean_text(item.findtext("title", ""))
        url = item.findtext("link", "").strip()
        snippet = _clean_text(item.findtext("description", ""))
        if title and url:
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                )
            )
    return results


def relevant(query: str, results: list[SearchResult]) -> list[SearchResult]:
    terms = [word.lower() for word in query.split() if len(word) >= 2]
    if not terms:
        return list(results)
    filtered: list[SearchResult] = []
    for result in results:
        haystack = f"{result.title}\n{result.snippet}".lower()
        if any(term in haystack for term in terms):
            filtered.append(result)
    return filtered


def _unique_results(
    results: list[SearchResult],
    max_results: int,
) -> list[SearchResult]:
    unique_results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for result in results:
        if not result.title or not result.url or result.url in seen_urls:
            continue
        seen_urls.add(result.url)
        unique_results.append(result)
        if len(unique_results) >= max_results:
            break

    return unique_results


def render_search_context(results: list[SearchResult]) -> str:
    if not results:
        return ""

    lines: list[str] = []
    for index, result in enumerate(results, start=1):
        lines.append(
            f"[{index}] {result.title}\n"
            f"URL: {result.url}\n"
            f"摘要: {result.snippet or '无摘要'}"
        )
    return "\n\n".join(lines)


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


def render_direct_search_results(
    results: list[SearchResult],
    max_results: int = 5,
) -> str:
    if not results:
        return ""

    lines = ["搜索结果："]
    for index, result in enumerate(results[:max_results], start=1):
        lines.append(
            f"{index}. {result.title}\n"
            f"{result.snippet or '无摘要'}\n"
            f"{result.url}"
        )
    return "\n\n".join(lines)


def search_freshness(text: str) -> str | None:
    """Time filter for the direct search command; prices and specs stay unfiltered."""
    normalized = text.strip().lower()
    if any(keyword in normalized for keyword in ("今天", "今日", "刚刚", "天气", "比分")):
        return "d"
    if any(keyword in normalized for keyword in ("新闻", "近期", "最近", "本周")):
        return "w"
    return None


def _clean_duckduckgo_url(href: str) -> str:
    if not href:
        return ""

    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://duckduckgo.com" + href

    parsed = urlsplit(href)
    query = parse_qs(parsed.query)
    redirected_url = query.get("uddg", [""])[0]
    if redirected_url:
        return unquote(redirected_url)
    if parsed.hostname == "duckduckgo.com":
        # Ad clicks (/y.js?ad_domain=...) only open through DuckDuckGo's redirector.
        return ""

    return href


def _clean_text(text: str) -> str:
    return " ".join(text.split())
