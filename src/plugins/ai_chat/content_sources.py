from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.bot_storage import PostgresDatabase

from .conversation_scope import ConversationScope
from .media_tools import BilibiliClient, BilibiliError, find_bilibili_ref
from .message_ir import CardNode, MessageBody, TextNode
from .card_summary import extract_card_summary


SOURCE_HANDLE_PATTERN = re.compile(r"^source#([1-9][0-9]*)$")
MESSAGE_HANDLE_PATTERN = re.compile(r"^msg#([1-9][0-9]*)$")
HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>\"'\]]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,!?;:，。！？；：、）》】}"
TRACKING_QUERY_KEYS = {
    "from",
    "share_source",
    "share_medium",
    "share_plat",
    "share_session_id",
    "spm_id_from",
}


class ContentSourceError(RuntimeError):
    pass


BrowserFetch = Callable[[str], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class ContentSource:
    source_id: int
    platform: str
    canonical_url: str
    remote_id: str
    content_kind: str
    title: str
    author: str
    summary: str
    body_text: str
    comments: tuple[dict[str, object], ...]
    metadata: dict[str, object]
    status: str
    last_error: str
    fetched_at: int | None
    expires_at: int | None
    first_seen_at: int
    last_seen_at: int

    @property
    def handle(self) -> str:
        return f"source#{self.source_id}"

    def as_tool_payload(self, *, cached: bool) -> dict[str, object]:
        return {
            "handle": self.handle,
            "platform": self.platform,
            "kind": self.content_kind,
            "url": self.canonical_url,
            "remote_id": self.remote_id,
            "title": self.title,
            "author": self.author,
            "summary": self.summary,
            "body_text": self.body_text[:12000],
            "comments": list(self.comments[:20]),
            "metadata": self.metadata,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "cached": cached,
        }


class ContentSourceStore:
    """Globally deduplicated public sources with conversation-scoped handles."""

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        cache_seconds: int = 6 * 60 * 60,
    ) -> None:
        self.database = database
        self.cache_seconds = max(int(cache_seconds), 60)
        self._inspect_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def ingest_message(
        self,
        scope: ConversationScope,
        *,
        body: MessageBody,
        native_message_id: str | int,
        sender_native_user_id: str | int,
        canonical_message_id: int | None = None,
        occurred_at: int | None = None,
    ) -> int:
        candidates = extract_shared_urls(body)
        if not candidates:
            return 0
        now = int(occurred_at or time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        added = 0
        try:
            for segment_index, raw_url, title_hint in candidates:
                try:
                    canonical_url = canonicalize_source_url(raw_url)
                except ValueError:
                    continue
                platform, content_kind = classify_source(canonical_url)
                source_id = self._upsert_source(
                    cursor,
                    canonical_url=canonical_url,
                    platform=platform,
                    content_kind=content_kind,
                    title_hint=title_hint,
                    now=now,
                )
                cursor.execute(
                    """
                    INSERT INTO message_sources (
                        source_id, scope_key, canonical_message_id,
                        native_message_id, sender_native_user_id,
                        segment_index, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_key, native_message_id, segment_index, source_id)
                    DO UPDATE SET
                        canonical_message_id = COALESCE(
                            EXCLUDED.canonical_message_id,
                            message_sources.canonical_message_id
                        )
                    """,
                    (
                        source_id,
                        scope.key,
                        canonical_message_id,
                        str(native_message_id),
                        str(sender_native_user_id),
                        segment_index,
                        now,
                    ),
                )
                added += int(cursor.rowcount > 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return added

    async def inspect(
        self,
        scope: ConversationScope,
        target: str,
        *,
        browser_fetch: BrowserFetch | None = None,
        force_refresh: bool = False,
        comment_count: int = 10,
    ) -> tuple[ContentSource, bool]:
        source = self.resolve_target(scope, target)
        lock = self._inspect_locks.setdefault(source.source_id, asyncio.Lock())
        async with lock:
            source = self._get_by_id(source.source_id)
            now = int(time.time())
            if (
                not force_refresh
                and source.status == "ready"
                and source.expires_at is not None
                and source.expires_at > now
            ):
                return source, True
            self._mark_fetching(source.source_id, now)
            try:
                if source.platform == "bilibili":
                    payload = await self._inspect_bilibili(
                        source.canonical_url,
                        comment_count=comment_count,
                    )
                else:
                    if browser_fetch is None:
                        raise ContentSourceError("网页读取器没有开启。")
                    page = await browser_fetch(source.canonical_url)
                    payload = self._page_payload(source, page)
                refreshed = self._save_success(
                    source.source_id,
                    payload,
                    int(time.time()),
                )
            except asyncio.CancelledError:
                self._save_failure(
                    source.source_id,
                    "读取已取消。",
                    int(time.time()),
                )
                raise
            except Exception as exc:
                self._save_failure(
                    source.source_id,
                    str(exc),
                    int(time.time()),
                )
                if isinstance(exc, (ContentSourceError, BilibiliError)):
                    raise
                raise ContentSourceError("分享内容读取失败。") from exc
            return refreshed, False

    def get_cached(
        self,
        scope: ConversationScope,
        source_handle: str,
    ) -> ContentSource:
        source_id = _parse_handle(SOURCE_HANDLE_PATTERN, source_handle)
        if source_id is None:
            raise ContentSourceError("source_handle 格式无效。")
        source = self._get_visible_by_id(scope, source_id)
        if source is None:
            raise ContentSourceError("当前群没有见过这个来源。")
        return source

    def refresh_source(self, source_id: int) -> ContentSource:
        return self._get_by_id(int(source_id))

    def cached_deep_analysis(
        self,
        source: ContentSource,
        *,
        max_age_seconds: int = 24 * 60 * 60,
    ) -> dict[str, object] | None:
        cached = source.metadata.get("deep_video_analysis_v1")
        if not isinstance(cached, dict):
            return None
        created_at = _safe_int(cached.get("created_at"))
        result = cached.get("result")
        if (
            created_at <= 0
            or created_at + max(int(max_age_seconds), 60) <= int(time.time())
            or not isinstance(result, dict)
        ):
            return None
        return dict(result)

    def save_deep_analysis(
        self,
        source_id: int,
        result: dict[str, object],
    ) -> ContentSource:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                "SELECT metadata_json FROM content_sources WHERE source_id = ? FOR UPDATE",
                (int(source_id),),
            ).fetchone()
            if row is None:
                raise ContentSourceError("分享来源不存在。")
            metadata = _json_load(row["metadata_json"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["deep_video_analysis_v1"] = {
                "created_at": now,
                "result": result,
            }
            cursor.execute(
                """
                UPDATE content_sources
                SET metadata_json = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (_json_dump(metadata, {}), now, int(source_id)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._get_by_id(source_id)

    def resolve_target(
        self,
        scope: ConversationScope,
        target: str,
    ) -> ContentSource:
        value = str(target).strip()
        source_id = _parse_handle(SOURCE_HANDLE_PATTERN, value)
        if source_id is not None:
            source = self._get_visible_by_id(scope, source_id)
            if source is None:
                raise ContentSourceError("当前群没有见过这个来源。")
            return source
        message_id = _parse_handle(MESSAGE_HANDLE_PATTERN, value)
        if message_id is not None:
            matches = self._get_for_message(scope, message_id)
            if not matches:
                raise ContentSourceError("这条消息没有可读取的分享链接。")
            if len(matches) > 1:
                handles = "、".join(item.handle for item in matches[:6])
                raise ContentSourceError(f"这条消息有多个来源，请指定 {handles}。")
            return matches[0]
        try:
            canonical_url = canonicalize_source_url(value)
        except ValueError as exc:
            raise ContentSourceError(
                "target 必须是当前群的 source#、msg# 或完整 HTTP(S) 链接。"
            ) from exc
        now = int(time.time())
        platform, content_kind = classify_source(canonical_url)
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            source_id = self._upsert_source(
                cursor,
                canonical_url=canonical_url,
                platform=platform,
                content_kind=content_kind,
                title_hint="",
                now=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._get_by_id(source_id)

    def render_recent(
        self,
        scope: ConversationScope,
        *,
        max_sources: int = 4,
        max_chars: int = 1200,
        max_age_seconds: int = 15 * 60,
    ) -> str:
        cutoff = int(time.time()) - max(int(max_age_seconds), 60)
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT source.source_id, source.platform, source.content_kind,
                       source.canonical_url, source.title, source.status,
                       link.canonical_message_id, link.created_at
                FROM message_sources AS link
                JOIN content_sources AS source ON source.source_id = link.source_id
                WHERE link.scope_key = ? AND link.created_at >= ?
                ORDER BY link.created_at DESC, link.message_source_id DESC
                LIMIT ?
                """,
                (scope.key, cutoff, min(max(int(max_sources) * 3, 4), 30)),
            ).fetchall()
        finally:
            connection.close()
        lines: list[str] = []
        seen: set[int] = set()
        for row in rows:
            source_id = int(row["source_id"])
            if source_id in seen:
                continue
            seen.add(source_id)
            state = {
                "ready": "已读取",
                "fetching": "读取中",
                "failed": "上次失败",
            }.get(str(row["status"]), "待读取")
            message = (
                f" msg#{int(row['canonical_message_id'])}"
                if row["canonical_message_id"] is not None
                else ""
            )
            label = str(row["title"] or "").strip() or str(row["canonical_url"])
            line = (
                f"source#{source_id}{message} "
                f"[{row['platform']}/{row['content_kind']} · {state}] "
                f"{label[:360]}"
            )
            if sum(len(item) + 1 for item in lines) + len(line) > max_chars:
                break
            lines.append(line)
            if len(lines) >= max(int(max_sources), 1):
                break
        return "\n".join(lines)

    def search_visible(
        self,
        scope: ConversationScope,
        query: str,
        *,
        limit: int = 6,
    ) -> list[ContentSource]:
        """Search only sources previously linked inside the current conversation."""

        bounded = min(max(int(limit), 1), 20)
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT source.*, MAX(link.created_at) AS scope_last_seen_at
                FROM message_sources AS link
                JOIN content_sources AS source ON source.source_id = link.source_id
                WHERE link.scope_key = ?
                GROUP BY source.source_id
                ORDER BY scope_last_seen_at DESC, source.source_id DESC
                LIMIT 200
                """,
                (scope.key,),
            ).fetchall()
        finally:
            connection.close()
        query_terms = _source_terms(query)
        ranked: list[tuple[float, int, ContentSource]] = []
        for row in rows:
            source = _row_to_source(row)
            searchable = " ".join(
                (
                    source.platform,
                    source.content_kind,
                    source.title,
                    source.author,
                    source.summary,
                    source.body_text[:4000],
                )
            )
            terms = _source_terms(searchable)
            overlap = (
                len(query_terms.intersection(terms)) / max(len(query_terms), 1)
                if query_terms
                else 0.0
            )
            if query_terms and overlap <= 0:
                continue
            readiness = 0.15 if source.status == "ready" else 0.0
            ranked.append(
                (
                    overlap + readiness,
                    int(row["scope_last_seen_at"] or 0),
                    source,
                )
            )
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in ranked[:bounded]]

    def admin_snapshot(self, *, limit: int = 100) -> dict[str, object]:
        connection = self.database.store_connection()
        try:
            counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'ready') AS ready,
                       COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                       COUNT(*) FILTER (WHERE status = 'fetching') AS fetching,
                       COUNT(*) FILTER (WHERE status = 'failed') AS failed
                FROM content_sources
                """
            ).fetchone()
            platforms = connection.execute(
                """
                SELECT platform, COUNT(*) AS total
                FROM content_sources
                GROUP BY platform
                ORDER BY total DESC, platform ASC
                """
            ).fetchall()
            rows = connection.execute(
                """
                SELECT source.source_id, source.platform, source.content_kind,
                       source.canonical_url, source.remote_id, source.title,
                       source.author, source.summary, source.status,
                       source.last_error, source.fetched_at, source.last_seen_at,
                       COUNT(link.message_source_id) AS occurrences,
                       COUNT(DISTINCT link.scope_key) AS scopes
                FROM content_sources AS source
                LEFT JOIN message_sources AS link ON link.source_id = source.source_id
                GROUP BY source.source_id
                ORDER BY source.last_seen_at DESC, source.source_id DESC
                LIMIT ?
                """,
                (min(max(int(limit), 1), 500),),
            ).fetchall()
        finally:
            connection.close()
        return {
            "counts": dict(counts) if counts is not None else {},
            "platforms": [dict(row) for row in platforms],
            "items": [dict(row) for row in rows],
        }

    async def _inspect_bilibili(
        self,
        url: str,
        *,
        comment_count: int,
    ) -> dict[str, object]:
        client = BilibiliClient()
        try:
            result = await client.inspect(
                url,
                comment_count=min(max(int(comment_count), 0), 20),
            )
        finally:
            await client.close()
        comments = result.get("top_comments")
        metadata = {
            key: result.get(key)
            for key in (
                "aid",
                "bvid",
                "cid",
                "duration_seconds",
                "published_at",
                "parts",
                "stats",
                "url",
            )
        }
        return {
            "remote_id": str(result.get("bvid") or result.get("aid") or ""),
            "title": str(result.get("title") or "")[:2000],
            "author": str(result.get("uploader") or "")[:500],
            "summary": str(result.get("description") or "")[:2000],
            "body_text": str(result.get("description") or "")[:12000],
            "comments": comments if isinstance(comments, list) else [],
            "metadata": metadata,
        }

    def _page_payload(
        self,
        source: ContentSource,
        page: dict[str, object],
    ) -> dict[str, object]:
        text = _compact_text(str(page.get("text") or ""), 12000)
        title = _compact_text(str(page.get("title") or source.title), 2000)
        resolved_url = str(page.get("url") or source.canonical_url)[:4000]
        if not title and not text:
            raise ContentSourceError(
                "页面没有返回可读标题或正文，可能需要登录。"
            )
        return {
            "remote_id": source.remote_id,
            "title": title,
            "author": "",
            "summary": _compact_text(text, 1000),
            "body_text": text,
            "comments": [],
            "metadata": {
                "resolved_url": resolved_url,
                "http_status": page.get("status"),
                "captured_by": "playwright",
            },
        }

    def _upsert_source(
        self,
        cursor: Any,
        *,
        canonical_url: str,
        platform: str,
        content_kind: str,
        title_hint: str,
        now: int,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO content_sources (
                platform, canonical_url, content_kind, title,
                first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                last_seen_at = EXCLUDED.last_seen_at,
                title = CASE
                    WHEN content_sources.title = '' THEN EXCLUDED.title
                    ELSE content_sources.title
                END,
                updated_at = EXCLUDED.updated_at
            RETURNING source_id
            """,
            (
                platform,
                canonical_url,
                content_kind,
                str(title_hint).strip()[:2000],
                now,
                now,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise ContentSourceError("来源索引写入失败。")
        return int(row["source_id"])

    def _get_visible_by_id(
        self,
        scope: ConversationScope,
        source_id: int,
    ) -> ContentSource | None:
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                """
                SELECT source.*
                FROM content_sources AS source
                WHERE source.source_id = ?
                  AND EXISTS (
                      SELECT 1 FROM message_sources AS link
                      WHERE link.source_id = source.source_id
                        AND link.scope_key = ?
                  )
                """,
                (int(source_id), scope.key),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_source(row) if row is not None else None

    def _get_for_message(
        self,
        scope: ConversationScope,
        canonical_message_id: int,
    ) -> list[ContentSource]:
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT source.*
                FROM message_sources AS link
                JOIN content_sources AS source ON source.source_id = link.source_id
                WHERE link.scope_key = ? AND link.canonical_message_id = ?
                ORDER BY link.segment_index ASC, link.message_source_id ASC
                """,
                (scope.key, int(canonical_message_id)),
            ).fetchall()
        finally:
            connection.close()
        return [_row_to_source(row) for row in rows]

    def _get_by_id(self, source_id: int) -> ContentSource:
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                "SELECT * FROM content_sources WHERE source_id = ?",
                (int(source_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ContentSourceError("来源不存在。")
        return _row_to_source(row)

    def _mark_fetching(self, source_id: int, now: int) -> None:
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE content_sources
                SET status = 'fetching', last_error = '', updated_at = ?
                WHERE source_id = ?
                """,
                (now, int(source_id)),
            )
            connection.commit()
        finally:
            connection.close()

    def _save_success(
        self,
        source_id: int,
        payload: dict[str, object],
        now: int,
    ) -> ContentSource:
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                "SELECT metadata_json FROM content_sources WHERE source_id = ? FOR UPDATE",
                (int(source_id),),
            ).fetchone()
            existing_metadata = (
                _json_load(row["metadata_json"], {}) if row is not None else {}
            )
            incoming_metadata = payload.get("metadata")
            merged_metadata = (
                dict(incoming_metadata) if isinstance(incoming_metadata, dict) else {}
            )
            if isinstance(existing_metadata, dict):
                deep_analysis = existing_metadata.get("deep_video_analysis_v1")
                if isinstance(deep_analysis, dict):
                    merged_metadata["deep_video_analysis_v1"] = deep_analysis
            connection.execute(
                """
                UPDATE content_sources
                SET remote_id = ?, title = ?, author = ?, summary = ?,
                    body_text = ?, comments_json = ?, metadata_json = ?,
                    status = 'ready', last_error = '', fetched_at = ?,
                    expires_at = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (
                    str(payload.get("remote_id") or "")[:1000],
                    str(payload.get("title") or "")[:2000],
                    str(payload.get("author") or "")[:500],
                    str(payload.get("summary") or "")[:2000],
                    str(payload.get("body_text") or "")[:12000],
                    _json_dump(payload.get("comments"), []),
                    _json_dump(merged_metadata, {}),
                    now,
                    now + self.cache_seconds,
                    now,
                    int(source_id),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return self._get_by_id(source_id)

    def _save_failure(self, source_id: int, error: str, now: int) -> None:
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE content_sources
                SET status = 'failed', last_error = ?, fetched_at = ?,
                    expires_at = NULL, updated_at = ?
                WHERE source_id = ?
                """,
                (str(error)[:500], now, now, int(source_id)),
            )
            connection.commit()
        finally:
            connection.close()


def extract_shared_urls(
    body: MessageBody,
) -> tuple[tuple[int, str, str], ...]:
    candidates: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for node in body.nodes:
        if isinstance(node, CardNode):
            raw_title, raw_url = extract_card_summary(dict(node.raw_data))
            value = (raw_url or node.url).strip().rstrip(
                TRAILING_URL_PUNCTUATION
            )
            if value and value not in seen:
                seen.add(value)
                candidates.append(
                    (node.segment_index, value, node.title or raw_title)
                )
        elif isinstance(node, TextNode):
            for matched in HTTP_URL_PATTERN.finditer(node.text):
                value = matched.group(0).rstrip(TRAILING_URL_PUNCTUATION)
                if value and value not in seen:
                    seen.add(value)
                    candidates.append((node.segment_index, value, ""))
    return tuple(candidates)


def canonicalize_source_url(value: str) -> str:
    raw = str(value).strip()
    if not raw or len(raw) > 4000:
        raise ValueError("invalid URL")
    parts = urlsplit(raw)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError("unsupported URL")
    if parts.username or parts.password:
        raise ValueError("URL credentials are not allowed")
    host = parts.hostname.casefold().rstrip(".")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (
        parts.scheme.casefold() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parts.path or "/"
    is_bili_host = (
        host == "b23.tv"
        or host == "bilibili.com"
        or host.endswith(".bilibili.com")
    )
    direct_bili = find_bilibili_ref(raw) if is_bili_host else None
    if direct_bili is not None and direct_bili.bvid:
        return f"https://www.bilibili.com/video/{direct_bili.bvid}"
    if direct_bili is not None and direct_bili.aid:
        return f"https://www.bilibili.com/video/av{direct_bili.aid}"
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in TRACKING_QUERY_KEYS
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def classify_source(url: str) -> tuple[str, str]:
    host = (urlsplit(url).hostname or "").casefold()
    if host == "b23.tv" or host == "bilibili.com" or host.endswith(".bilibili.com"):
        return "bilibili", "video"
    if (
        host == "xhslink.com"
        or host == "xiaohongshu.com"
        or host.endswith(".xiaohongshu.com")
    ):
        return "xiaohongshu", "post"
    if host == "douyin.com" or host.endswith(".douyin.com"):
        return "douyin", "video"
    if host == "weibo.com" or host.endswith(".weibo.com") or host == "weibo.cn":
        return "weibo", "post"
    if host == "zhihu.com" or host.endswith(".zhihu.com"):
        return "zhihu", "article"
    if host in {"youtube.com", "www.youtube.com", "youtu.be"}:
        return "youtube", "video"
    return "web", "webpage"


def _parse_handle(pattern: re.Pattern[str], value: str) -> int | None:
    matched = pattern.fullmatch(str(value).strip())
    return int(matched.group(1)) if matched is not None else None


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _row_to_source(row: Any) -> ContentSource:
    comments = _json_load(row["comments_json"], [])
    metadata = _json_load(row["metadata_json"], {})
    return ContentSource(
        source_id=int(row["source_id"]),
        platform=str(row["platform"]),
        canonical_url=str(row["canonical_url"]),
        remote_id=str(row["remote_id"]),
        content_kind=str(row["content_kind"]),
        title=str(row["title"]),
        author=str(row["author"]),
        summary=str(row["summary"]),
        body_text=str(row["body_text"]),
        comments=tuple(item for item in comments if isinstance(item, dict)),
        metadata=metadata if isinstance(metadata, dict) else {},
        status=str(row["status"]),
        last_error=str(row["last_error"]),
        fetched_at=(
            int(row["fetched_at"])
            if row["fetched_at"] is not None
            else None
        ),
        expires_at=(
            int(row["expires_at"])
            if row["expires_at"] is not None
            else None
        ),
        first_seen_at=int(row["first_seen_at"]),
        last_seen_at=int(row["last_seen_at"]),
    )


def _source_terms(value: str) -> set[str]:
    folded = str(value).casefold()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_.+#/-]{1,}", folded))
    for run in re.findall(r"[\u3400-\u9fff]+", folded):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _compact_text(value: str, limit: int) -> str:
    lines = [" ".join(line.split()) for line in str(value).splitlines()]
    return "\n".join(line for line in lines if line).strip()[: max(int(limit), 0)]


def _json_load(value: object, default: object) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _json_dump(value: object, default: object) -> str:
    candidate = value if isinstance(value, (dict, list)) else default
    return json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
