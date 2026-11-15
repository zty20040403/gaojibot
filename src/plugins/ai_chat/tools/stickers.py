"""Global sticker search over the vision-tagged library."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from src.bot_storage import DatabaseError
from ..ai_tools import FIND_STICKERS_TOOL
from .registry import FunctionTool
from .state import ToolContext, TurnState

# A sticker this conversation saw within this window goes behind fresh ones.
RECENT_STICKER_SECONDS = 6 * 3600
_logger = logging.getLogger(__name__)


def search_enabled(context: ToolContext) -> bool:
    return "media" in context.capabilities


async def recent_sends(turn: TurnState) -> dict[int, int]:
    """media_id -> last time this conversation was sent it; empty when unknown."""
    library, scope = turn.context.app.media_library, turn.context.scope
    if library is None or scope is None or not hasattr(library, "recent_sticker_sends"):
        return {}
    try:
        return await asyncio.to_thread(
            library.recent_sticker_sends, scope.key, int(time.time()) - RECENT_STICKER_SECONDS,
        )
    except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
        _logger.warning("Recent sticker lookup failed: %s", exc)
        return {}


def fresh_first(records, recent: dict[int, int]):
    """Keep relevance order, but stickers sent here lately go last, oldest send first."""
    def key(item):
        media_id = getattr(item, "media_id", None)
        return (media_id in recent, recent.get(media_id, 0))
    return sorted(records, key=key)


async def find_stickers(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.context.app.media_library is None:
        return json.dumps(
            {"ok": False, "error": "媒体检索暂时不可用。"},
            ensure_ascii=False,
        )
    query = str(arguments.get("query") or "").strip()
    try:
        limit = min(max(int(arguments.get("limit") or 5), 1), 10)
    except (TypeError, ValueError):
        limit = 5
    if not query:
        return json.dumps(
            {"ok": False, "error": "检索词不能为空。"},
            ensure_ascii=False,
        )
    try:
        records = await turn.context.app.media_library.search_stickers(
            query,
            limit=limit,
        )
        turn.sticker_handles_seen.update(item.handle for item in records)
    except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
        turn.context.app.logger.warning(f"Media search tool failed: {exc}")
        records = []
    recent = await recent_sends(turn)
    records = fresh_first(records, recent)
    return json.dumps(
        {
            "ok": True,
            "query": query,
            "items": [
                {
                    "media_handle": item.handle,
                    "summary": item.summary,
                    "description": item.description,
                    "subjects": list(item.subjects),
                    "actions": list(item.actions),
                    "emotion": list(item.emotions),
                    "usage": list(item.usage),
                    "is_sticker": item.is_sticker,
                    "score": round(item.score, 4),
                    "recently_sent_here": item.media_id in recent,
                }
                for item in records
            ],
        },
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(FIND_STICKERS_TOOL, search_enabled, find_stickers, platform_neutral=True),
)
