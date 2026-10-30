"""Own the OneBot application's startup, runtime dependencies and shutdown."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import IgnoredException

from .application_construction import build_owned_context
from .adapters import OneBotIngestAdapter
from .agent_loop import configure_llm_runtime, release_llm_runtime
from .alert_notifier import AlertNotificationService
from .bootstrap import register_http_surfaces
from .config import Settings
from .handler_constants import BOT_VERSION
from .handler_services import HandlerServices
from .mobile_authorization import handle_approval_event
from .observability import observed_ai_turn
from .onebot_workers import start_background_tasks
from .runtime import AppContext, RuntimeLogger, build_app_context
from .video_analysis import DeepVideoAnalysisError, DeepVideoAnalyzer


def assemble_handlers(context: AppContext) -> HandlerServices:
    settings = context.settings
    video_analyzer = None
    if (
        settings.video_deep_enabled
        and context.source_store is not None
        and context.vision_worker is not None
    ):
        try:
            video_analyzer = DeepVideoAnalyzer(
                context.source_store,
                context.vision_worker,
                whisper_model_path=settings.video_whisper_model_path,
                frame_count=settings.video_frame_count,
                max_download_bytes=settings.video_max_download_bytes,
                max_duration_seconds=settings.video_max_duration_seconds,
                timeout_seconds=settings.video_timeout_seconds,
                whisper_threads=settings.video_whisper_threads,
                cache_seconds=settings.video_cache_seconds,
            )
        except DeepVideoAnalysisError as exc:
            context.logger.warning(f"Deep video analysis is unavailable: {exc}")

    handlers = HandlerServices(context, video_analyzer=video_analyzer)
    handlers.chat._run_tracked_ai = observed_ai_turn(handlers.chat._run_tracked_ai)
    if context.vision_worker is not None:
        context.vision_worker.set_source_resolver(
            handlers.chat._refresh_vision_source_url
        )
    handlers.ingest_adapter = OneBotIngestAdapter(
        group_enabled=handlers.group_enabled,
        canonical_scope=handlers.chat._conversation_scope,
        image_cache_key=handlers.chat._image_cache_key,
        voice_cache_key=handlers.chat._voice_cache_key,
        video_cache_key=handlers.chat._video_cache_key,
        ocr_max_images=settings.ocr_max_images,
        logger=context.logger,
        message_ledger=context.message_ledger,
        delivery_store=context.delivery_store,
        bridge_router=context.bridge_router,
        mirror_state=context.mirror_state,
        bridge_manager=context.bridge_manager,
        media_library=context.media_library,
        source_store=context.source_store,
        user_profiles=context.user_profiles,
        recent_images=context.recent_images,
        recent_voices=context.recent_voices,
        recent_videos=context.recent_videos,
        db_call=context.db_call,
    )
    from .qq_action_authorization import register_qq_executors

    register_qq_executors(handlers)
    from .mcp_bridge.runtime import install_mcp

    install_mcp(handlers)
    from .project_runtime import install_projects

    install_projects(handlers)
    return handlers


class OneBotRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        state_dir: Path,
        cache_dir: Path,
        project_root: Path,
        logger: RuntimeLogger,
        migration_service_factory=None,
    ) -> None:
        self.settings = settings
        self.state_dir = state_dir
        self.cache_dir = cache_dir
        self.project_root = project_root
        self.logger = logger
        self._context: AppContext | None = None
        self._handlers: HandlerServices | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.ready = False
        self.migration_service_factory = migration_service_factory
        self.migration_routes = None

    @property
    def context(self) -> AppContext:
        if self._context is None:
            raise RuntimeError("OneBot runtime has not started")
        return self._context

    @property
    def handlers(self) -> HandlerServices:
        if self._handlers is None:
            raise RuntimeError("OneBot handlers have not started")
        return self._handlers

    async def _generate_historian(self, candidate):
        return await self.handlers.triggers._generate_historian(candidate)

    async def _generate_dream(self, scope_key, entries, evidence):
        return await self.handlers.triggers._generate_dream(
            scope_key, list(entries), evidence
        )

    def _dream_evidence(self, entry):
        return self.handlers.triggers._dream_evidence(entry)

    async def start(self, app: Any) -> None:
        async with self._lock:
            if self.ready:
                return
            if self._closed:
                raise RuntimeError("OneBot runtime is closed; restart the process")
            context = await build_owned_context(
                build_app_context,
                self.settings,
                state_dir=self.state_dir,
                cache_dir=self.cache_dir,
                project_root=self.project_root,
                logger=self.logger,
                historian_generator=self._generate_historian,
                dream_generator=self._generate_dream,
                evidence_provider=self._dream_evidence,
            )
            self._context = context
            try:
                await self._start_migration_service(context)
                self._handlers = assemble_handlers(context)
                notifier = AlertNotificationService(
                    alertmanager_url=self.settings.alertmanager_url,
                    group_id=self.settings.alert_notify_group_id,
                    check_seconds=self.settings.alert_notify_check_seconds,
                    state_path=context.state_dir / "alert-notifier.json",
                    logger=self.logger,
                    history_store=context.alert_store,
                    enabled_provider=lambda: (
                        context.alert_preferences.effective_enabled(
                            self.settings.alert_notify_enabled
                        )
                    ),
                )
                configure_llm_runtime(context.model_catalog, context.llm_gateway)
                register_http_surfaces(
                    app,
                    context,
                    settings=self.settings,
                    version=BOT_VERSION,
                    logger=self.logger,
                )
                await context.start_official_ingress()
                if getattr(context,'official_runtime',None) is not None:
                    from nonebot import get_bots
                    from .application_diagnostics import start_application_diagnostics
                    def legacy_health():
                        key=str(self.settings.admin_bot_id or '')
                        return {'qq':key,'connected':bool(key) and key in get_bots()}
                    await start_application_diagnostics(app,context,legacy_getter=legacy_health)
                await start_background_tasks(context, self.handlers, notifier)
                self.ready = True
            except BaseException:
                await self._close()
                raise

    async def preprocess(self, bot: Bot, event: MessageEvent) -> None:
        if not self.ready:
            raise IgnoredException("OneBot runtime is not ready")
        if self.migration_routes is not None:
            if event.user_id == event.self_id:
                from .adapters.onebot_echo import settle_self_echo
                await self.context.db_call(settle_self_echo,self.migration_routes,self.context.delivery_store,bot,event)
                raise IgnoredException('Managed self receipt settled outside conversation handlers')
            # Approval reducers may cause effects before a matcher runs. Their
            # actual task needs its own captured source; a later matcher obtains
            # another handler-scoped proof for the same authenticated event.
            async with self.migration_routes.handler_context("approval_preprocessor", bot, event):
                if await handle_approval_event(self.context.mobile_authorization, bot, event):
                    raise IgnoredException("Mobile authorization handled outside conversation history")
        elif await handle_approval_event(self.context.mobile_authorization, bot, event):
            raise IgnoredException("Mobile authorization handled outside conversation history")
        if isinstance(event, GroupMessageEvent) and not await self.context.db_call(
            self.handlers.group_enabled, event.group_id
        ):
            raise IgnoredException("QQ group is disabled for this bot")

    async def _start_migration_service(self, context):
        if self.migration_service_factory is None:
            from .adapters.qq_official.host_config import configured_migration_factory
            self.migration_service_factory = configured_migration_factory()
        if self.migration_service_factory is None:
            return
        service = self.migration_service_factory(context, self)
        if inspect.isawaitable(service):
            service = await service
        if service is None:
            raise RuntimeError("Migration service factory returned no service")
        self.migration_routes = service
        context.migration_service = service
        if getattr(service, 'native_only', False) or getattr(service, "baselines", None) == {}:
            await service.start_native()
            self.migration_routes = None
            return
        start = getattr(service, "start", None)
        if start is not None:
            result = start()
            if inspect.isawaitable(result):
                await result
        from .adapters.onebot_route import install_boundary
        from .adapters.onebot_route_context import install_source_runtime
        install_boundary(service)
        install_source_runtime(service)
        worker = getattr(context, "job_worker", None)
        if worker is not None:
            worker.source_context = service.job_context
        tasks = getattr(context, "subagent_store", None)
        if tasks is not None:
            tasks.source_recorder = service.capture_domain
        routines = getattr(context, "routine_store", None)
        if routines is not None:
            routines.source_recorder = service.capture_domain
            routines.source_job_recorder = service.derive_job
            routines.source_schedule_guard = service.guard_due_routines
        deliveries = getattr(context, "delivery_store", None)
        if deliveries is not None:
            deliveries.source_recorder = service.capture_delivery
        reminders = getattr(context, "reminder_store", None)
        if reminders is not None:
            reminders.source_recorder = service.capture_reminder

    async def connected(self, bot):
        if self.migration_routes is not None:
            connect = getattr(self.migration_routes, "connected", None)
            if connect is not None:
                await connect(bot)

    async def _close(self) -> None:
        self.ready = False
        self._closed = True
        context = self._context
        try:
            if context is not None:
                await context.shutdown()
        finally:
            if self.migration_routes is not None:
                from .adapters.onebot_route import uninstall_boundary
                from .adapters.onebot_route_context import uninstall_source_runtime
                uninstall_boundary(self.migration_routes)
                uninstall_source_runtime(self.migration_routes)
                self.migration_routes = None
            if context is not None:
                release_llm_runtime(context.llm_gateway)
            self._handlers = None
            self._context = None

    async def shutdown(self) -> None:
        async with self._lock:
            await self._close()
