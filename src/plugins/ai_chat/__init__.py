from __future__ import annotations

import asyncio

from src.bot_storage import DatabaseError

from nonebot import get_app, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor

from .alert_notifier import AlertNotificationService
from .adapters import OneBotIngestAdapter
from .bootstrap import register_http_surfaces
from .config import settings
from .context_pipeline import ReferenceResolver
from .deepseek import DeepSeekConfigError, configure_llm_runtime
from .observability import observed_ai_turn
from .onebot_codec import decode_onebot_message
from .proactive import ProactiveCheckGate
from .paths import CACHE_DIR, PROJECT_ROOT, STATE_DIR
from .runtime import build_app_context
from .handler_services import HandlerServices
from .application import ChatTurnResult
from .stickers import ai_reply_message
from .voice import VoiceError
from .video_analysis import DeepVideoAnalysisError, DeepVideoAnalyzer
from . import matchers
from .handler_constants import BOT_VERSION
from .mobile_authorization import handle_approval_event, redact_approval_event_logs
redact_approval_event_logs()
proactive_check_gate = ProactiveCheckGate()

app_context = build_app_context(
    settings,
    state_dir=STATE_DIR,
    cache_dir=CACHE_DIR,
    project_root=PROJECT_ROOT,
    logger=logger,
    historian_generator=lambda candidate: _generate_historian(candidate),
    dream_generator=lambda scope_key, entries, evidence: _generate_dream(
        scope_key,
        list(entries),
        evidence,
    ),
    evidence_provider=lambda entry: _dream_evidence(entry),
)
configure_llm_runtime(app_context.model_catalog, app_context.llm_gateway)

# Legacy exports preserve imports. Services use the injected AppContext, not these aliases.
memory = app_context.memory
group_context = app_context.group_context
long_term_memory = app_context.long_term_memory
running_tasks = app_context.running_tasks
user_profiles = app_context.user_profiles
model_preferences = app_context.model_preferences
reasoning_preferences = app_context.reasoning_preferences
model_profiles = app_context.model_catalog
model_gateway = app_context.llm_gateway
message_ledger = app_context.message_ledger
context_store = app_context.context_store
topic_graph_store = app_context.topic_graph_store
pin_store = app_context.pin_store
self_source = app_context.self_source
skill_registry = app_context.skill_registry
reminder_store = app_context.reminder_store
delivery_store = app_context.delivery_store
job_store = app_context.job_store
job_worker = app_context.job_worker
subagent_store = app_context.subagent_store
subagent_coordinator = app_context.subagent_coordinator
bridge_router = app_context.bridge_router
mirror_state = app_context.mirror_state
bridge_manager = app_context.bridge_manager
usage_store = app_context.usage_store
semantic_recall = app_context.semantic_recall
semantic_index_state = app_context.semantic_index_state
maintenance_state = app_context.maintenance_state
historian_service = app_context.historian_service
dream_service = app_context.dream_service
turn_journal = app_context.turn_journal
recent_images = app_context.recent_images
recent_voices = app_context.recent_voices
recent_videos = app_context.recent_videos
reference_resolver = ReferenceResolver(graph_store=topic_graph_store)
sandbox_manager = app_context.sandbox_manager
browser_manager = app_context.browser_manager
rich_renderer = app_context.rich_renderer
media_library = app_context.media_library
source_store = app_context.source_store
alert_store = app_context.alert_store
vision_worker = app_context.vision_worker
video_analyzer: DeepVideoAnalyzer | None = None
if (
    settings.video_deep_enabled
    and source_store is not None
    and vision_worker is not None
):
    try:
        video_analyzer = DeepVideoAnalyzer(
            source_store,
            vision_worker,
            whisper_model_path=settings.video_whisper_model_path,
            frame_count=settings.video_frame_count,
            max_download_bytes=settings.video_max_download_bytes,
            max_duration_seconds=settings.video_max_duration_seconds,
            timeout_seconds=settings.video_timeout_seconds,
            whisper_threads=settings.video_whisper_threads,
            cache_seconds=settings.video_cache_seconds,
        )
    except DeepVideoAnalysisError as exc:
        logger.warning(f"Deep video analysis is unavailable: {exc}")
