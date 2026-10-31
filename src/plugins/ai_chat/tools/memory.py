"""memory_append and memory_write: the conversation's own memory file."""

from __future__ import annotations

import json
import re

from ..ai_tools import MEMORY_APPEND_TOOL, MEMORY_WRITE_TOOL
from .registry import FunctionTool
from .state import ToolContext, TurnState, structured_tool_source

MAX_MEMORY_CHARS = 20_000


def looks_like_secret(content: str) -> bool:
    return bool(re.search(r"\bsk-[A-Za-z0-9_-]{10,}\b", content) or re.search(
        r"(?i)(?:api[_ -]?key|access[_ -]?token|password|secret|密码|验证码)"
        r"\s*[:=：]\s*\S+", content,
    ))


def enabled(context: ToolContext) -> bool:
    return "memory" in context.capabilities and getattr(context.app, "conversations", None) is not None


def _result(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _write(turn: TurnState, text: str, *, append: bool) -> str:
    source = structured_tool_source(turn.context)
    if source is None:
        return _result({"ok": False, "error": "当前没有可写的会话。"})
    if not text.strip():
        return _result({"ok": False, "error": "内容不能为空。"})
    if looks_like_secret(text):
        return _result({"ok": False, "error": "记忆里不能存密码、密钥或验证码。"})
    store = turn.context.app.conversations
    if not append and len(text) > MAX_MEMORY_CHARS:
        return _result({"ok": False, "error": f"整份记忆太长了，压到 {MAX_MEMORY_CHARS} 字以内。"})
    content = await turn.context.app.db_call(store.write_memory, source.scope.key, text, append=append)
    return _result({"ok": True, "chars": len(content)})


async def memory_append(turn: TurnState, arguments: dict[str, object]) -> str:
    line = " ".join(str(arguments.get("line") or "").split())[:500]
    if line and not line.startswith("- "):
        line = "- " + line
    return await _write(turn, line, append=True)


async def memory_write(turn: TurnState, arguments: dict[str, object]) -> str:
    return await _write(turn, str(arguments.get("content") or ""), append=False)


HANDLERS = (
    FunctionTool(MEMORY_APPEND_TOOL, enabled, memory_append, platform_neutral=True),
    FunctionTool(MEMORY_WRITE_TOOL, enabled, memory_write, platform_neutral=True),
)
