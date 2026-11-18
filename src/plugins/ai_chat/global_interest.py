"""Daily public RSS headlines; all returned text is untrusted source material."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx

from .handler_constants import SHANGHAI_TZ

# Only publisher-advertised, unauthenticated RSS feeds; article URLs are never fetched.
SOURCES = (
    ("nasa-technology", "https://www.nasa.gov/technology/feed/", "www.nasa.gov", ("科技", "航天")),
    ("loc-timeless", "https://blogs.loc.gov/loc/feed/", "blogs.loc.gov", ("文化", "历史")),
)
MAX_BYTES = 256 * 1024
MAX_ITEMS_PER_SOURCE = 5
MAX_INSPECTED = 50
SOURCE_TIMEOUT = 8
MAX_AGE = 14 * 86400
CANDIDATE_TTL = 3 * 86400
TOPICS = {"music": "音乐", "book": "书籍", "film": "电影", "map": "地图",
          "space": "太空", "moon": "月球", "mars": "火星", "robot": "机器人"}


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / "global-interest" / "state.json"


def _load(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        return {"last_attempt_day": "", "candidates": []}
    if not isinstance(state, dict) or not isinstance(state.get("last_attempt_day"), str) or not isinstance(state.get("candidates"), list):
        raise ValueError("invalid_global_interest_state")
    return state


def _save(path: Path, state: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".interest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o640)
            json.dump(state, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _unexpired(candidates: list, now: int) -> list[dict]:
    return [item for item in candidates
            if isinstance(item, dict) and type(item.get("expires_at")) is int and item["expires_at"] > now]


def read_candidates(state_dir: Path, *, now: int | None = None) -> list[dict]:
    """Return unexpired headline records, not instructions; no network access."""
    current = int(time.time()) if now is None else now
    return _unexpired(_load(_path(state_dir))["candidates"], current)


def _link(value: str, host: str) -> str:
    if len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return ""
    try:
        url = urlsplit(value)
        if url.scheme != "https" or url.netloc != host or not url.path.startswith("/"):
            return ""
    except ValueError:
        return ""
    path = unquote(url.path)
    parts = path.casefold().split("/")
    if ("%" in path or "\\" in path or any(ord(c) <= 32 or ord(c) == 127 for c in path)
            or any(part in {".", ".."} for part in parts)):
        return ""
    if host == "blogs.loc.gov" and not re.match(r"^/loc/\d{4}/\d{2}/[^/]+/?$", path):
        return ""
    if host == "www.nasa.gov" and any(part in {"people", "person", "profile", "author", "biographies"} for part in parts):
        return ""
    return urlunsplit(("https", host, url.path, "", ""))


def _parse_feed(data: bytes, source: tuple, now: int) -> list[dict]:
    text = data.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ValueError("rss_dtd_forbidden")
    root = ET.fromstring(text)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("invalid_rss")
    name, _url, host, base_tags = source
    found = []
    for index, item in enumerate(root.find("channel").iterfind("item")):
        if index >= MAX_INSPECTED or len(found) >= MAX_ITEMS_PER_SOURCE:
            break
        title = " ".join((item.findtext("title") or "").split())[:240]
        link = _link((item.findtext("link") or "").strip(), host)
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                continue
            published_at = int(published.timestamp())
        except (TypeError, ValueError, OverflowError):
            continue
        if (not title or "<" in title or ">" in title or any(ord(c) < 32 for c in title)
                or not link or published_at > now or published_at + MAX_AGE <= now
                or any(existing["url"] == link for existing in found)):
            continue
        tags = list(base_tags)
        lowered = title.casefold()
        tags.extend(tag for word, tag in TOPICS.items() if re.search(r"\b" + word + r"\w*\b", lowered))
        found.append({"source": name, "title": title, "url": link, "tags": tags,
                      "published_at": published_at, "collected_at": now,
                      "expires_at": min(now + CANDIDATE_TTL, published_at + MAX_AGE)})
    return found


async def _fetch(source: tuple, now: int, transport=None) -> list[dict]:
    async with asyncio.timeout(SOURCE_TIMEOUT):
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     headers={"User-Agent": "gaoji-interest/1.0", "Accept": "application/rss+xml, application/xml",
                                              "Accept-Encoding": "identity"}) as client:
            async with client.stream("GET", source[1]) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("rss_content_encoding")
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > MAX_BYTES:
                        raise ValueError("rss_too_large")
                    data.extend(chunk)
        return _parse_feed(bytes(data), source, now)


async def collect_daily(state_dir: Path, *, logger, now: int | None = None, transport=None) -> bool:
    """Attempt each fixed feed once per Shanghai day; failures wait until tomorrow."""
    current = int(time.time()) if now is None else now
    day = datetime.fromtimestamp(current, timezone.utc).astimezone(SHANGHAI_TZ).date().isoformat()
    path = _path(state_dir)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o750)
    with (path.parent / "lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o640)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        state = _load(path)
        if state["last_attempt_day"] >= day:
            return False
        # Persist before the first request: a crash cannot trigger a second daily crawl.
        state["last_attempt_day"] = day
        state["candidates"] = _unexpired(state["candidates"], current)
        _save(path, state)
        for source in SOURCES:
            try:
                candidates = await _fetch(source, current, transport=transport)
            except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, ET.ParseError) as exc:
                logger.warning("Global interest source %s failed: %s", source[0], type(exc).__name__)
                continue
            state["candidates"] = [item for item in state["candidates"] if item.get("source") != source[0]] + candidates
            _save(path, state)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect the daily global public interest candidates")
    parser.add_argument("state_dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(collect_daily(args.state_dir, logger=logging.getLogger("gaoji.global_interest")))


if __name__ == "__main__":
    main()
