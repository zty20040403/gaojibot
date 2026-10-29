"""Compatibility entry for the shared worker lifecycle."""
from .application_workers import start_background_tasks as _start

async def start_background_tasks(context, handlers, alert_notifier):
    await _start(context, handlers, alert_notifier)
