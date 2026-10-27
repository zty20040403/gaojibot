"""One capability-aware catalog for tool visibility and execution."""

from ..ai_tools import TOOL_DEFINITIONS, ToolDefinition
from .alerts import HANDLERS as ALERT_HANDLERS
from .context import HANDLERS as CONTEXT_HANDLERS
from .executor import HANDLERS as EXECUTOR_HANDLERS
from .fleet import HANDLERS as FLEET_HANDLERS
from .media import HANDLERS as MEDIA_HANDLERS
from .memory import HANDLERS as MEMORY_HANDLERS
from .registry import ToolRegistry
from .reminders import HANDLERS as REMINDER_HANDLERS
from .routines import HANDLERS as ROUTINE_HANDLERS
from .sources import HANDLERS as SOURCE_HANDLERS
from .stickers import HANDLERS as STICKER_HANDLERS
from .subagents import HANDLERS as SUBAGENT_HANDLERS
from .state import ToolContext

domain_registry = ToolRegistry(
    (
        *ALERT_HANDLERS,
        *MEMORY_HANDLERS,
        *CONTEXT_HANDLERS,
        *SOURCE_HANDLERS,
        *MEDIA_HANDLERS,
        *STICKER_HANDLERS,
        *SUBAGENT_HANDLERS,
        *REMINDER_HANDLERS,
        *ROUTINE_HANDLERS,
        *FLEET_HANDLERS,
        *EXECUTOR_HANDLERS,
    ),
    catalog=TOOL_DEFINITIONS,
)


def available_tools(context: ToolContext) -> list[ToolDefinition]:
    return domain_registry.definitions(context)


__all__ = ["ToolDefinition", "available_tools", "domain_registry"]
