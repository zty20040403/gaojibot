"""One capability-aware catalog for official QQ turns."""

from functools import cache

from ..ai_tools import ToolDefinition
from .registry import ToolRegistry
from .state import ToolContext, structured_tool_source


@cache
def _base_registry() -> ToolRegistry:
    from .history import HANDLERS as HISTORY_HANDLERS
    from .memory import HANDLERS as MEMORY_HANDLERS
    from .sources import HANDLERS as SOURCE_HANDLERS
    from .stickers import HANDLERS as STICKER_HANDLERS
    from .owned_sandbox import HANDLERS as OWNED_SANDBOX_HANDLERS
    from .progress import HANDLERS as PROGRESS_HANDLERS
    from .background import HANDLERS as BACKGROUND_HANDLERS
    from .schedules import HANDLERS as SCHEDULE_HANDLERS

    return ToolRegistry(handler for handler in (
        *HISTORY_HANDLERS, *MEMORY_HANDLERS, *SOURCE_HANDLERS, *STICKER_HANDLERS,
        *OWNED_SANDBOX_HANDLERS, *PROGRESS_HANDLERS, *BACKGROUND_HANDLERS, *SCHEDULE_HANDLERS
    ) if handler.platform_neutral)


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
