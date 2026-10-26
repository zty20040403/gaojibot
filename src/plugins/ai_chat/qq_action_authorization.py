from __future__ import annotations

import asyncio
import inspect
import json
from functools import wraps
from typing import Any

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher, current_bot, current_event, current_matcher

from src.bot_security.service import approved_request, assert_approved
from src.bot_security.store import SecurityError, canonical
from .tool_policy import tool_enabled

COMMANDS = {
    "handle_model_command": "修改会话模型", "handle_effort_command": "修改推理强度",
    "handle_shell_command": "执行沙盒命令", "handle_memory_command": "修改记忆",
    "handle_pin_command": "固定消息", "handle_unpin_command": "取消固定消息",
    "handle_task_stop": "停止任务", "handle_ai_reset": "清空会话记忆",
    "handle_clear_data": "清空数据与配置", "handle_control_command": "修改任务或消息",
}

AUTOMATIC_SANDBOX_TOOLS = frozenset({
    "sandbox_create", "sandbox_list", "sandbox_exec", "sandbox_destroy",
    "sandbox_write_file", "sandbox_read_file", "nix_search",
    "import_file_to_sandbox", "import_agent_artifact",
    "send_file_from_sandbox", "send_image_from_sandbox",
})


def requires_mobile_tool(name: str) -> bool:
    # Important server mutations are checked at FleetAuthorization, where the
    # actual remote contract is known. Other tools keep their native access checks.
    return False


def command_changes_state(name: str, event: Any, args: Any) -> bool:
    text = args.extract_plain_text().strip() if isinstance(args, Message) else ""
    if name in {"handle_model_command", "handle_effort_command"}:
        return bool(text)
    if name == "handle_shell_command":
        return text.casefold() not in {"", "help", "帮助", "status", "状态"}
    if name == "handle_memory_command":
        return bool(text) and text.split()[0].casefold() not in {"列表", "list", "ls", "查看", "history", "历史", "audit", "审计"}
    if name == "handle_control_command":
        return event.get_plaintext().strip().split()[0].lower() in {"/kill", "/pin", "/unpin"}
    return name in COMMANDS


def command_payload(name: str, event: Any, args: Any) -> dict[str, Any]:
    return {"handler": name, "event": event.model_dump(mode="json"), "args": str(args) if isinstance(args, Message) else ""}


def command_targets() -> dict[str, Any]:
    request = approved_request.get()
    if request and request["kind"] == "command":
        return request["payload"].get("targets", {})
    return {}


async def freeze_targets(service, name, event, args) -> dict[str, Any]:
    text = args.extract_plain_text().strip() if isinstance(args, Message) else ""
    plain = event.get_plaintext().strip()
    if name == "handle_task_stop" or (name == "handle_control_command" and plain.split()[0].lower() == "/kill"):
        requested = text if name == "handle_task_stop" else plain.partition(" ")[2].strip()
        if requested:
            return {"task_id": requested}
        tasks = (service.context.running_tasks.list_for_group(event.group_id) if isinstance(event, GroupMessageEvent)
                 else service.context.running_tasks.list_for(service.services.chat._conversation_id(event)))
        if not tasks:
            raise SecurityError("当前没有运行任务，无需发送停止口令", 409)
        return {"task_id": tasks[-1].task_id}
    if name == "handle_shell_command" and text.casefold() in {"reset", "重建", "destroy", "销毁"}:
        items = await service.context.sandbox_manager.list(service._shell_owner(event))
        return {"sandbox_ids": sorted(str(item["sandbox_id"]) for item in items if item.get("purpose") == "shell")}
    return {}


