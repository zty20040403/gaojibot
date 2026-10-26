"""Read authoritative alert rankings, not notification counts in chat history."""

from __future__ import annotations

import asyncio
import json

from src.bot_storage import DatabaseError
from ..ai_tools import QUERY_ALERTS_TOOL
from .registry import FunctionTool
from .state import ToolContext, TurnState


def enabled(context: ToolContext) -> bool:
    return "alerts" in context.capabilities and context.app.alert_store is not None


def _compact(item: dict[str, object]) -> dict[str, object]:
    return {
        "target": str(item.get("incident_key") or ""),
        "severity": str(item.get("severity") or ""),
        "status": str(item.get("status") or ""),
        "event_count": int(item.get("event_count") or 0),
        "active_event_count": int(item.get("active_event_count") or 0),
        "summary": str(item.get("summary") or "")[:300],
        "last_seen_at": int(item.get("last_seen_at") or 0),
    }


async def query(turn: TurnState, arguments: dict[str, object]) -> str:
    app = turn.context.app
    try:
        days = min(max(int(arguments.get("days") or 7), 1), 365)
        limit = min(max(int(arguments.get("limit") or 10), 1), 20)
        snapshot = await asyncio.to_thread(
            app.alert_store.snapshot, days=days, limit=100
        )
        ranked = await asyncio.to_thread(
            app.alert_store.rank_incidents, days=days, limit=limit
        )
        incidents = (
            snapshot.get("incidents")
            if isinstance(snapshot, dict)
            and isinstance(snapshot.get("incidents"), list)
            else []
        )
        details = {
            str(item.get("incident_key") or ""): item
            for item in incidents
            if isinstance(item, dict)
        }
        ranked_items = (
            ranked.get("items")
            if isinstance(ranked, dict) and isinstance(ranked.get("items"), list)
            else []
        )
        ranking = [
            {**details.get(str(item.get("incident_key") or ""), {}), **item}
            for item in ranked_items
            if isinstance(item, dict)
        ]
        active = sorted(
            (
                item
                for item in incidents
                if isinstance(item, dict)
                and int(item.get("active_event_count") or 0) > 0
            ),
            key=lambda item: (
                int(item.get("active_event_count") or 0),
                int(item.get("last_seen_at") or 0),
            ),
            reverse=True,
        )[:limit]
        return json.dumps(
            {
                "ok": True,
                "authoritative": True,
                "timezone": str(snapshot.get("timezone") or "Asia/Shanghai"),
                "days": days,
                "range_start": int(ranked.get("range_start") or 0),
                "generated_at": int(ranked.get("generated_at") or 0),
                "summary": snapshot.get("summary") or {},
                "ranking": [_compact(item) for item in ranking],
                "active": [_compact(item) for item in active],
                "ranking_basis": "按同一 incident_key 在统计周期内的独立告警事件数降序；不是按群聊通知条数。",
            },
            ensure_ascii=False,
        )
    except (OSError, RuntimeError, TypeError, ValueError, DatabaseError) as exc:
        app.logger.warning(f"Alert query tool failed: {exc}")
        return json.dumps(
            {"ok": False, "error": "权威告警库查询失败，请稍后重试。"},
            ensure_ascii=False,
        )


HANDLERS = (FunctionTool(QUERY_ALERTS_TOOL, enabled, query),)
