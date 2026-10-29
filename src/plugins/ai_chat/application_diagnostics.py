"""Bind shared official health to the actual application lifespan."""
from pathlib import Path

_desktop_requested=False


def configure_desktop_health(enabled):
    global _desktop_requested
    if type(enabled) is not bool:raise ValueError('invalid_desktop_health_mode')
    _desktop_requested=enabled


async def start_application_diagnostics(app,context,*,desktop_health=None,legacy_getter=None):
    if getattr(context,'official_runtime',None) is None:return
    from .adapters.qq_official.observability import register_official_http
    if context.official_observability is not None:raise RuntimeError('application_diagnostics_already_started')
    context.official_observability=register_official_http(app,context)
    await context.official_observability.start()
    desktop_health=_desktop_requested if desktop_health is None else desktop_health
    if desktop_health:
        from src.local_runtime.runtime_health import SDKFreeHealthWriter
        context.desktop_health_writer=SDKFreeHealthWriter(Path(context.state_dir)/'desktop-health.json',
            lambda:context.official_observability,legacy_getter=legacy_getter)
        await context.desktop_health_writer.start()
