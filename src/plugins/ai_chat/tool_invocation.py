"""Host-owned identity of the tool call currently being executed."""

from contextvars import ContextVar


active_tool_call_id: ContextVar[str | None] = ContextVar(
    "active_tool_call_id", default=None
)
# (host turn key, loop sequence); propagated to tool-created asyncio tasks.
active_tool_step: ContextVar[tuple[str, int] | None] = ContextVar(
    "active_tool_step", default=None
)
