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
    agent_executor: AgentToolExecutor | None = None
    routine_run_id: str = ""

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

    @property
    def agent_executor(self) -> AgentToolExecutor | None:
        return self.context.agent_executor

    def reply_checkpoint(self) -> dict[str, Any]:
        def encode(segment):
            return {"type": segment.type, "data": segment.data} if segment else None

        return {
            "visual": encode(self.visual_reply_segment),
            "voice": encode(self.voice_reply_segment),
            "voice_text": self.voice_reply_text,
            "task_started": self.subagent_task_started,
            "delegations": self.subagent_delegations,
        }

    def restore_reply_checkpoint(self, saved: dict[str, Any]) -> None:
        # A cached tool result can prepare a reply without having sent it yet.
        # Restore those segments as well as the model-visible tool receipt.
        if saved.get("visual"):
            self.visual_reply_segment = MessageSegment(**saved["visual"])
        if saved.get("voice"):
            self.voice_reply_segment = MessageSegment(**saved["voice"])
        self.voice_reply_text = str(saved.get("voice_text") or self.voice_reply_text)
        self.subagent_task_started = self.subagent_task_started or bool(saved.get("task_started"))
        self.subagent_delegations = max(self.subagent_delegations, int(saved.get("delegations", 0)))
