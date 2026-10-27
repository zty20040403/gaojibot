"""Start owned workers only after runtime services have been assembled."""

from __future__ import annotations

import asyncio

from src.bot_storage import DatabaseError

from .alert_notifier import AlertNotificationService
from .handler_services import HandlerServices
from .observability import telemetry
from .runtime import AppContext


async def start_background_tasks(
    context: AppContext,
    handlers: HandlerServices,
    alert_notifier: AlertNotificationService,
) -> None:
    if context.turn_journal is not None:
        await asyncio.to_thread(context.turn_journal.recover_interrupted)
        if (
            context.turn_journal.recovered_unknown_effects
            or context.turn_journal.recovered_crashed_turns
        ):
            context.logger.warning(
                f"Recovered {context.turn_journal.recovered_crashed_turns} interrupted turns and {context.turn_journal.recovered_unknown_effects} unknown effects at service startup."
            )
    if context.settings.observability_enabled:
        context.background_tasks.start("event-loop-lag", telemetry.monitor_event_loop)
    if context.proactive_learning is not None:
        context.background_tasks.start("proactive-feedback", handlers.triggers._proactive_feedback_loop)
    if context.mobile_authorization is not None:
        context.background_tasks.start(
            "mobile-authorization", context.mobile_authorization.run_forever
        )
    if (
        context.subagent_coordinator is not None
        and context.subagent_coordinator.dispatcher is not None
    ):
        context.background_tasks.start(
            "subagent-workflows", context.subagent_coordinator.dispatcher.run_forever
        )
    if context.local_model is not None:
        context.background_tasks.start(
            "local-model-health", context.local_model.run_forever
        )
    if (
        context.settings.alertmanager_url
        and context.settings.alert_notify_group_id > 0
        and context.background_tasks.start("alert-notifier", alert_notifier.run_forever)
    ):
        context.logger.info(
            f"Activity alert QQ notifier started for group {context.settings.alert_notify_group_id}; delivery is {('enabled' if alert_notifier.notifications_enabled else 'disabled')}."
        )
    if (
        context.job_store is not None
        and context.media_library is not None
        and (context.semantic_recall is not None)
    ):
        try:
            _job, created = await asyncio.to_thread(
                context.job_store.enqueue,
                kind="media.index_stickers",
                idempotency_key=f"media.index_stickers:{context.settings.embedding_model}:{context.settings.embedding_dimensions}",
                scope_key="system",
            )
            if created:
                context.logger.info("Scheduled restart-safe sticker search indexing.")
        except (OSError, RuntimeError, DatabaseError) as exc:
            context.logger.warning(
                f"Could not schedule global sticker search indexing: {exc}"
            )
    if context.job_worker is not None and context.background_tasks.start(
        "durable-jobs", context.job_worker.run_forever
    ):
        context.logger.info(
            f"PostgreSQL durable application worker enabled for {', '.join(context.job_worker.registered_kinds) or 'registered jobs'}."
        )
    if context.media_library is not None and context.background_tasks.start(
        "media-library", context.media_library.run_forever
    ):
        context.logger.info(
            f"Durable media worker enabled with vision profile {context.settings.vision_profile}."
        )
    if context.vision_worker is not None and context.background_tasks.start(
        "vision-worker", context.vision_worker.run_forever
    ):
        context.logger.info(
            f"Transient image understanding worker enabled with vision profile {context.settings.vision_profile}."
        )
    if context.cold_archive is not None and context.background_tasks.start(
        "cold-archive", context.cold_archive.run_forever
    ):
        context.logger.info(
            "Automatic cold archive enabled; h610 remains the hot cache."
        )
    if context.reminder_store is not None and context.background_tasks.start(
        "reminders", handlers.delivery._reminder_loop
    ):
        context.logger.info("Persistent reminder scheduler enabled.")
    if context.delivery_store is not None and context.background_tasks.start(
        "delivery", handlers.delivery._delivery_loop
    ):
        context.logger.info("Durable outbound delivery worker enabled.")
    if (
        context.bridge_manager is not None
        and context.bridge_manager.matrix is not None
        and context.background_tasks.start(
            "matrix-sync", handlers.delivery._matrix_sync_loop
        )
    ):
        context.logger.info("Matrix durable sync bridge enabled.")
    if (
        context.bridge_manager is not None
        and context.bridge_manager.imessage is not None
        and context.bridge_manager.imessage_poll_enabled
        and context.background_tasks.start(
            "imessage-sync", handlers.delivery._imessage_sync_loop
        )
    ):
        context.logger.info("iMessage configured-chat polling enabled.")
    if context.semantic_recall is not None and context.background_tasks.start(
        "semantic-index", handlers.triggers._semantic_index_loop
    ):
        context.logger.info("PostgreSQL/pgvector semantic recall worker enabled.")
    if context.historian_service is not None and context.background_tasks.start(
        "historian", handlers.triggers._historian_loop
    ):
        context.logger.info("Model-backed episode Historian enabled.")
    if context.dream_service is not None and context.background_tasks.start(
        "memory-dream", handlers.triggers._dream_loop
    ):
        context.logger.info(
            f"Nightly memory dream pass enabled at {context.settings.dream_hour:02d}:00."
        )
