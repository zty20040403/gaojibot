from __future__ import annotations

from typing import Any, Protocol

from .admin import AdminServices, register_admin
from .bridges import register_bridge_routes
from .config import Settings
from .observability import register_metrics_endpoint, telemetry
from .runtime import AppContext
from .stickers import sticker_inventory


class BootstrapLogger(Protocol):
    def error(self, message: object, *args: object, **kwargs: object) -> object: ...

    def warning(self, message: object, *args: object, **kwargs: object) -> object: ...


def register_http_surfaces(
    app: Any,
    context: AppContext,
    *,
    settings: Settings,
    version: str,
    logger: BootstrapLogger,
) -> None:
    mcp_runtime = getattr(context, "mcp_runtime", None)
    if mcp_runtime is not None and mcp_runtime.endpoint is not None:
        app.mount(mcp_runtime.config.read_path, mcp_runtime.http, name="mcp-group-reader")
    if settings.observability_enabled:
        try:
            register_metrics_endpoint(
                app,
                path=settings.metrics_path,
                service_name=settings.otel_service_name,
                service_version=version,
                otlp_endpoint=settings.otel_exporter_otlp_endpoint,
                running_tasks=context.running_tasks,
                delivery_store=context.delivery_store,
                db_call=context.db_call,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.error(f"Observability endpoint could not be registered: {exc}")

    if context.bridge_manager is not None:
        try:
            register_bridge_routes(
                app,
                context.bridge_manager,
                matrix_appservice_token=settings.matrix_appservice_token,
                bluebubbles_webhook_token=settings.imessage_webhook_token,
                bluebubbles_chat_guid=settings.imessage_chat_guid,
                bluebubbles_bot_handle=settings.imessage_bot_handle,
                path=settings.bridge_path,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.error(
                f"Cross-platform bridge routes could not be registered: {exc}"
            )

    if not settings.admin_enabled:
        return
    try:
        register_admin(
            app,
            AdminServices(
                version=version,
                started_at=context.started_at,
                delivery_store=context.delivery_store,
                usage_store=context.usage_store,
                running_tasks=context.running_tasks,
                job_store=context.job_store,
                job_worker=context.job_worker,
                subagent_store=context.subagent_store,
                subagent_coordinator=context.subagent_coordinator,
                bridge_router=context.bridge_router,
                bridge_state=context.mirror_state,
                browser_manager=context.browser_manager,
                background_tasks=context.background_tasks,
                model_catalog=context.model_catalog,
                llm_gateway=context.llm_gateway,
                local_model=context.local_model,
                model_preferences=context.model_preferences,
                reasoning_preferences=context.reasoning_preferences,
                user_profiles=context.user_profiles,
                message_ledger=context.message_ledger,
                group_wiki_store=context.group_wiki_store,
                mcp_runtime=mcp_runtime,
                project_runtime=getattr(context, "project_runtime", None),
                settings=context.settings,
                sandbox_manager=context.sandbox_manager,
                sticker_inventory=sticker_inventory,
                media_library=context.media_library,
                source_store=context.source_store,
                media_cleanup=context.media_cleanup,
                vision_worker=context.vision_worker,
                turn_journal=context.turn_journal,
                database=context.database,
                telemetry=telemetry,
                alert_store=context.alert_store,
                alert_preferences=context.alert_preferences,
                fleet_client=context.fleet_client,
                mobile_authorization=context.mobile_authorization,
                official_file_service=lambda: getattr(getattr(context, "official_runtime", None), "file_service", None),
                official_migration_service=lambda: context.migration_service.admin_service()
                    if context.migration_service is not None else None,
            ),
            path=settings.admin_path,
        )
        if context.mobile_authorization is None:
            logger.warning(
                "Admin dashboard is locked until account authentication is initialized."
            )
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.error(f"Admin dashboard could not be registered: {exc}")
