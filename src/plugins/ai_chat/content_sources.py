from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.bot_storage import PostgresDatabase

from .conversation_scope import ConversationScope
from .media_tools import find_bilibili_ref
from .message_ir import CardNode, MessageBody, TextNode
from .card_summary import extract_card_summary


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


class ContentSourceStore:
    """Globally deduplicated public sources with conversation-scoped handles."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

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
        return int(cursor.fetchone()["source_id"])


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
