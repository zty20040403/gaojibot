"""Tool capability boundary.

The existing tool catalog and executors remain source-compatible while callers
migrate behind this package boundary.
"""

from ..ai_tools import ToolDefinition, available_tools
from .alerts import HANDLERS as ALERT_HANDLERS
from .context import HANDLERS as CONTEXT_HANDLERS
from .media import HANDLERS as MEDIA_HANDLERS
from .memory import HANDLERS as MEMORY_HANDLERS
from .registry import ToolRegistry
from .reminders import HANDLERS as REMINDER_HANDLERS
from .sources import HANDLERS as SOURCE_HANDLERS
from .stickers import HANDLERS as STICKER_HANDLERS
from .subagents import HANDLERS as SUBAGENT_HANDLERS

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
    )
)

__all__ = ["ToolDefinition", "available_tools", "domain_registry"]