cold_archive = app_context.cold_archive
background_tasks = app_context.background_tasks
alert_notifier = AlertNotificationService(
    alertmanager_url=settings.alertmanager_url,
    group_id=settings.alert_notify_group_id,
    check_seconds=settings.alert_notify_check_seconds,
    state_path=app_context.state_dir / "alert-notifier.json",
    logger=logger,
    history_store=app_context.alert_store,
    enabled_provider=lambda: app_context.alert_preferences.effective_enabled(
        settings.alert_notify_enabled
    ),
)
BOT_STARTED_AT = app_context.started_at
driver = get_driver()


def _is_group_enabled(group_id: int) -> bool:
    return handlers.group_enabled(group_id)


def _is_group_vision_auto_describe_enabled(group_id: int) -> bool:
    return handlers.auto_describe_enabled(group_id)


@event_preprocessor
async def ignore_disabled_group_event(bot: Bot, event: MessageEvent) -> None:
    if await handle_approval_event(app_context.mobile_authorization, bot, event):
        raise IgnoredException("Mobile authorization handled outside conversation history")
    if (
        isinstance(event, GroupMessageEvent)
        and not _is_group_enabled(event.group_id)
    ):
        raise IgnoredException("QQ group is disabled for this bot")

register_http_surfaces(
    get_app(),
    app_context,
    settings=settings,
    version=BOT_VERSION,
    logger=logger,
)


TrackedAIResult = ChatTurnResult


handlers = HandlerServices(app_context, video_analyzer=video_analyzer)
from .qq_action_authorization import register_qq_executors
register_qq_executors(handlers)
handlers.reference_resolver = reference_resolver
handlers.proactive_gate = proactive_check_gate

