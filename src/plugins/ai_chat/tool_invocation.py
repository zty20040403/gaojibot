"""Host-owned identity of the tool call currently being executed."""

from contextvars import ContextVar


active_tool_call_id: ContextVar[str | None] = ContextVar(
    "active_tool_call_id", default=None
)
