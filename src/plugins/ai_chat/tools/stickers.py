"""Global sticker search over the vision-tagged library."""

from __future__ import annotations

import json

from src.bot_storage import DatabaseError
from ..ai_tools import FIND_STICKERS_TOOL
from .registry import FunctionTool
from .state import ToolContext, TurnState


def search_enabled(context: ToolContext) -> bool:
    return "media" in context.capabilities


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
        turn.sticker_handles_this_turn.update(item.handle for item in records)
    except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
        turn.context.app.logger.warning(f"Media search tool failed: {exc}")
        records = []
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
                }
                for item in records
            ],
        },
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(FIND_STICKERS_TOOL, search_enabled, find_stickers, platform_neutral=True),
)