_format_elapsed = handlers.commands._format_elapsed
_task_status_text = handlers.commands._task_status_text
_usage_text = handlers.commands._usage_text
_memory_scopes = handlers.commands._memory_scopes
_memory_provenance = handlers.commands._memory_provenance
_memory_scope_keys = handlers.commands._memory_scope_keys
_current_long_term_memory = handlers.commands._current_long_term_memory
_memory_entry_payload = handlers.commands._memory_entry_payload
_canonical_message_id = handlers.commands._canonical_message_id
_reminder_id = handlers.commands._reminder_id
_parse_reminder_due_at = handlers.commands._parse_reminder_due_at
_reminder_payload = handlers.commands._reminder_payload
_pin_target_message_id = handlers.commands._pin_target_message_id
_looks_like_secret = handlers.commands._looks_like_secret
_can_edit_group_memory = handlers.commands._can_edit_group_memory
_memory_label = handlers.commands._memory_label
_find_visible_memory = handlers.commands._find_visible_memory
_direct_web_search = handlers.commands._direct_web_search
_resolve_ocr_sources = handlers.commands._resolve_ocr_sources
_resolve_voice_message_id = handlers.commands._resolve_voice_message_id
_finish_image_ocr = handlers.commands._finish_image_ocr
_finish_voice_transcription = handlers.commands._finish_voice_transcription
handle_ai = handlers.commands.handle_ai
handle_web_search = handlers.commands.handle_web_search
handle_image_ocr = handlers.commands.handle_image_ocr
handle_voice_answer = handlers.commands.handle_voice_answer
handle_voice_transcription = handlers.commands.handle_voice_transcription
handle_model_command = handlers.commands.handle_model_command
handle_effort_command = handlers.commands.handle_effort_command
_shell_owner = handlers.commands._shell_owner
_format_shell_result = handlers.commands._format_shell_result
handle_shell_command = handlers.commands.handle_shell_command
handle_memory_command = handlers.commands.handle_memory_command
_ack_control_command = handlers.commands._ack_control_command
handle_control_command = handlers.commands.handle_control_command
handle_pin_command = handlers.commands.handle_pin_command
handle_unpin_command = handlers.commands.handle_unpin_command
handle_pins_command = handlers.commands.handle_pins_command
handle_task_status = handlers.commands.handle_task_status
handle_usage_command = handlers.commands.handle_usage_command
handle_task_stop = handlers.commands.handle_task_stop
handle_mention_ai = handlers.commands.handle_mention_ai
handle_sticker = handlers.commands.handle_sticker
handle_qq_face = handlers.commands.handle_qq_face
handle_sticker_status = handlers.commands.handle_sticker_status
handle_ai_reset = handlers.commands.handle_ai_reset
handle_clear_data = handlers.commands.handle_clear_data
handle_canonical_ingest = handlers.ingest.handle_canonical_ingest
handle_group_activity = handlers.ingest.handle_group_activity
handle_image_auto_description = handlers.ingest.handle_image_auto_description
handle_group_context_recorder = handlers.ingest.handle_group_context_recorder
_generate_proactive_reply = handlers.triggers._generate_proactive_reply
_semantic_documents = handlers.triggers._semantic_documents
_semantic_index_once = handlers.triggers._semantic_index_once
_semantic_index_loop = handlers.triggers._semantic_index_loop
_record_background_usage = handlers.triggers._record_background_usage
_generate_historian = handlers.triggers._generate_historian
_dream_evidence = handlers.triggers._dream_evidence
_generate_dream = handlers.triggers._generate_dream
_historian_loop = handlers.triggers._historian_loop
_dream_loop = handlers.triggers._dream_loop
handle_proactive_chat = handlers.triggers.handle_proactive_chat
_conversation_scope = handlers.chat._conversation_scope
_image_cache_key = handlers.chat._image_cache_key
_indexed_image_sources = handlers.chat._indexed_image_sources
_refresh_vision_source_url = handlers.chat._refresh_vision_source_url
_voice_cache_key = handlers.chat._voice_cache_key
_video_cache_key = handlers.chat._video_cache_key
_has_available_ocr_image = handlers.chat._has_available_ocr_image
_has_available_voice = handlers.chat._has_available_voice
_sender_name = handlers.chat._sender_name
_sender_label = handlers.chat._sender_label
_render_message_text = handlers.chat._render_message_text
_current_group_context = handlers.chat._current_group_context
_current_user_identity = handlers.chat._current_user_identity
_current_turn_context = handlers.chat._current_turn_context
_reply_target_turn = handlers.chat._reply_target_turn
_drain_task_feedback = handlers.chat._drain_task_feedback
_record_turn_loop_event = handlers.chat._record_turn_loop_event
_conversation_id = handlers.chat._conversation_id
_group_default_model_preference = handlers.chat._group_default_model_preference
_group_member_reasoning_preference = handlers.chat._group_member_reasoning_preference
_preferred_model_profile = handlers.chat._preferred_model_profile
_background_model_profile = handlers.chat._background_model_profile
_running_tasks_for_event = handlers.chat._running_tasks_for_event
_group_turn_context_plan = handlers.chat._group_turn_context_plan
_record_turn_trigger = handlers.chat._record_turn_trigger
_build_chat_orchestrator = handlers.chat._build_chat_orchestrator
_run_tracked_ai = handlers.chat._run_tracked_ai
_private_vision_required = handlers.tools._private_vision_required
_video_analysis_required = handlers.tools._video_analysis_required
_alert_query_required = handlers.tools._alert_query_required
_alert_tools_allowed = handlers.tools._alert_tools_allowed
_resolve_video_reference = handlers.tools._resolve_video_reference
_ask_ai = handlers.tools._ask_ai
_is_napcat_send_timeout = handlers.replies._is_napcat_send_timeout
_make_retry_text = handlers.replies._make_retry_text
_reply_target_segments = handlers.replies._reply_target_segments
_reply_message = handlers.replies._reply_message
_planned_chunk_message = handlers.replies._planned_chunk_message
_render_planned_chunk_message = handlers.replies._render_planned_chunk_message
_reaction_target_message_id = handlers.replies._reaction_target_message_id
_set_message_reaction = handlers.replies._set_message_reaction
_make_retry_message = handlers.replies._make_retry_message
_finish_safely = handlers.replies._finish_safely
_finish_sticker = handlers.replies._finish_sticker
_finish_qq_face = handlers.replies._finish_qq_face
_finish_tracked_ai = handlers.replies._finish_tracked_ai
_journal_reply_text = handlers.replies._journal_reply_text
_sent_message_id = handlers.replies._sent_message_id
_deliver_reminder = handlers.delivery._deliver_reminder
_reminder_loop = handlers.delivery._reminder_loop
_deliver_onebot_outbox = handlers.delivery._deliver_onebot_outbox
_delivery_loop = handlers.delivery._delivery_loop
_matrix_sync_loop = handlers.delivery._matrix_sync_loop
_imessage_sync_loop = handlers.delivery._imessage_sync_loop

