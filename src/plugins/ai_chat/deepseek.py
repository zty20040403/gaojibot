"""One-release import compatibility; new code must use agent_loop."""

from . import agent_loop as _implementation

DeepSeekTrace = _implementation.AgentTrace

__all__ = [name for name in dir(_implementation) if not name.startswith("_")]
__all__.append("DeepSeekTrace")


def __getattr__(name: str):
    return getattr(_implementation, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_implementation)))
