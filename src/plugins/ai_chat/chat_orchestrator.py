"""One-release import compatibility; new OneBot code must use onebot_chat."""

from . import onebot_chat as _implementation
from .onebot_chat import OneBotChatService as OneBotChatService

__all__ = [name for name in dir(_implementation) if not name.startswith("_")]


def __getattr__(name: str):
    return getattr(_implementation, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_implementation)))
