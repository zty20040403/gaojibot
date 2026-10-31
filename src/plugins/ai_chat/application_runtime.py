"""Official QQ application lifecycle: common services started inside the ASGI lifespan."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from .application_construction import build_owned_context
from .application_maintenance import ApplicationMaintenance
from .application_workers import start_background_tasks
from .agent_loop import configure_llm_runtime, release_llm_runtime
from .bootstrap import register_http_surfaces
from .handler_constants import BOT_VERSION
from .runtime import build_app_context


class OfficialApplicationRuntime:
    def __init__(self,settings,*,state_dir,cache_dir,project_root,env,logger=None):
        self.settings=settings
        self.state_dir,self.cache_dir,self.project_root=map(Path,(state_dir,cache_dir,project_root))
        self.env=dict(env)
        self.logger=logger or logging.getLogger('gaoji.application')
        self._context=None
        self.ready=False
        self._closed=False
        self._lock=asyncio.Lock()

    @property
    def context(self):
        if self._context is None:raise RuntimeError('application_not_started')
        return self._context

    async def _host(self,context):
        from .adapters.qq_official.host_config import configured_migration_factory
        factory=configured_migration_factory(self.env)
        if factory is None:
            return
        service=factory(context,self)
        context.migration_service=service
        await service.start_native()

    async def start(self,app):
        async with self._lock:
            if self.ready:return
            if self._closed:raise RuntimeError('application_closed')
            maintenance=ApplicationMaintenance()
            context=await build_owned_context(build_app_context,self.settings,state_dir=self.state_dir,cache_dir=self.cache_dir,
                project_root=self.project_root,logger=self.logger,environment=self.env)
            self._context=context
            maintenance.bind(context)
            try:
                await self._host(context)
                configure_llm_runtime(context.model_catalog,context.llm_gateway)
                register_http_surfaces(app,context,settings=self.settings,version=BOT_VERSION,logger=self.logger)
                await context.start_official_ingress(self.env)
                await start_background_tasks(context,maintenance=maintenance)
                from .application_diagnostics import start_application_diagnostics
                await start_application_diagnostics(app,context)
                app.state.gaoji_context=context
                self.ready=True
            except BaseException:
                await self._close()
                raise

    async def _close(self):
        self.ready=False
        self._closed=True
        if self._context is not None:
            try:await self._context.shutdown()
            finally:release_llm_runtime(self._context.llm_gateway)

    async def close(self):
        async with self._lock:await self._close()


def create_application(*,env=None,runtime=None):
    """Build ASGI application without credential/DB I/O until lifespan starts."""
    import os
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from .config import Settings
    from .paths import application_paths
    environment=dict(os.environ if env is None else env)
    state_dir,cache_dir,project_root=application_paths(environment)
    runtime=runtime or OfficialApplicationRuntime(Settings.from_env(environment),state_dir=state_dir,
        cache_dir=cache_dir,project_root=project_root,env=environment)
    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start(app)
            yield
        finally:await runtime.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.gaoji_runtime=runtime
    return app