def mobile_command(function):
    signature = inspect.signature(function)

    @wraps(function)
    async def guarded(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        service, event = bound.arguments["self"], bound.arguments["event"]
        command_args = bound.arguments.get("args")
        if function.__name__ == "handle_shell_command" or not command_changes_state(function.__name__, event, command_args):
            return await function(*args, **kwargs)
        payload = command_payload(function.__name__, event, command_args)
        if approved_request.get() is not None:
            payload["targets"] = command_targets()
            assert_approved("command", payload)
            return await function(*args, **kwargs)
        mobile = getattr(service.context, "mobile_authorization", None)
        try:
            if mobile is None:
                raise SecurityError("账户认证未初始化，管理操作已锁定")
            account = await asyncio.to_thread(mobile.store.account_for_qq, str(event.user_id))
            if account is None:
                raise SecurityError("仅绑定 QQ 的管理员可以执行管理变更")
            await asyncio.to_thread(mobile.store.record_action, account["account_id"],
                "command.requested", function.__name__, str(getattr(event, "group_id", "private")))
            return await function(*args, **kwargs)
        except SecurityError as exc:
            response = str(exc)
        bot = bound.arguments.get("bot") or get_bots().get(str(event.self_id))
        if bot is None:
            raise SecurityError("QQ 机器人未连接", 503)
        await bot.send(event, response, auto_escape=True)
        return None

    return guarded


def tool_payload(name: str, arguments: dict[str, Any], event: Any, user_text: str) -> dict[str, Any]:
    return {"tool": name, "arguments": arguments, "event": event.model_dump(mode="json"), "user_text": user_text}


async def propose_tool(context: Any, name: str, arguments: dict[str, Any], event: Any, user_text: str) -> str:
    mobile = getattr(context, "mobile_authorization", None)
    try:
        if mobile is None:
            raise SecurityError("手机口令授权未初始化，操作不会执行")
        result = await mobile.propose_qq(str(event.user_id), kind="tool", payload=tool_payload(name, arguments, event, user_text),
            summary=f"机器人操作：{name}\n发起 QQ：{event.user_id}\n会话：{getattr(event, 'group_id', '私聊')}\n完整参数：{json.dumps(arguments, ensure_ascii=False, indent=2)}")
        return canonical({**result, "executed": False, "next_action": "等待本人在 QQ 私聊中确认。模型不能确认口令，不能声称已执行，也不要重复创建同一操作。"})
    except SecurityError as exc:
        return canonical({"ok": False, "executed": False, "error": str(exc)})


def register_qq_executors(services: Any) -> None:
    mobile = services.context.mobile_authorization
    if mobile is None:
        return

    def event_from(payload):
        raw = payload["event"]
        event_type = GroupMessageEvent if raw["message_type"] == "group" else PrivateMessageEvent
        return event_type.model_validate(raw)

    async def execute_command(request: dict[str, Any]) -> dict[str, Any]:
        payload = request["payload"]
        assert_approved("command", payload)
        name = payload["handler"]
        if name not in COMMANDS:
            raise SecurityError("未登记的管理指令")
        event = event_from(payload)
        if str(event.user_id) != request["qq_id"] or str(event.self_id) != request["bot_id"]:
            raise SecurityError("QQ 身份与批准不匹配")
        bot = get_bots().get(request["bot_id"])
        if bot is None:
            raise SecurityError("QQ 机器人未连接", 503)
        function = getattr(services.commands, name)
        arguments: dict[str, Any] = {"event": event}
        parameters = inspect.signature(function).parameters
        if "args" in parameters:
            arguments["args"] = Message(payload["args"])
        if "bot" in parameters:
            arguments["bot"] = bot
        tokens = (current_bot.set(bot), current_event.set(event), current_matcher.set(Matcher()))
        try:
            await function(**arguments)
        except FinishedException:
            pass
        finally:
            current_bot.reset(tokens[0]); current_event.reset(tokens[1]); current_matcher.reset(tokens[2])
        return {"ok": True, "message": "已执行已核对的指令，结果已回复原会话。"}

    async def execute_tool(request: dict[str, Any]) -> dict[str, Any]:
        payload = request["payload"]
        assert_approved("tool", payload)
        event = event_from(payload)
        if str(event.user_id) != request["qq_id"] or str(event.self_id) != request["bot_id"]:
            raise SecurityError("QQ 身份与批准不匹配")
        if not tool_enabled(payload["tool"]):
            raise SecurityError("该工具已停用，请重新核对")
        bot = get_bots().get(request["bot_id"])
        if bot is None:
            raise SecurityError("QQ 机器人未连接", 503)
        result = await services.tools._ask_ai(bot, event, payload["user_text"], _approved_call=payload)
        try:
            parsed = json.loads(str(result))
        except (ValueError, TypeError):
            parsed = {"ok": False, "message": str(result)}
        return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}

    mobile.executors.update(command=execute_command, tool=execute_tool)
