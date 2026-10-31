"""SDK-free command parsing and dispatch for trusted platform input.

The host supplies model policy and any usage/binding service. A platform role or
canonical history route never supplies that authority.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import inspect
from typing import Any, Literal

from ..conversation_keys import conversation_key
from ..model_preferences import ModelPreferenceStore
from ..platform_types import InboundMessage
from ..tasks import RunningTaskRegistry


CommandName = Literal["help", "bind", "model", "effort", "usage", "task", "cancel", "memory", "clear"]
CommandStatus = Literal["ok", "unknown", "syntax", "denied", "unavailable", "not_found", "unsupported", "error"]
_ALIASES: dict[str, CommandName] = {
    "memory": "memory", "记忆": "memory",
    "clear": "clear", "清空": "clear", "清空上下文": "clear",
    "help": "help", "帮助": "help",
    "bind": "bind", "绑定": "bind",
    "model": "model", "模型": "model", "切换模型": "model",
    "effort": "effort", "强度": "effort", "推理强度": "effort", "思考强度": "effort", "reasoning": "effort",
    "usage": "usage", "用量": "usage", "token用量": "usage",
    "task": "task", "tasks": "task", "任务": "task", "ps": "task", "status": "task",
    "stop": "cancel", "cancel": "cancel", "停止": "cancel", "取消": "cancel", "取消任务": "cancel", "kill": "cancel",
}
_RESETS = frozenset({"default", "reset", "默认", "重置"})
_STATUS = frozenset({"status", "list", "ls", "状态", "列表"})
_EFFORT_ALIASES = {"最低": "minimal", "低": "low", "中": "medium", "高": "high", "超高": "xhigh", "最高": "max", "关闭": "none"}


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    # Includes binding tokens: never expose this field in repr or diagnostics.
    argument: str = field(default="", repr=False)
    valid: bool = True


def parse_command(text: str) -> ParsedCommand | None:
    """Recognize explicit slash/bang commands, without interpreting opaque IDs."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text.startswith(("/", "!")):
        return None
    parts = text[1:].split(None, 1)
    if not parts:
        return None
    name = _ALIASES.get(parts[0].casefold())
    if name is None:
        return None
    argument = parts[1].strip() if len(parts) == 2 else ""
    # Do not retain an oversized binding token in a parsed request.
    if len(argument) > 4096:
        return ParsedCommand(name, valid=False)
    return ParsedCommand(name, argument)


@dataclass(frozen=True)
class CommandResult:
    status: CommandStatus
    text: str = ""

    @property
    def handled(self) -> bool:
        return self.status != "unknown"


@dataclass(frozen=True)
class CommandPolicy:
    """Explicit host decisions; defaults authorize no model configuration access."""

    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    default_model: str = ""
    default_effort: str = ""
    allow_model_read: bool = False
    allow_model_change: bool = False
    allow_effort_read: bool = False
    allow_effort_change: bool = False
    allow_group_default: bool = False


PolicyCallback = Callable[[InboundMessage], CommandPolicy | Awaitable[CommandPolicy]]
ResultCallback = Callable[[InboundMessage, ParsedCommand], CommandResult | Awaitable[CommandResult]]


@dataclass(frozen=True)
class CommandServices:
    running_tasks: RunningTaskRegistry | None = None
    model_preferences: ModelPreferenceStore | None = None
    reasoning_preferences: ModelPreferenceStore | None = None
    host_policy: PolicyCallback | None = None
    usage: ResultCallback | None = None
    binding_handler: ResultCallback | None = None
    db_call: Callable[..., Any] | None = None
    memory: ResultCallback | None = None
    clear: ResultCallback | None = None


async def _await(value):
    return await value if inspect.isawaitable(value) else value


async def _store(services: CommandServices, function, *args):
    if services.db_call is not None:
        return await _await(services.db_call(function, *args))
    return function(*args)


async def dispatch_command(
    request: ParsedCommand | None,
    inbound: InboundMessage | None = None,
    *,
    services: CommandServices | None = None,
) -> CommandResult:
    if request is None or request.name not in set(_ALIASES.values()):
        return CommandResult("unknown", "未知命令。")
    if inbound is None:
        return CommandResult("error", "命令缺少平台身份。")
    if not request.valid:
        return CommandResult("syntax", "命令参数过长。")
    services = services or CommandServices()
    if request.name == "help":
        if request.argument:
            return CommandResult("syntax", "用法：/help")
        extras = [label for key, label in (("memory", "/memory"), ("clear", "/clear"))
                  if getattr(services, key) is not None]
        commands = "/help、/bind、/model、/effort、/usage、/task、/cancel" + ("、" + "、".join(extras) if extras else "")
        return CommandResult("ok", "可用命令："+commands+"。\n任务状态和停止仅作用于你在当前入口的运行任务。")
    if request.name in {"memory", "clear"}:
        callback = getattr(services, request.name)
        if callback is None:
            return CommandResult("unavailable", "该业务命令尚未配置。")
        result = await _await(callback(inbound, request))
        return result if isinstance(result, CommandResult) else CommandResult("error", "命令服务返回无效结果。")
    if request.name in {"bind", "usage"}:
        callback = services.binding_handler if request.name == "bind" else services.usage
        if callback is None:
            return CommandResult("unavailable", "绑定功能尚未接入。" if request.name == "bind" else "用量查询暂时不可用。")
        if request.name == "usage" and request.argument:
            return CommandResult("syntax", "用法：/usage")
        # Error text never contains callback exceptions or binding arguments.
        try:
            result = await _await(callback(inbound, request))
            return result if isinstance(result, CommandResult) else CommandResult("error", "命令服务返回无效结果。")
        except Exception:
            return CommandResult("unavailable", "命令服务暂时不可用。")
    if request.name in {"task", "cancel"}:
        return _task_command(request, inbound, services)
    return await _setting_command(request, inbound, services)


