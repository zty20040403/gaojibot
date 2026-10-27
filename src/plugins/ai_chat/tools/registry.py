"""Tool definitions, availability and execution share one handler registration."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from ..ai_tools import ToolDefinition
from ..tool_policy import ToolPolicy, policy_for_tool, tool_enabled
from .state import ToolContext, TurnState


class ToolHandler(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    @property
    def policy(self) -> ToolPolicy: ...

    def enabled(self, context: ToolContext) -> bool: ...

    def definition_for(self, context: ToolContext) -> ToolDefinition: ...

    async def run(self, turn: TurnState, arguments: dict[str, object]) -> str: ...


@dataclass(frozen=True)
class FunctionTool:
    definition: ToolDefinition
    available: Callable[[ToolContext], bool]
    implementation: Callable[[TurnState, dict[str, object]], Awaitable[str]]
    project: Callable[[ToolDefinition, ToolContext], None] | None = None

    @property
    def policy(self) -> ToolPolicy:
        return policy_for_tool(self.definition["function"]["name"])

    def enabled(self, context: ToolContext) -> bool:
        return self.available(context)

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
            if tool_enabled(name) and handler.enabled(context)
        ]

    async def execute(
        self, turn: TurnState, name: str, arguments: dict[str, object]
    ) -> str | None:
        handler = self._handlers.get(name)
        if handler is None:
            return None
        if not tool_enabled(name) or not await turn.context.app.db_call(
            handler.enabled, turn.context
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
