"""Tool capability boundary.

The existing tool catalog and executors remain source-compatible while callers
migrate behind this package boundary.
"""

from ..ai_tools import ToolDefinition, available_tools
from .alerts import HANDLERS as ALERT_HANDLERS
from .context import HANDLERS as CONTEXT_HANDLERS
from .memory import HANDLERS as MEMORY_HANDLERS
from .registry import ToolRegistry

domain_registry = ToolRegistry((*ALERT_HANDLERS, *MEMORY_HANDLERS, *CONTEXT_HANDLERS))

__all__ = ["ToolDefinition", "available_tools", "domain_registry"]
