"""Conversation workspace direct execution, multimodal inspection, and native file/image delivery."""
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
        "description": "从当前会话沙盒读取并发送任意格式文件（上限 200 MB；图片含 GIF 直接显示成图，上限 10 MB；传 20 MB 以上大文件前先用 say 说一声）。",
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

_SEND_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "send_image",
        "description": "把本会话容器 /work 里的图片（PNG/JPEG/GIF，上限 10 MB；WebP 自动转 PNG）直接发到当前会话显示。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "/work 下的图片路径，例如 /work/out.png。",
                },
                "filename": {
                    "type": "string",
                    "description": "可选显示文件名（保留图片扩展名）。",
                },
            },
            "required": ["path"],
        },
    },
}

_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": (
            "在本会话的持久化 Linux 容器（/work）里直接执行 bash 命令（可联网，预装 python3/node/ffmpeg/yt-dlp/poppler/reportlab/gaoji）。"
            "几秒到两分钟内能跑完的命令直接同步执行；耗时更长的命令设 background=true 起成容器后台单元，结束时自动唤醒前台。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 bash 命令。",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "前台等待超时秒数，默认 120，最大 300。",
                },
                "workdir": {
                    "type": "string",
                    "description": "工作目录，默认 /work。",
                },
                "background": {
                    "type": "boolean",
                    "description": "设为 true 时作为容器后台单元异步运行，跑完通知前台。",
                },
                "title": {
                    "type": "string",
                    "description": "background=true 时的简短任务标题。",
                },
            },
            "required": ["command"],
        },
    },
}

_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "video",
        "description": (
            "直接看本会话容器 /work 里的视频片段（容器内 ffmpeg 按每秒 1 帧、最长边 640、单声道压制，宿主不解码；单次最多 10 分钟、32 MiB），"
            "当轮就能看到画面并听见声音。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "/work 下的视频路径（如 import_file_to_sandbox 返回的 /work/chat/... 或下载的视频）。",
                },
                "start": {
                    "type": ["number", "string"],
                    "description": "可选起始时间（秒数或 \"1:30\" / \"0:05:00\"）。",
                },
                "end": {
                    "type": ["number", "string"],
                    "description": "可选结束时间（单段跨度不超过 10 分钟 / 600 秒）。",
                },
            },
            "required": ["path"],
        },
    },
}

_RUN_CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": (
            "在本会话容器里运行一段异步 JavaScript 函数体（参数 tools、gaoji、args）："
            "支持 Promise.all 并发调用多个工具（tools.web_search / tools.web_fetch / tools.history / tools.say / tools.send_image / tools.send_file / tools.show / tools.video），"
            "以及通过 gaoji.page() / gaoji.snapshot(page) 操作本会话常驻 Chromium 浏览器（标签页跨轮保留，严格遵守 skills/browser.md 登录规则）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "async 函数体代码（直接 return 结果；内部可用 await tools.<name>({...}) 和 await gaoji.page()）。",
                },
                "args": {
                    "type": "object",
                    "description": "可选传入脚本的 args 对象。",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "执行超时秒数，默认 120，最大 300。",
                },
            },
            "required": ["code"],
        },
    },
}

_DIRECT_TOOLS = (_SHELL_TOOL, _SEND_IMAGE_TOOL, _VIDEO_TOOL, _RUN_CODE_TOOL)
_DIRECT_NAMES = frozenset(t["function"]["name"] for t in _DIRECT_TOOLS)


def enabled(context):
    executor = context.agent_executor
    return (
        "sandbox" in context.capabilities
        and isinstance(executor, OwnedSandboxExecutor)
        and executor.generation_enabled
        and executor.matches(context.inbound or context.background_source)
    )


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
    if call_name in _DIRECT_NAMES:
        result = await executor.execute(
            call_name, call_args, model_profile=getattr(turn, "model_profile", None),
        )
    else:
        result = await executor.execute(call_name, call_args)
    raw_text = str(result) if result is not None else ""
    if name in {"send_file", "send_file_from_sandbox", "send_image", "run_code"} and raw_text:
        try:
            payload = json.loads(raw_text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("ok") is True and (
                payload.get("delivery_state") in {"stored", "waiting", "committed"}
                or payload.get("delivered") is True
            ):
                turn.delivered = True
            elif (
                isinstance(result, str)
                and payload.get("ok") is False
                and "error" not in payload
                and isinstance(payload.get("code"), str)
            ):
                payload["error"] = payload["code"]
                result = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return result


def project(definition, context):
    function = definition["function"]
    name = function["name"]
    if name in {"send_file", *_DIRECT_NAMES}:
        return
    function["description"] = {
        "sandbox_read_file": "读取本会话工作区里的文本文件（例如报告或输出）。",
        "send_file_from_sandbox": "从本会话工作区发送任意格式文件（上限 200 MB；图片含 GIF 直接显示成图，上限 10 MB）。",
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
    for definition in (
        *[d for d in SANDBOX_TOOLS if d["function"]["name"] in _NAMES],
        _SEND_FILE_TOOL,
        *_DIRECT_TOOLS,
    )
)
