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

    async def run(self, turn: TurnState, arguments: dict[str, object]) -> str: ...


@dataclass(frozen=True)
class FunctionTool:
    definition: ToolDefinition
    available: Callable[[ToolContext], bool]
    implementation: Callable[[TurnState, dict[str, object]], Awaitable[str]]

    @property
    def policy(self) -> ToolPolicy:
        return policy_for_tool(self.definition["function"]["name"])

    def enabled(self, context: ToolContext) -> bool:
        return self.available(context)

    async def run(self, turn: TurnState, arguments: dict[str, object]) -> str:
        return await self.implementation(turn, arguments)


class ToolRegistry:
    def __init__(self, handlers: Iterable[ToolHandler]) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        for handler in handlers:
            name = handler.definition["function"]["name"]
            if not name or name in self._handlers:
                raise ValueError(f"Duplicate or empty tool registration: {name!r}")
            if not callable(getattr(handler, "run", None)):
                raise TypeError(f"Tool has no handler: {name}")
            self._handlers[name] = handler

    @property
    def handlers(self) -> tuple[ToolHandler, ...]:
        return tuple(self._handlers.values())

    def definitions(self, context: ToolContext) -> list[ToolDefinition]:
        return [
            deepcopy(handler.definition)
            for name, handler in self._handlers.items()
            if tool_enabled(name) and handler.enabled(context)
        ]

    def project_legacy_catalog(
        self, definitions: list[ToolDefinition], context: ToolContext
    ) -> list[ToolDefinition]:
        # Preserve existing prompt order during incremental domain migration.
        # Unmigrated definitions and execution stay on the old path for now.
        migrated = {
            item["function"]["name"]: item for item in self.definitions(context)
        }
        return [
            migrated.get(item["function"]["name"], item)
            for item in definitions
            if item["function"]["name"] not in self._handlers
            or item["function"]["name"] in migrated
        ]

    async def execute(
        self, turn: TurnState, name: str, arguments: dict[str, object]
    ) -> str | None:
        handler = self._handlers.get(name)
        if handler is None:
            return None
        if not tool_enabled(name) or not handler.enabled(turn.context):
            return json.dumps(
                {
                    "ok": False,
                    "error": "当前会话不能使用此工具。",
                    "code": "tool_unavailable",
                },
                ensure_ascii=False,
            )
        return await handler.run(turn, arguments)
