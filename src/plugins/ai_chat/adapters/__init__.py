"""Platform adapters; importing shared contracts does not load any platform SDK."""

__all__ = ["OneBotIngestAdapter"]


def __getattr__(name: str):
    # Keep the established export for OneBotRuntime, but resolve the concrete
    # implementation only when a caller explicitly asks for the OneBot adapter.
    if name == "OneBotIngestAdapter":
        from .onebot_v11 import OneBotIngestAdapter

        return OneBotIngestAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)
