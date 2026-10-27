"""Global sticker retrieval and per-turn visual reply preparation."""

from __future__ import annotations

import json
import time

from nonebot.adapters.onebot.v11 import MessageSegment

from src.bot_storage import DatabaseError
from ..ai_tools import FIND_STICKERS_TOOL, SEND_STICKER_TOOL, SEND_QQ_FACE_TOOL
from ..media_library import choose_sticker_candidate, requests_sticker_variation
from ..stickers import qq_face_message, random_sticker_message
from .registry import FunctionTool
from .state import ToolContext, TurnState


def search_enabled(context: ToolContext) -> bool:
    return "media" in context.capabilities


def reply_enabled(context: ToolContext) -> bool:
    return "stickers" in context.capabilities


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


async def send_sticker(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.visual_reply_segment is not None:
        return json.dumps(
            {"ok": True, "message": "本轮表情已经准备发送。"},
            ensure_ascii=False,
        )
    media_handle = str(arguments.get("media_handle") or "").strip()
    query = str(arguments.get("query") or "").strip()
    require_different = requests_sticker_variation(turn.user_text)
    record = None
    searched_stickers = False
    if turn.context.app.media_library is not None and query:
        searched_stickers = True
        try:
            candidates = await turn.context.app.media_library.search_stickers(
                query,
                limit=10,
            )
            record = choose_sticker_candidate(
                candidates,
                allow_recent_fallback=not require_different,
            )
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            DatabaseError,
        ) as exc:
            turn.context.app.logger.warning(
                f"Sticker search-and-send tool failed: {exc}"
            )
    elif (
        turn.context.app.media_library is not None
        and media_handle in turn.sticker_handles_this_turn
        and media_handle.startswith("media#")
    ):
        try:
            media_id = int(media_handle.removeprefix("media#"))
            record = await turn.context.app.db_call(
                turn.context.app.media_library.get_sticker, media_id
            )
            if (
                require_different
                and record is not None
                and record.last_sent_at is not None
                and record.last_sent_at >= int(time.time()) - 300
            ):
                record = None
        except (OSError, RuntimeError, TypeError, ValueError, DatabaseError):
            record = None
    if (
        turn.context.app.media_library is not None
        and record is None
        and not searched_stickers
    ):
        try:
            candidates = await turn.context.app.media_library.search_stickers(
                query or turn.user_text,
                limit=10,
            )
            record = choose_sticker_candidate(
                candidates,
                allow_recent_fallback=not require_different,
            )
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            DatabaseError,
        ) as exc:
            turn.context.app.logger.warning(
                f"Sticker search-and-send tool failed: {exc}"
            )
    if record is not None and record.is_sticker and record.safety == "safe":
        turn.visual_reply_segment = MessageSegment.image(
            record.storage_path.read_bytes()
        )
        (
            await turn.context.app.db_call(
                turn.context.app.media_library.mark_sent, record.media_id
            )
        )
        return json.dumps(
            {
                "ok": True,
                "message": "已从全局表情库找到并准备发送。",
                "media_handle": record.handle,
                "summary": record.summary,
            },
            ensure_ascii=False,
        )
    if turn.context.app.media_library is not None:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "没有别的匹配表情。" if require_different else "没有这个表情。"
                ),
            },
            ensure_ascii=False,
        )
    sticker_message = random_sticker_message()
    if isinstance(sticker_message, str):
        return json.dumps(
            {"ok": False, "error": sticker_message},
            ensure_ascii=False,
        )
    turn.visual_reply_segment = sticker_message
    return json.dumps(
        {"ok": True, "message": "表情包已经准备发送。"},
        ensure_ascii=False,
    )


async def send_face(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.visual_reply_segment is not None:
        return json.dumps(
            {"ok": True, "message": "本轮表情已经准备发送。"},
            ensure_ascii=False,
        )
    expression = str(arguments.get("expression", "随机")).strip()
    face_message = qq_face_message(expression)
    if isinstance(face_message, str):
        return json.dumps(
            {"ok": False, "error": face_message},
            ensure_ascii=False,
        )
    turn.visual_reply_segment = face_message
    return json.dumps(
        {"ok": True, "message": "QQ 自带表情已经准备发送。"},
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(FIND_STICKERS_TOOL, search_enabled, find_stickers),
    FunctionTool(SEND_STICKER_TOOL, reply_enabled, send_sticker),
    FunctionTool(SEND_QQ_FACE_TOOL, reply_enabled, send_face),
)
