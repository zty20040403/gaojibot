"""Slash-command adapter for the same owner/version-checked routine tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nonebot.adapters.onebot.v11 import Message, MessageEvent

from .matchers import routine_command
from .tools import domain_registry
from .tools.state import ToolContext, TurnState

if TYPE_CHECKING:
    from .handler_services import HandlerServices


from .application.routine_commands import HELP as HELP, _target as _target


async def command_result(services, event, text):
    from .application.routine_commands import command_result as shared_result
    turn = TurnState(ToolContext(services, event, frozenset({'routines'})))
    async def call(tool_name, arguments):
        return json.loads(await domain_registry.execute(turn, tool_name, arguments))
    return await shared_result(text, call)


async def handle_routine_command(
    services: HandlerServices, event: MessageEvent, args: Message
) -> None:
    result = await command_result(services, event, args.extract_plain_text())
    await routine_command.finish(result)
