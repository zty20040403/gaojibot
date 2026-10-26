"""Per-turn dependencies and mutable output state shared by tool handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment

if TYPE_CHECKING:
    from ..agent_tools import AgentToolExecutor
    from ..handler_services import HandlerServices
    from ..runtime import AppContext
    from ..web_search import SearchResult


@dataclass(frozen=True)
class ToolContext:
    services: HandlerServices
    event: MessageEvent
    capabilities: frozenset[str]

    @property
    def app(self) -> AppContext:
        return self.services.context


@dataclass(frozen=True)
class SubAgentActions:
    delegate: Callable[[str, str], Awaitable[dict[str, Any]]]
    run: Callable[[str], Awaitable[str]]
    resume: Callable[[int], Awaitable[str]]


@dataclass
class TurnState:
    context: ToolContext
    bot: Bot | None = None
    agent_executor: AgentToolExecutor | None = None
    user_text: str = ""
    image_sources: list[str] = field(default_factory=list)
    voice_message_id: int | None = None
    search_results: list[SearchResult] = field(default_factory=list)
    used_ocr_texts: list[str] = field(default_factory=list)
    used_voice_texts: list[str] = field(default_factory=list)
    voice_reply_segment: MessageSegment | None = None
    voice_reply_text: str = ""
    visual_reply_segment: MessageSegment | None = None
    sticker_handles_this_turn: set[str] = field(default_factory=set)
    subagent_task_started: bool = False
    subagent_delegations: int = 0
    subagent_actions: SubAgentActions | None = None
