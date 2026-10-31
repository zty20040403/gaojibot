"""Per-turn dependencies and mutable output state shared by tool handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..platform_types import BackgroundToolSource, InboundMessage, TurnPlatform

if TYPE_CHECKING:
    from ..runtime import AppContext
    from ..web_search import SearchResult
    from ..conversation_scope import ConversationScope


def structured_tool_source(context) -> InboundMessage | BackgroundToolSource | None:
    """Read typed identity metadata; every effect still needs its source fence."""
    inbound = getattr(context, "inbound", None)
    background = getattr(context, "background_source", None)
    if inbound is not None and background is not None:
        raise ValueError("tool sources are mutually exclusive")
    if inbound is not None and not isinstance(inbound, InboundMessage):
        raise TypeError("inbound must be an InboundMessage")
    if background is not None and not isinstance(background, BackgroundToolSource):
        raise TypeError("background_source must be a BackgroundToolSource")
    return inbound if inbound is not None else background


@dataclass(frozen=True)
class ToolContext:
    services: Any
    event: object | None = None
    capabilities: frozenset[str] = frozenset()
    agent_executor: Any = None
    journal_turn_id: int | None = None
    inbound: InboundMessage | None = None
    platform: TurnPlatform | None = None
    background_source: BackgroundToolSource | None = None

    def __post_init__(self) -> None:
        if self.inbound is not None and not isinstance(self.inbound, InboundMessage):
            raise TypeError("inbound must be an InboundMessage")
        source = structured_tool_source(self)
        if self.background_source is not None and self.event is not None:
            raise ValueError("background work cannot carry a legacy event")
        if source is not None and self.platform is not None:
            if source.endpoint.account != self.platform.account:
                raise ValueError("tool platform account does not match inbound endpoint")

    @property
    def app(self) -> AppContext:
        return self.services.context

    @property
    def scope(self) -> ConversationScope | None:
        """History scope only; endpoint and actor still govern authorization."""
        source = structured_tool_source(self)
        return source.scope if source is not None else None


@dataclass(frozen=True)
class SubAgentActions:
    delegate: Callable[[str, str], Awaitable[dict[str, Any]]]
    run: Callable[[str], Awaitable[str]]
    resume: Callable[[int], Awaitable[str]]


@dataclass
class TurnState:
    context: ToolContext
    user_text: str = ""
    image_sources: list[str] = field(default_factory=list)
    voice_message_id: int | None = None
    search_results: list[SearchResult] = field(default_factory=list)
    sticker_handles_this_turn: set[str] = field(default_factory=set)
    sticker_sent: bool = False
    subagent_task_started: bool = False
    subagent_delegations: int = 0
    subagent_actions: SubAgentActions | None = None
    model_profile: Any = None

    @property
    def agent_executor(self) -> Any:
        return self.context.agent_executor
