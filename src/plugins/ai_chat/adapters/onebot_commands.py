"""Keep native OneBot command arguments and finish behavior at the SDK edge."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..application.commands import ParsedCommand, dispatch_command


async def dispatch_onebot_command(name: str, callback: Callable[[], Awaitable[Any]]) -> None:
    # NoneBot already recognized this command. Do not reparse or normalize the
    # original Message/Event: legacy handlers own their exact existing behavior.
    await dispatch_command(ParsedCommand(name), legacy_callback=callback)
