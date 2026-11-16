"""Other bots in a group are ordinary speakers, with a bound on bot-to-bot chat.

The model is told who is a bot and asked to wind down on its own; this module is
the hard stop for when it does not. A run of bot messages with no human message
in between may get at most ``STREAK_LIMIT`` replies from us. Our own outbound
messages do not reset the run, so two bots answering each other stop quickly.
"""
from __future__ import annotations

import json
from typing import Any

STREAK_LIMIT = 3
# A human message older than this no longer counts as "someone is talking".
WINDOW_SECONDS = 600
_SCAN = 40
SUFFIX = "（机器人）"


def display(decoded: Any) -> str:
    name = (decoded.sender_display or "").strip()
    if not getattr(decoded, "sender_is_bot", False):
        return decoded.sender_display
    return f"{name}{SUFFIX}" if name else "另一个机器人"


def is_bot_message(row: Any) -> bool:
    try:
        raw = json.loads(row["raw_event_json"] or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("sender_is_bot") is True


def bot_streak(cursor: Any, endpoint_key: str, now: int) -> int:
    """Inbound bot messages since the latest human message in this endpoint."""
    rows = cursor.execute(
        """SELECT direction, raw_event_json FROM messages
        WHERE source_endpoint_key=? AND occurred_at>=?
        ORDER BY canonical_message_id DESC LIMIT ?""",
        (endpoint_key, int(now) - WINDOW_SECONDS, _SCAN),
    ).fetchall()
    streak = 0
    for row in rows:
        if row["direction"] != "inbound":
            continue
        if not is_bot_message(row):
            break
        streak += 1
    return streak


def exchange_exhausted(cursor: Any, endpoint_key: str, now: int) -> bool:
    return bot_streak(cursor, endpoint_key, now) > STREAK_LIMIT
