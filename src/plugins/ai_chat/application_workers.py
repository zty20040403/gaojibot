"""Shared owned workers for the official application."""

from __future__ import annotations

import asyncio

from src.bot_storage import DatabaseError

from .observability import telemetry
from .runtime import AppContext


async def start_background_tasks(context: AppContext, *, maintenance) -> None:
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
    if context.local_model is not None:
        context.background_tasks.start(
            "local-model-health", context.local_model.run_forever
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
    if context.cold_archive is not None and context.background_tasks.start(
        "cold-archive", context.cold_archive.run_forever
    ):
        context.logger.info("Automatic cold archive enabled.")
    if context.conversations is not None and context.background_tasks.start(
        "nightly-maintenance", maintenance._nightly_loop
    ):
        context.logger.info("Nightly memory tidy and request-log pruning enabled.")
