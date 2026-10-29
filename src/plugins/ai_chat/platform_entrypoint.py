"""Choose the real product transport before importing a platform SDK."""
from __future__ import annotations
import os
from .platform_mode import PlatformMode


def serve(*,env_file=None,health_writer=False):
    mode=PlatformMode.from_env(os.environ)
    if mode.onebot:
        from .application_diagnostics import configure_desktop_health
        configure_desktop_health(bool(health_writer and mode.official))
        import nonebot
        from .adapters.onebot_route import RouteGuardedOneBotAdapter
        kwargs={} if env_file is None else {'_env_file':env_file}
        nonebot.init(**kwargs)
        nonebot.get_driver().register_adapter(RouteGuardedOneBotAdapter)
        if nonebot.load_plugin('src.plugins.ai_chat.onebot_plugin') is None:
            raise SystemExit('Required ai_chat plugin failed to load; refusing to start an empty bot.')
        if health_writer and not mode.official:
            from src.local_runtime.runtime_health import install_health_writer
            install_health_writer()
        nonebot.run(timeout_graceful_shutdown=10)
        return
    import uvicorn
    from .application_runtime import create_application
    port=int(os.environ.get('PORT','8080'))
    if not 1<=port<=65535:raise ValueError('invalid_application_port')
    uvicorn.run(create_application(desktop_health=health_writer),host=os.environ.get('HOST','127.0.0.1'),port=port,
        timeout_graceful_shutdown=10,access_log=False)
