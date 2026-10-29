"""Host-only dynamic sandbox source check; absent means legacy behavior."""
from contextvars import ContextVar

active_sandbox_source = ContextVar('active_sandbox_source', default=None)


async def assert_sandbox_source():
    check = active_sandbox_source.get()
    if check is not None:
        await check()
