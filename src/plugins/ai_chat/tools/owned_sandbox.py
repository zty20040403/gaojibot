"""Conversation workspace file reading and direct QQ Official file sending."""
from __future__ import annotations

from copy import deepcopy
from functools import partial
import json

from src.plugins.ai_chat.ai_tools import SANDBOX_TOOLS
from src.plugins.ai_chat.tools.registry import FunctionTool

from ..application.owned_sandbox import OwnedSandboxExecutor

_NAMES = frozenset({"sandbox_read_file", "send_file_from_sandbox"})

_SEND_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "send_file",
        "description": "从当前会话沙盒读取并发送任意格式文件（图片含 GIF 直接显示成图，上限 100 MB）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区文件的相对路径，或 /work/、/workspace/ 开头的绝对路径。",
                },
                "name": {
                    "type": "string",
                    "description": "可选显示文件名，保留实际扩展名。",
                },
            },
            "required": ["path"],
        },
    },
}


def enabled(context):
    executor = context.agent_executor
    return ("sandbox" in context.capabilities and isinstance(executor, OwnedSandboxExecutor)
            and executor.generation_enabled and executor.matches(context.inbound or context.background_source))


async def execute(turn, arguments, *, name):
    executor = turn.context.agent_executor
    if not enabled(turn.context):
        return '{"ok":false,"code":"generation_unavailable","error":"当前会话沙盒不可用。","message_accepted":false}'
    call_name, call_args = name, arguments
    if name == "send_file" and isinstance(arguments, dict):
        call_name = "send_file_from_sandbox"
        call_args = {"path": arguments.get("path")}
        if arguments.get("name") or arguments.get("filename"):
            call_args["filename"] = arguments.get("name") or arguments.get("filename")
    result = await executor.execute(call_name, call_args)
    if name in {"send_file", "send_file_from_sandbox"} and isinstance(result, str):
        try:
            payload = json.loads(result)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("ok") is True and payload.get("delivery_state") in {"stored", "waiting", "committed"}:
                turn.delivered = True
            elif payload.get("ok") is False and "error" not in payload and isinstance(payload.get("code"), str):
                payload["error"] = payload["code"]
                result = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return result


def project(definition, context):
    function = definition["function"]
    name = function["name"]
    if name == "send_file":
        return
    function["description"] = {
        "sandbox_read_file": "读取本会话工作区里的文本，例如后台报告全文。生成、修改文件请交给 agent。",
        "send_file_from_sandbox": "从本会话工作区发送任意格式文件（图片含 GIF 直接显示成图，上限 100 MB）。",
    }[name]
    function["parameters"]["properties"].pop("sandbox_id", None)
    function["parameters"]["required"] = ["path"]
    function["parameters"]["properties"]["path"]["description"] = "工作区文件的相对路径，或 /work/、/workspace/ 开头的绝对路径。"
    if name == "send_file_from_sandbox":
        function["parameters"]["properties"]["filename"]["description"] = "显示文件名，保留实际扩展名。"


HANDLERS = tuple(
    FunctionTool(
        deepcopy(definition),
        enabled,
        partial(execute, name=definition["function"]["name"]),
        project=project,
        platform_neutral=True,
        required_features=frozenset(),
    )
    for definition in (*[d for d in SANDBOX_TOOLS if d["function"]["name"] in _NAMES], _SEND_FILE_TOOL)
)
