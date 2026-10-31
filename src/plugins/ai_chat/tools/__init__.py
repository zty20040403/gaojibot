"""One capability-aware catalog for official QQ turns."""

from functools import cache
import json

from ..ai_tools import TOOL_DEFINITIONS, ToolDefinition
from .registry import FunctionTool, ToolRegistry
from .state import ToolContext, structured_tool_source


def _unavailable(context: ToolContext) -> bool:
    return False


async def _reject(turn, arguments) -> str:
    return json.dumps({"ok": False, "code": "tool_unavailable", "error": "当前会话不能使用此工具。"}, ensure_ascii=False)


@cache
def _base_registry() -> ToolRegistry:
    from .history import HANDLERS as HISTORY_HANDLERS
    from .memory import HANDLERS as MEMORY_HANDLERS
    from .sources import HANDLERS as SOURCE_HANDLERS
    from .stickers import HANDLERS as STICKER_HANDLERS
    from .owned_sandbox import HANDLERS as OWNED_SANDBOX_HANDLERS
    from .progress import HANDLERS as PROGRESS_HANDLERS
    from .background import HANDLERS as BACKGROUND_HANDLERS

    handlers = {handler.definition["function"]["name"]: handler
                for handler in (*HISTORY_HANDLERS,
                                *MEMORY_HANDLERS, *SOURCE_HANDLERS, *STICKER_HANDLERS,
                                *OWNED_SANDBOX_HANDLERS, *PROGRESS_HANDLERS, *BACKGROUND_HANDLERS)
                if handler.platform_neutral}
    names = {definition["function"]["name"] for definition in TOOL_DEFINITIONS}
    catalog = [*TOOL_DEFINITIONS, *(handler.definition for name, handler in handlers.items() if name not in names)]
    return ToolRegistry((handlers.get(definition["function"]["name"])
                         or FunctionTool(definition, _unavailable, _reject)
                         for definition in catalog), catalog=catalog)


def neutral_registry_for(context: ToolContext) -> ToolRegistry:
    """Catalog for an actual structured inbound, with fail-closed gates."""
    if structured_tool_source(context) is None or context.platform is None:
        raise ValueError("neutral tool registry requires inbound and platform")
    from ..adapters.qq_official.media_tools import media_registry
    return media_registry(_base_registry(), context)


registry_for = neutral_registry_for


def available_tools(context: ToolContext) -> list[ToolDefinition]:
    return registry_for(context).definitions(context)


__all__ = ["ToolDefinition", "available_tools", "registry_for", "neutral_registry_for"]