handlers.chat._run_tracked_ai = observed_ai_turn(handlers.chat._run_tracked_ai)
_run_tracked_ai = handlers.chat._run_tracked_ai

if vision_worker is not None:
    vision_worker.set_source_resolver(_refresh_vision_source_url)


onebot_ingest_adapter = OneBotIngestAdapter(
    group_enabled=_is_group_enabled, canonical_scope=_conversation_scope,
    image_cache_key=_image_cache_key, voice_cache_key=_voice_cache_key,
    video_cache_key=_video_cache_key,
    ocr_max_images=settings.ocr_max_images, logger=logger,
    message_ledger=message_ledger, delivery_store=delivery_store,
    bridge_router=bridge_router, mirror_state=mirror_state,
    bridge_manager=bridge_manager, media_library=media_library,
    source_store=source_store, user_profiles=user_profiles,
    recent_images=recent_images, recent_voices=recent_voices,
    recent_videos=recent_videos,
)

handlers.ingest_adapter = onebot_ingest_adapter

@driver.on_startup
async def start_background_tasks() -> None:
    if turn_journal is not None:
        await asyncio.to_thread(turn_journal.recover_interrupted)
        if turn_journal.recovered_unknown_effects or turn_journal.recovered_crashed_turns:
            logger.warning(
                f"Recovered {turn_journal.recovered_crashed_turns} interrupted turns and "
                f"{turn_journal.recovered_unknown_effects} unknown effects at service startup."
            )
    if app_context.mobile_authorization is not None:
        background_tasks.start("mobile-authorization", app_context.mobile_authorization.run_forever)
    if subagent_coordinator is not None and subagent_coordinator.dispatcher is not None:
        background_tasks.start("subagent-workflows", subagent_coordinator.dispatcher.run_forever)
    if app_context.local_model is not None:
        background_tasks.start("local-model-health", app_context.local_model.run_forever)
    if (
        settings.alertmanager_url
        and settings.alert_notify_group_id > 0
        and background_tasks.start("alert-notifier", alert_notifier.run_forever)
    ):
        logger.info(
            "Activity alert QQ notifier started for group "
            f"{settings.alert_notify_group_id}; delivery is "
            f"{'enabled' if alert_notifier.notifications_enabled else 'disabled'}."
        )
    if (
        job_store is not None
        and media_library is not None
        and semantic_recall is not None
    ):
        try:
            _job, created = await asyncio.to_thread(
                job_store.enqueue,
                kind="media.index_stickers",
                idempotency_key=(
                    "media.index_stickers:"
                    f"{settings.embedding_model}:{settings.embedding_dimensions}"
                ),
                scope_key="system",
            )
            if created:
                logger.info("Scheduled restart-safe sticker search indexing.")
        except (OSError, RuntimeError, DatabaseError) as exc:
            logger.warning(f"Could not schedule global sticker search indexing: {exc}")
    if job_worker is not None and background_tasks.start(
        "durable-jobs",
        job_worker.run_forever,
    ):
        logger.info(
            "PostgreSQL durable application worker enabled for "
            f"{', '.join(job_worker.registered_kinds) or 'registered jobs'}."
        )
    if media_library is not None and background_tasks.start(
        "media-library",
        media_library.run_forever,
    ):
        logger.info(
            "Durable media worker enabled with vision profile "
            f"{settings.vision_profile}."
        )
    if vision_worker is not None and background_tasks.start(
        "vision-worker",
        vision_worker.run_forever,
    ):
        logger.info(
            "Transient image understanding worker enabled with vision profile "
            f"{settings.vision_profile}."
        )
    if cold_archive is not None and background_tasks.start(
        "cold-archive",
        cold_archive.run_forever,
    ):
        logger.info(
            "Automatic cold archive enabled; h610 remains the hot cache."
        )
    if reminder_store is not None and background_tasks.start(
        "reminders",
        _reminder_loop,
    ):
        logger.info("Persistent reminder scheduler enabled.")
    if delivery_store is not None and background_tasks.start(
        "delivery",
        _delivery_loop,
    ):
        logger.info("Durable outbound delivery worker enabled.")
    if (
        bridge_manager is not None
        and bridge_manager.matrix is not None
        and background_tasks.start("matrix-sync", _matrix_sync_loop)
    ):
        logger.info("Matrix durable sync bridge enabled.")
    if (
        bridge_manager is not None
        and bridge_manager.imessage is not None
        and bridge_manager.imessage_poll_enabled
        and background_tasks.start("imessage-sync", _imessage_sync_loop)
    ):
        logger.info("iMessage configured-chat polling enabled.")
    if semantic_recall is not None and background_tasks.start(
        "semantic-index",
        _semantic_index_loop,
    ):
        logger.info("PostgreSQL/pgvector semantic recall worker enabled.")
    if historian_service is not None and background_tasks.start(
        "historian",
        _historian_loop,
    ):
        logger.info("Model-backed episode Historian enabled.")
    if dream_service is not None and background_tasks.start(
        "memory-dream",
        _dream_loop,
    ):
        logger.info(
            f"Nightly memory dream pass enabled at {settings.dream_hour:02d}:00."
        )


