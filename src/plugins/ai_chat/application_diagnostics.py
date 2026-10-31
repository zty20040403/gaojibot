"""Bind shared official health to the actual application lifespan."""

async def start_application_diagnostics(app,context):
    if getattr(context,'official_runtime',None) is None:return
    from .adapters.qq_official.observability import register_official_http
    if context.official_observability is not None:raise RuntimeError('application_diagnostics_already_started')
    context.official_observability=register_official_http(app,context)
    await context.official_observability.start()
