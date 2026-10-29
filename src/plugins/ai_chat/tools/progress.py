"""Official say: advertise and execute only with a trusted bound host port."""
import json

from ..ai_tools import SAY_TOOL
from ..adapters.qq_official.progress import ProgressEmitter
from .registry import FunctionTool
from .state import structured_tool_source


def enabled(context):
    source = structured_tool_source(context)
    return (source is not None and source.endpoint.account.platform == "qq-official"
            and isinstance(getattr(context.platform, "progress", None), ProgressEmitter)
            and context.platform.progress_enabled is True)


async def execute(turn, arguments):
    if not enabled(turn.context):
        return json.dumps({"ok": False, "status": "suppressed", "reason": "progress_unavailable"})
    return json.dumps(await turn.context.platform.progress.say(arguments.get("text")), ensure_ascii=False)


HANDLERS = (FunctionTool(SAY_TOOL, enabled, execute, platform_neutral=True),)
