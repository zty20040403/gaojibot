from __future__ import annotations
from .background_jobs import BackgroundJobs

from typing import Any, Protocol

from .admin import AdminServices, register_admin
from .config import Settings
from .observability import register_metrics_endpoint, telemetry
from .runtime import AppContext


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

    # The official classified media library has its own authenticated API.
    sticker_inventory = None
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
                background_jobs=BackgroundJobs(context.job_store) if context.job_store else None,
                background_tasks=context.background_tasks,
                model_catalog=context.model_catalog,
                llm_gateway=context.llm_gateway,
                local_model=context.local_model,
                model_preferences=context.model_preferences,
                reasoning_preferences=context.reasoning_preferences,
                user_profiles=context.user_profiles,
                message_ledger=context.message_ledger,
                conversations=context.conversations,
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
                mobile_authorization=context.mobile_authorization,
                official_observability=lambda: getattr(context,'official_observability',None),
                official_identity_service=lambda: getattr(getattr(context,'official_runtime',None),'admin_identity_service',None),
                official_file_service=lambda: getattr(getattr(context, "official_runtime", None), "file_service", None),
            ),
            path=settings.admin_path,
        )
        if context.mobile_authorization is None:
            logger.warning(
                "Admin dashboard is locked until account authentication is initialized."
            )
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.error(f"Admin dashboard could not be registered: {exc}")
