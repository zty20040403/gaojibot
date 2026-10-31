"""history: search or read the current conversation's chat record."""

from __future__ import annotations

from datetime import datetime
import json
import re

from ..ai_tools import HISTORY_TOOL
from ..timeline import is_noise, render_line
from .registry import FunctionTool
from .state import ToolContext, TurnState, structured_tool_source

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
_ID = re.compile(r"^#?([1-9][0-9]{0,18})$")


def enabled(context: ToolContext) -> bool:
    return "context" in context.capabilities and context.app.message_ledger is not None


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _boundary(ledger, scope, value: str, *, end: bool) -> int | None:
    """A date, date-time or #id as a message id; dates count whole days for `to`."""
    value = value.strip()
    matched = _ID.match(value)
    if matched:
        return int(matched.group(1))
    if not _DATE.match(value):
        raise ValueError(value)
    moment = datetime.strptime(value, "%Y-%m-%d %H:%M" if " " in value else "%Y-%m-%d")
    stamp = int(moment.timestamp()) + (86400 if end and " " not in value else 0)
    found = ledger.first_id_at(scope, stamp)
    return found if found is not None else (2**62 if end else None)


async def history(turn: TurnState, arguments: dict[str, object]) -> str:
    app = turn.context.app
    source = structured_tool_source(turn.context)
    if source is None:
        return _error("当前没有可查询的会话。")
    scope, ledger = source.scope, app.message_ledger
    query = str(arguments.get("query") or "").strip()
    who = str(arguments.get("who") or "").strip()[:50]
    try:
        limit = min(max(int(arguments.get("limit") or (10 if query else 50)), 1), 30 if query else 100)
    except (TypeError, ValueError):
        return _error("limit 必须是整数。")
    around = str(arguments.get("around") or "").strip()
    if around:
        matched = _ID.match(around)
        if not matched:
            return _error("around 要写成 #消息编号。")
        center = int(matched.group(1))
        before = await app.db_call(ledger.history, scope, before_id=center + 1, limit=11, newest=True)
        after = await app.db_call(ledger.history, scope, after_id=center, limit=10)
        lines = [render_line(message, max_chars=4000 if message.canonical_message_id == center else 600)
                 for message in (*before, *after)]
        return json.dumps({"ok": True, "lines": lines or ["（没有这样的消息）"]}, ensure_ascii=False)
    try:
        start = _boundary(ledger, scope, str(arguments["from"]), end=False) if arguments.get("from") else None
        end = _boundary(ledger, scope, str(arguments["to"]), end=True) if arguments.get("to") else None
    except ValueError:
        return _error("from/to 要写成 YYYY-MM-DD、YYYY-MM-DD HH:MM 或 #消息编号。")
    after_id = (start - 1) if start is not None else 0
    if query:
        terms = tuple(dict.fromkeys(term[:50] for term in query.split()))[:8]
        found = await app.db_call(ledger.history, scope, terms=terms, who=who, after_id=after_id,
                                  before_id=end, limit=limit * 3, newest=True)
        found = [message for message in found if not is_noise(message)]
        found.sort(key=lambda message: (-sum(term.lower() in message.prompt_text.lower() for term in terms),
                                        -message.canonical_message_id))
        lines = [render_line(message, max_chars=300) for message in found[:limit]]
        return json.dumps({"ok": True, "results": lines or ["（没有搜到）"],
                           "hint": "用 around=#编号 读某条前后的原文"}, ensure_ascii=False)
    if start is None and end is None and not who:
        return _error("给 query 搜索，或者用 from/to/who/around 指定要读的原文。")
    # Fetch extra: noise rows are dropped after the query, and must not hide a next page.
    fetched = limit * 3 + 1
    raw = await app.db_call(ledger.history, scope, who=who, after_id=after_id, before_id=end,
                            limit=fetched, newest=start is None)
    rows = [message for message in raw if not is_noise(message)]
    more = len(rows) > limit or len(raw) == fetched
    rows = rows[:limit] if start is not None else rows[-limit:]
    lines = [render_line(message, max_chars=600) for message in rows]
    payload: dict[str, object] = {"ok": True, "lines": lines or ["（没有这样的消息）"]}
    if more and rows:
        payload["next"] = ({"from": f"#{rows[-1].canonical_message_id + 1}"} if start is not None
                           else {"to": f"#{rows[0].canonical_message_id}"})
    return json.dumps(payload, ensure_ascii=False)


HANDLERS = (FunctionTool(HISTORY_TOOL, enabled, history, platform_neutral=True),)