def _task_command(request: ParsedCommand, inbound: InboundMessage, services: CommandServices) -> CommandResult:
    registry = services.running_tasks
    if registry is None:
        return CommandResult("unavailable", "运行任务服务暂时不可用。")
    argument = request.argument.strip()
    words = argument.split()
    cancelling = request.name == "cancel"
    if request.name == "task" and words and words[0].casefold() in {"cancel", "stop", "停止", "取消"}:
        cancelling = True
        words = words[1:]
        argument = " ".join(words)
    if cancelling:
        if len(words) > 1:
            return CommandResult("syntax", "用法：/cancel [运行任务ID]")
        # An explicit ID always reaches the exact-ID path, including invalid IDs.
        stopped = registry.cancel_for_endpoint(inbound.endpoint, inbound.actor, argument or None)
        if stopped is None:
            return CommandResult("not_found", "当前入口没有匹配的本人运行任务。")
        return CommandResult("ok", f"已请求停止任务 {stopped.task_id}。")
    if words and words[0].casefold() in _STATUS:
        words = words[1:]
    if len(words) > 1:
        return CommandResult("unsupported", "此入口暂不支持发起后台任务。查看：/task；停止：/cancel [运行任务ID]。")
    selected_id = words[0] if words else None
    visible = registry.list_for_endpoint(inbound.endpoint, inbound.actor)
    if selected_id is not None:
        visible = [task for task in visible if task.task_id == selected_id]
        if not visible:
            return CommandResult("not_found", "当前入口没有匹配的本人运行任务。")
    if not visible:
        return CommandResult("ok", "当前入口没有本人的运行任务。")
    return CommandResult("ok", "当前入口的本人运行任务：\n" + "\n".join(
        f"- {task.task_id} · {task.elapsed_seconds} 秒 · {task.summary}" for task in visible
    ))


async def _setting_command(request: ParsedCommand, inbound: InboundMessage, services: CommandServices) -> CommandResult:
    if services.host_policy is None:
        return CommandResult("denied", "宿主尚未授权模型配置命令。")
    try:
        policy = await _await(services.host_policy(inbound))
    except Exception:
        return CommandResult("unavailable", "模型配置策略暂时不可用。")
    if not isinstance(policy, CommandPolicy):
        return CommandResult("denied", "模型配置策略无效。")
    model = request.name == "model"
    store = services.model_preferences if model else services.reasoning_preferences
    if store is None:
        return CommandResult("unavailable", "配置存储暂时不可用。")
    value = request.argument.strip()
    words = value.split(None, 1)
    group_default = bool(words and words[0].casefold() in {"group", "群默认"})
    if group_default:
        if inbound.endpoint.kind != "group" or not policy.allow_group_default:
            return CommandResult("denied", "宿主未授权修改当前入口的群默认配置。")
        value = words[1].strip() if len(words) == 2 else ""
    read_allowed = policy.allow_model_read if model else policy.allow_effort_read
    change_allowed = policy.allow_model_change if model else policy.allow_effort_change
    label = "模型" if model else "推理强度"
    default = policy.default_model if model else policy.default_effort
    options = policy.models if model else policy.efforts
    key = conversation_key(inbound)
    # Defaults are attached to the real endpoint, never a canonical alias.
    target_scope = inbound.endpoint.scope
    if not value:
        if not read_allowed:
            return CommandResult("denied", "宿主未授权查看此模型配置。")
        if group_default:
            current = await _store(services, store.get_scope_default if model else store.get_scope_member_default, target_scope)
        else:
            current = await _store(services, store.get_explicit, key)
            if current is None and inbound.endpoint.kind == "group":
                current = await _store(services, store.get_scope_default if model else store.get_scope_member_default, target_scope)
        # Policy may hide a saved profile whose access has since been revoked.
        displayed = current if current in options else default if default in options else "宿主默认"
        return CommandResult("ok", f"当前{label}：{displayed}\n可用：" + ("、".join(options) or "无"))
    if not change_allowed:
        return CommandResult("denied", "宿主未授权更改此模型配置。")
    reset = value.casefold() in _RESETS
    if not model and not reset:
        value = _EFFORT_ALIASES.get(value.casefold(), value.casefold())
    if not reset and value not in options:
        return CommandResult("denied", f"该{label}不在宿主授权范围内。")
    if group_default:
        if reset:
            clear = store.clear_scope_default if model else store.clear_scope_member_default
            await _store(services, clear, target_scope)
        else:
            setter = store.set_scope_default if model else store.set_scope_member_default
            await _store(services, setter, target_scope, value)
    elif reset:
        await _store(services, store.clear, key)
    else:
        await _store(services, store.set, key, value)
    target = "当前入口的群默认" if group_default else "本人当前会话的"
    return CommandResult("ok", f"已恢复{target}{label}。" if reset else f"已设置{target}{label}：{value}。")
