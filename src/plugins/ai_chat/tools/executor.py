"""Registered adapters for the scoped sandbox, browser and conversation executor."""

from __future__ import annotations

import json
from functools import partial

from ..ai_tools import (
    BROWSER_TOOLS,
    CONVERSATION_TOOLS,
    SANDBOX_TOOLS,
    SOURCE_TOOLS,
    VIEW_BILIBILI_TOOL,
    VIEW_BILIBILI_TOOL_NAME,
    NIX_SEARCH_TOOL_NAME,
    SANDBOX_CREATE_TOOL_NAME,
    SANDBOX_LIST_TOOL_NAME,
    SANDBOX_EXEC_TOOL_NAME,
    ToolDefinition,
)
from .registry import FunctionTool
from .state import ToolContext, TurnState


def enabled(context: ToolContext, *, capability: str, name: str) -> bool:
    if capability not in context.capabilities or context.agent_executor is None:
        return False
    if name == VIEW_BILIBILI_TOOL_NAME:
        return "shared_content" not in context.capabilities
    if capability == "sandbox":
        return context.agent_executor.sandbox_manager is not None and not (
            name == NIX_SEARCH_TOOL_NAME
            and context.app.settings.sandbox_backend == "vm"
        )
    if capability == "browser":
        return context.agent_executor.browser_manager is not None
    if capability == "shared_content":
        return context.agent_executor.source_store is not None
    return True


def project_sandbox(definition: ToolDefinition, context: ToolContext) -> None:
    if context.app.settings.sandbox_backend != "vm":
        return
    function = definition["function"]
    if function["name"] == SANDBOX_CREATE_TOOL_NAME:
        function["description"] = (
            "创建独立 Debian KVM 虚拟机沙盒，工作目录 /workspace。"
            "文件保存在该沙盒的独立磁盘中，直到交付后按任务策略回收。"
        )
    elif function["name"] == SANDBOX_LIST_TOOL_NAME:
        function["description"] = "列出当前用户拥有的 KVM 虚拟机沙盒及状态。"
    elif function["name"] == SANDBOX_EXEC_TOOL_NAME:
        function["description"] = (
            "在 Debian 虚拟机的 /workspace 中运行 shell 命令。"
            "缺少工具时可将 Debian apt 软件包名放进 packages，"
            "会在该虚拟机内安装；不确定包名先运行 apt-cache search。"
        )
        function["parameters"]["properties"]["packages"]["description"] = (
            "要在该虚拟机内安装的 Debian apt 软件包名。"
        )


async def execute(turn: TurnState, arguments: dict[str, object], *, name: str) -> str:
    executor = turn.agent_executor
    if executor is not None:
        result = await executor.execute(name, arguments)
        if result is not None:
            return result
    return json.dumps(
        {
            "ok": False,
            "code": "tool_unavailable",
            "error": "当前会话的工具执行器不可用。",
        },
        ensure_ascii=False,
    )


HANDLERS = tuple(
    FunctionTool(
        definition,
        partial(enabled, capability=capability, name=definition["function"]["name"]),
        partial(execute, name=definition["function"]["name"]),
        project_sandbox if capability == "sandbox" else None,
    )
    for capability, definitions in (
        ("sandbox", SANDBOX_TOOLS),
        ("conversation", [*CONVERSATION_TOOLS, VIEW_BILIBILI_TOOL]),
        ("shared_content", SOURCE_TOOLS),
        ("browser", BROWSER_TOOLS),
    )
    for definition in definitions
)
