"""Human-readable project commands over the shared, permission-checked registry."""

import json

from nonebot.adapters.onebot.v11 import MessageSegment

from .tools import domain_registry
from .tools.state import ToolContext, TurnState


from .application.project_commands import HELP as HELP, STATES as STATES, _summary as _summary, _bytes as _bytes


async def command_result(services, event, text):
    from .application.project_commands import command_result as shared_result
    turn = TurnState(ToolContext(services, event, frozenset({'projects'})))
    async def call(tool_name, arguments):
        return json.loads(await domain_registry.execute(turn, tool_name, arguments))
    return await shared_result(text, call)


async def handle_project_command(services, event, args):
    from .matchers import project_command

    await project_command.finish(
        MessageSegment.text(
            await command_result(services, event, args.extract_plain_text())
        )
    )
