"""Per-turn dependencies and mutable output state shared by tool handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from ..platform_types import BackgroundToolSource, InboundMessage, TurnPlatform

if TYPE_CHECKING:
    from ..agent_tools import AgentToolExecutor
    from ..handler_services import HandlerServices
    from ..runtime import AppContext
    from ..web_search import SearchResult
    from ..mcp_bridge.continuation_runtime import McpChatCheckpoint
    from ..conversation_scope import ConversationScope


class ReplyCheckpointCodec(Protocol):
    """An explicitly injected platform codec for pending native reply objects."""

    platform: str

    def encode(self, segment: object) -> dict[str, Any]: ...

    def decode(self, saved: dict[str, Any]) -> object: ...


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
    services: HandlerServices
    event: object | None = None
    capabilities: frozenset[str] = frozenset()
    agent_executor: AgentToolExecutor | None = None
    routine_run_id: str = ""
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
    bot: object | None = None
    user_text: str = ""
    image_sources: list[str] = field(default_factory=list)
    voice_message_id: int | None = None
    search_results: list[SearchResult] = field(default_factory=list)
    used_ocr_texts: list[str] = field(default_factory=list)
    used_voice_texts: list[str] = field(default_factory=list)
    voice_reply_segment: object | None = None
    voice_reply_text: str = ""
    visual_reply_segment: object | None = None
    sticker_handles_this_turn: set[str] = field(default_factory=set)
    subagent_task_started: bool = False
    subagent_delegations: int = 0
    subagent_actions: SubAgentActions | None = None
    mcp_checkpoint: McpChatCheckpoint | None = None
    reply_checkpoint_codec: ReplyCheckpointCodec | None = None

    @property
    def agent_executor(self) -> AgentToolExecutor | None:
        return self.context.agent_executor

    def reply_checkpoint(self) -> dict[str, Any]:
        def encode(segment):
            return self._checkpoint_codec().encode(segment) if segment is not None else None

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
        # Decode before applying state so a rejected codec cannot partly restore.
        visual = self._checkpoint_codec().decode(saved["visual"]) if saved.get("visual") else None
        voice = self._checkpoint_codec().decode(saved["voice"]) if saved.get("voice") else None
        if visual is not None:
            self.visual_reply_segment = visual
        if voice is not None:
            self.voice_reply_segment = voice
        self.voice_reply_text = str(saved.get("voice_text") or self.voice_reply_text)
        self.subagent_task_started = self.subagent_task_started or bool(saved.get("task_started"))
        self.subagent_delegations = max(self.subagent_delegations, int(saved.get("delegations", 0)))

    def _checkpoint_codec(self) -> ReplyCheckpointCodec:
        codec = self.reply_checkpoint_codec
        if codec is None:
            raise ValueError("native reply checkpoints require an explicit platform codec")
        source = structured_tool_source(self.context)
        account = source.endpoint.account if source is not None else (
            self.context.platform.account if self.context.platform is not None else None
        )
        if account is not None and codec.platform != account.platform:
            raise ValueError("reply checkpoint codec does not match the actual platform")
        return codec
