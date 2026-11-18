"""Recall the bot's own recently sent message in the current conversation (QQ 2-minute window)."""
from __future__ import annotations

import json
import re

from .registry import FunctionTool
from .state import structured_tool_source

RECALL_MESSAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "recall_message",
        "description": (
            "撤回你自己在当前会话 2 分钟内发出的消息。id 填聊天记录行首 #编号 里的数字（如 #123 填 123）；"
            "只能撤回自己的消息，分段发出的回复每段是一个独立编号。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": ["integer", "string"],
                    "description": "要撤回的你自己的消息编号，例如 123 或 #123。",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
}


def enabled(context) -> bool:
    source = structured_tool_source(context)
    return (
        source is not None
        and source.endpoint.account.platform == "qq-official"
        and callable(getattr(context.platform, "recall_own_message", None))
    )


def _parse_id(raw) -> int | None:
    if type(raw) is int:
        return raw if raw >= 1 else None
    if isinstance(raw, str):
        text = raw.strip()
        m = re.fullmatch(r"(?:msg)?#?([1-9][0-9]{0,17})", text)
        if m:
            return int(m.group(1))
    return None


async def execute(turn, arguments) -> str:
    if not enabled(turn.context):
        return json.dumps({"ok": False, "code": "unavailable", "error": "当前入口不支持撤回消息。"}, ensure_ascii=False)
    cid = _parse_id(arguments.get("id") if isinstance(arguments, dict) else None)
    if cid is None:
        return json.dumps({"ok": False, "code": "invalid_id", "error": "请提供有效的正整数消息编号（如 123）。"}, ensure_ascii=False)
    result = await turn.context.platform.recall_own_message(cid)
    return json.dumps(result, ensure_ascii=False)


HANDLERS = (FunctionTool(RECALL_MESSAGE_TOOL, enabled, execute, platform_neutral=True),)
