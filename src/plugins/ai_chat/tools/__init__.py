"""One capability-aware catalog; importing public state has no SDK side effects."""

from functools import cache
import json

from ..ai_tools import TOOL_DEFINITIONS, ToolDefinition
from .registry import FunctionTool, ToolRegistry
from .state import ToolContext, structured_tool_source


@cache
def _domain_registry() -> ToolRegistry:
    from .alerts import HANDLERS as ALERT_HANDLERS
    from .context import HANDLERS as CONTEXT_HANDLERS
    from .recent_speakers import HANDLERS as RECENT_SPEAKERS_HANDLERS
    from .executor import HANDLERS as EXECUTOR_HANDLERS
    from .fleet import HANDLERS as FLEET_HANDLERS
    from .media import HANDLERS as MEDIA_HANDLERS
    from .memory import HANDLERS as MEMORY_HANDLERS
    from .reminders import HANDLERS as REMINDER_HANDLERS
    from .routines import HANDLERS as ROUTINE_HANDLERS
    from .projects import HANDLERS as PROJECT_HANDLERS
    from .sources import HANDLERS as SOURCE_HANDLERS
    from .stickers import HANDLERS as STICKER_HANDLERS
    from .subagents import HANDLERS as SUBAGENT_HANDLERS

    return ToolRegistry(
        (
            *ALERT_HANDLERS, *MEMORY_HANDLERS, *CONTEXT_HANDLERS, *RECENT_SPEAKERS_HANDLERS,
            *SOURCE_HANDLERS, *MEDIA_HANDLERS, *STICKER_HANDLERS,
            *SUBAGENT_HANDLERS, *REMINDER_HANDLERS, *ROUTINE_HANDLERS,
            *PROJECT_HANDLERS, *FLEET_HANDLERS, *EXECUTOR_HANDLERS,
        ),
        catalog=TOOL_DEFINITIONS,
    )


def __getattr__(name: str):
    if name == "domain_registry":
        return _domain_registry()
    raise AttributeError(name)


def available_tools(context: ToolContext) -> list[ToolDefinition]:
    return registry_for(context).definitions(context)


def registry_for(context: ToolContext) -> ToolRegistry:
    """Freeze the advertised catalog for one turn, never a process-global MCP cache."""
    source = structured_tool_source(context)
    if source is not None and source.endpoint.account.platform != "onebot-v11":
        return neutral_registry_for(context)
    domain = _domain_registry()
    from .mcp import handlers as mcp_handlers

    extra = mcp_handlers(context)
    return ToolRegistry((*domain.handlers, *extra)) if extra else domain


def _unavailable(context: ToolContext) -> bool:
    return False


async def _reject(turn, arguments) -> str:
    return json.dumps({"ok": False, "code": "tool_unavailable", "error": "当前会话不能使用此工具。"}, ensure_ascii=False)


@cache
def _neutral_registry() -> ToolRegistry:
    # These modules import no native SDK. Unsupported catalog entries get a
    # rejecting host handler without importing their legacy implementations.
    from .alerts import HANDLERS as ALERT_HANDLERS
    from .context import HANDLERS as CONTEXT_HANDLERS
    from .recent_speakers import HANDLERS as RECENT_SPEAKERS_HANDLERS
    from .memory import HANDLERS as MEMORY_HANDLERS
    from .sources import HANDLERS as SOURCE_HANDLERS
    from .stickers import HANDLERS as STICKER_HANDLERS
    from .owned_sandbox import HANDLERS as OWNED_SANDBOX_HANDLERS
    from .projects import HANDLERS as PROJECT_HANDLERS

    from .native_reminders import HANDLERS as NATIVE_REMINDER_HANDLERS
    from .progress import HANDLERS as PROGRESS_HANDLERS

    migrated = {handler.definition["function"]["name"]: handler
                for handler in (*ALERT_HANDLERS, *CONTEXT_HANDLERS, *RECENT_SPEAKERS_HANDLERS, *NATIVE_REMINDER_HANDLERS, *MEMORY_HANDLERS,
                                *SOURCE_HANDLERS, *STICKER_HANDLERS, *OWNED_SANDBOX_HANDLERS, *PROJECT_HANDLERS, *PROGRESS_HANDLERS)
                if handler.platform_neutral}
    return ToolRegistry((migrated.get(definition["function"]["name"])
                         or FunctionTool(definition, _unavailable, _reject)
                         for definition in TOOL_DEFINITIONS), catalog=TOOL_DEFINITIONS)


def neutral_registry_for(context: ToolContext) -> ToolRegistry:
    """SDK-free catalog for an actual structured inbound, with fail-closed gates."""
    if structured_tool_source(context) is None or context.platform is None:
        raise ValueError("neutral tool registry requires inbound and platform")
    from ..adapters.qq_official.project_tools import project_registry
    from ..adapters.qq_official.media_tools import media_registry
    return media_registry(project_registry(_neutral_registry(), context), context)


__all__ = ["ToolDefinition", "available_tools", "domain_registry", "registry_for", "neutral_registry_for"]
