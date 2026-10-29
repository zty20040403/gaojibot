"""Tool definitions, availability and execution share one handler registration."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, fields
from typing import Protocol

from ..ai_tools import ToolDefinition
from ..platform_types import PlatformFeatures
from ..tool_policy import ToolPolicy, policy_for_tool, tool_enabled
from .state import ToolContext, TurnState


class ToolHandler(Protocol):
    platform_neutral: bool
    required_features: frozenset[str]

    @property
    def definition(self) -> ToolDefinition: ...

    @property
    def policy(self) -> ToolPolicy: ...

    def enabled(self, context: ToolContext) -> bool: ...

    def definition_for(self, context: ToolContext) -> ToolDefinition: ...

    async def run(self, turn: TurnState, arguments: dict[str, object]) -> str: ...


_FEATURE_NAMES = frozenset(field.name for field in fields(PlatformFeatures))


def platform_enabled(handler: ToolHandler, context: ToolContext) -> bool:
    """Check the actual transport before any legacy availability callback runs.

    Old callers with only a legacy event keep their existing behavior. Structured
    callers require both account-bound inputs; history aliases grant no access.
    Unknown/custom handlers must opt in explicitly to non-OneBot execution.
    """
    from .state import structured_tool_source
    inbound, platform = structured_tool_source(context), context.platform
    neutral = getattr(handler, "platform_neutral", False) is True
    required = getattr(handler, "required_features", frozenset())
    if inbound is None and platform is None:
        return context.event is not None or (neutral and required == frozenset())
    if inbound is None or platform is None or platform.account != inbound.endpoint.account:
        return False
    if inbound.endpoint.account.platform != "onebot-v11":
        if not neutral:
            return False
    elif not neutral and context.event is None:
        return False
    if not isinstance(required, frozenset) or not required <= _FEATURE_NAMES:
        return False
    return all(getattr(platform.features, feature, False) is True for feature in required)


@dataclass(frozen=True)
class FunctionTool:
    definition: ToolDefinition
    available: Callable[[ToolContext], bool]
    implementation: Callable[[TurnState, dict[str, object]], Awaitable[str]]
    project: Callable[[ToolDefinition, ToolContext], None] | None = None
    platform_neutral: bool = False
    required_features: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.platform_neutral) is not bool:
            raise TypeError("platform_neutral must be a bool")
        if not isinstance(self.required_features, frozenset) or not self.required_features <= _FEATURE_NAMES:
            raise ValueError("required_features must contain known platform features")

    @property
    def policy(self) -> ToolPolicy:
        return policy_for_tool(self.definition["function"]["name"])

    def enabled(self, context: ToolContext) -> bool:
        return platform_enabled(self, context) and self.available(context)

    def definition_for(self, context: ToolContext) -> ToolDefinition:
        definition = deepcopy(self.definition)
        if self.project is not None:
            self.project(definition, context)
        if definition["function"]["name"] != self.definition["function"]["name"]:
            raise ValueError("Tool projection cannot change its registered name")
        return definition

    async def run(self, turn: TurnState, arguments: dict[str, object]) -> str:
        return await self.implementation(turn, arguments)


class ToolRegistry:
    def __init__(
        self,
        handlers: Iterable[ToolHandler],
        *,
        catalog: Iterable[ToolDefinition] | None = None,
    ) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        for handler in handlers:
            name = handler.definition["function"]["name"]
            if not name or name in self._handlers:
                raise ValueError(f"Duplicate or empty tool registration: {name!r}")
            if not callable(getattr(handler, "run", None)):
                raise TypeError(f"Tool has no handler: {name}")
            self._handlers[name] = handler
        if catalog is not None:
            order = [item["function"]["name"] for item in catalog]
            if len(order) != len(set(order)) or set(order) != self._handlers.keys():
                raise ValueError("Tool catalog must match registered handlers exactly")
            self._handlers = {name: self._handlers[name] for name in order}

    @property
    def handlers(self) -> tuple[ToolHandler, ...]:
        return tuple(self._handlers.values())

    def definitions(self, context: ToolContext) -> list[ToolDefinition]:
        return [
            handler.definition_for(context)
            for name, handler in self._handlers.items()
            if self._enabled(handler, context)
        ]

    @staticmethod
    def _enabled(handler: ToolHandler, context: ToolContext) -> bool:
        return (tool_enabled(handler.definition["function"]["name"])
                and platform_enabled(handler, context) and handler.enabled(context))

    async def execute(
        self, turn: TurnState, name: str, arguments: dict[str, object]
    ) -> str | None:
        handler = self._handlers.get(name)
        if handler is None:
            return None
        if not await turn.context.app.db_call(
            self._enabled, handler, turn.context
        ):
            return json.dumps(
                {
                    "ok": False,
                    "error": "当前会话不能使用此工具。",
                    "code": "tool_unavailable",
                },
                ensure_ascii=False,
            )
        return await handler.run(turn, arguments)