@driver.on_shutdown
async def shutdown_app_context() -> None:
    await app_context.shutdown()



for _matcher, _handler in (
    (matchers.ai, handle_ai),
    (matchers.web_search, handle_web_search),
    (matchers.image_ocr, handle_image_ocr),
    (matchers.voice_answer, handle_voice_answer),
    (matchers.voice_transcription, handle_voice_transcription),
    (matchers.model_command, handle_model_command),
    (matchers.memory_command, handle_memory_command),
    (matchers.control_command, handle_control_command),
    (matchers.effort_command, handle_effort_command),
    (matchers.shell_command, handle_shell_command),
    (matchers.pin_command, handle_pin_command),
    (matchers.unpin_command, handle_unpin_command),
    (matchers.pins_command, handle_pins_command),
    (matchers.task_status, handle_task_status),
    (matchers.usage_command, handle_usage_command),
    (matchers.task_stop, handle_task_stop),
    (matchers.mention_ai, handle_mention_ai),
    (matchers.canonical_ingest_tracker, handle_canonical_ingest),
    (matchers.group_activity_tracker, handle_group_activity),
    (matchers.image_auto_description, handle_image_auto_description),
    (matchers.proactive_chat, handle_proactive_chat),
    (matchers.sticker, handle_sticker),
    (matchers.qq_face, handle_qq_face),
    (matchers.sticker_status, handle_sticker_status),
    (matchers.group_context_recorder, handle_group_context_recorder),
    (matchers.ai_reset, handle_ai_reset),
    (matchers.clear_data, handle_clear_data),
):
    _matcher.handle()(_handler)
