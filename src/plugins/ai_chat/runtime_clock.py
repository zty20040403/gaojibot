from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_WEEKDAYS = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)


def runtime_clock_prompt(now: datetime | None = None) -> str:
    """Render a fresh, timezone-explicit clock line for an LLM request."""
    current = (
        datetime.now(SHANGHAI_TZ)
        if now is None
        else now.astimezone(SHANGHAI_TZ)
    )
    raw_offset = current.strftime("%z")
    offset = f"{raw_offset[:3]}:{raw_offset[3:]}"
    return (
        f"当前时间是 {current.strftime('%Y-%m-%d %H:%M')}"
        f"（Asia/Shanghai，UTC{offset}，{_WEEKDAYS[current.weekday()]}）。"
        "这是服务器为本次请求动态生成的时间；回答当前日期或时间时以它为准。"
    )
