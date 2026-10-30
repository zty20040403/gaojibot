from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from collections.abc import Mapping
from functools import partial

if TYPE_CHECKING:
    from .conversation_scope import ConversationScope


def _get_int(name: str, default: int, *, environ: Mapping[str, str] | None = None) -> int:
    value = (os.environ if environ is None else environ).get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_float(name: str, default: float, *, environ: Mapping[str, str] | None = None) -> float:
    value = (os.environ if environ is None else environ).get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_resource_limit(name: str, default: int, minimum: int, maximum: int, *, environ: Mapping[str, str] | None = None) -> int:
    raw = (os.environ if environ is None else environ).get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _get_bool(name: str, default: bool, *, environ: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if environ is None else environ).get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "enabled"}


def _get_group_ids(name: str, *, environ: Mapping[str, str] | None = None) -> set[int]:
    raw = (os.environ if environ is None else environ).get(name, "").strip()
    if not raw:
        return set()

    group_ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            group_ids.add(int(item))
        except ValueError:
            continue
    return group_ids


def _get_csv(name: str, *, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in (os.environ if environ is None else environ).get(name, "").split(",")
        if item.strip()
    )


def _get_group_model_profiles(name: str, *, environ: Mapping[str, str] | None = None) -> dict[int, str]:
    raw = (os.environ if environ is None else environ).get(name, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be a JSON object")

    profiles: dict[int, str] = {}
    for raw_group_id, raw_profile in payload.items():
        try:
            group_id = int(str(raw_group_id).strip())
        except ValueError as exc:
            raise ValueError(
                f"{name} contains an invalid QQ group id: {raw_group_id!r}"
            ) from exc
        profile = str(raw_profile).strip()
        if group_id <= 0 or not profile:
            raise ValueError(
                f"{name} requires positive group ids and non-empty profile names"
            )
        profiles[group_id] = profile
    return profiles


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_thinking: str
    model_default_profile: str
    model_simple_chat_profile: str
    local_model_profile: str
    local_model_probe_interval_seconds: int
    local_model_probe_timeout_seconds: int
    qwen_control_url: str
    qwen_control_token: str
    model_profiles_json: str
    model_fallback_enabled: bool
    model_circuit_failure_threshold: int
    model_circuit_cooldown_seconds: int
    model_circuit_long_cooldown_seconds: int
    group_model_profiles: dict[int, str]
    system_prompt: str
    max_context_turns: int
    group_context_messages: int
    group_context_chars: int
    ledger_enabled: bool
    context_lifecycle_enabled: bool
    context_input_budget_tokens: int
    context_high_watermark_tokens: int
    context_low_watermark_tokens: int
    context_compartment_target_tokens: int
    context_raw_tail_min_messages: int
    context_max_compartments: int
    turn_journal_enabled: bool
    turn_recent_hours: int
    turn_recent_limit: int
    turn_archive_ttl_days: int
    turn_archive_max_per_scope: int
    turn_archive_max_bytes: int
    turn_event_max_chars: int
    turn_expand_max_chars: int
    turn_replay_enabled: bool
    turn_replay_max_chars: int
    turn_replay_max_segments: int
    memory_max_entries: int
    memory_max_chars: int
    max_input_chars: int
    max_reply_chars: int
    reply_chunk_delay_seconds: float
    stream_enabled: bool
    tool_max_rounds: int
    tool_simple_max_rounds: int
    tool_max_calls_per_round: int
    tool_max_total_calls: int
    tool_max_result_chars: int
    tool_max_context_chars: int
    search_enabled: bool
    search_auto_enabled: bool
    search_max_results: int
    search_timeout_seconds: int
    ocr_enabled: bool
    ocr_max_images: int
    ocr_max_chars: int
    ocr_timeout_seconds: int
    ocr_recent_image_seconds: int
    media_enabled: bool
    media_root: str
    vision_profile: str
    vision_auto_describe: bool
    vision_cache_seconds: int
    vision_cache_entries: int
    media_max_source_bytes: int
    media_max_vision_bytes: int
    media_prepare_threshold_bytes: int
    media_max_edge_pixels: int
    media_timeout_seconds: int
    media_max_attempts: int
    media_lease_seconds: int
    media_batch_size: int
    media_worker_concurrency: int
    video_deep_enabled: bool
    video_whisper_model_path: str
    video_frame_count: int
    video_max_download_bytes: int
    video_max_duration_seconds: int
    video_timeout_seconds: int
    video_whisper_threads: int
    video_cache_seconds: int
    video_recent_seconds: int
    archive_enabled: bool
    archive_root: str
    archive_media_retention_days: int
    archive_delivery_retention_days: int
    archive_delivery_min_bytes: int
    archive_interval_seconds: int
    archive_batch_size: int
    voice_enabled: bool
    voice_provider: str
    voice_name: str
    voice_rate: str
    voice_pitch: str
    voice_local_name: str
    voice_local_rate: int
    voice_max_chars: int
    voice_timeout_seconds: int
    voice_recent_seconds: int
    proactive_enabled: bool
    proactive_adaptive: bool
    proactive_adaptive_groups: set[int]
    proactive_feedback_seconds: int
    proactive_interest_threshold: int
    proactive_gate_percent: int
    proactive_max_checks_per_hour: int
    proactive_classifier_profile: str
    proactive_voice_percent: int
    proactive_max_reply_chars: int
    reminders_enabled: bool
    reminder_check_seconds: int
    reminder_max_per_scope: int
    routines_enabled: bool
    routine_check_seconds: int
    routine_max_per_owner: int
    routine_timeout_seconds: int
    projects_enabled: bool
    project_instance_id: str
    project_idle_stop_minutes: int
    project_hibernate_days: int
    project_archive_days: int
    project_version_keep_recent: int
    outbox_enabled: bool
    outbox_check_seconds: int
    outbox_lease_seconds: int
    outbox_max_attempts: int
    durable_jobs_enabled: bool
    durable_job_poll_seconds: float
    durable_job_lease_seconds: int
    durable_job_max_attempts: int
    durable_job_concurrency: int
    subagents_enabled: bool
    subagent_entry_enabled: bool
    subagent_max_steps: int
    subagent_max_parallelism: int
    subagent_max_tool_rounds: int
    subagent_timeout_seconds: int
    subagent_retention_seconds: int
    subagent_profiles_json: str
    quota_enabled: bool
    quota_daily_calls: int
    quota_daily_input_tokens: int
    quota_daily_output_tokens: int
    postgres_schema: str
    postgres_pool_min_size: int
    postgres_pool_max_size: int
    postgres_pool_timeout_seconds: int
    postgres_health_check_interval_seconds: float
    postgres_node_names: tuple[str, ...]
    semantic_enabled: bool
    postgres_dsn: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_dimensions: int
    embedding_timeout_seconds: int
    semantic_index_seconds: int
    semantic_batch_size: int
    recall_router_model_enabled: bool
    recall_router_profile: str
    recall_router_timeout_seconds: float
    evidence_guard_enabled: bool
    historian_enabled: bool
    historian_profile: str
    historian_model: str
    historian_check_seconds: int
    historian_max_scopes: int
    historian_idle_seconds: int
    historian_max_attempts: int
    group_wiki_enabled: bool
    group_wiki_profile: str
    group_wiki_hour: int
    group_wiki_timezone: str
    group_wiki_check_seconds: int
    mcp_enabled: bool
    mcp_config_file: str
    dream_enabled: bool
    dream_profile: str
    dream_model: str
    dream_hour: int
    dream_min_entries: int
    dream_check_seconds: int
    admin_enabled: bool
    admin_secret_file: str
    admin_origin: str
    admin_bot_id: str
    admin_path: str
    admin_user_ids: set[int]
    observability_enabled: bool
    metrics_path: str
    prometheus_url: str
    alertmanager_url: str
    alert_notify_enabled: bool
    alert_notify_group_id: int
    alert_notify_check_seconds: int
    cluster_enabled: bool
    cluster_control_url: str
    cluster_control_token_file: str
    cluster_control_timeout_seconds: int
    fleet_allowed_groups: set[int]
    fleet_log_allowed_groups: set[int]
    otel_service_name: str
    otel_exporter_otlp_endpoint: str
    mirror_routes_json: str
    bridge_path: str
    matrix_enabled: bool
    matrix_homeserver: str
    matrix_access_token: str
    matrix_user_id: str
    matrix_appservice_token: str
    matrix_sync_timeout_ms: int
    matrix_sync_retry_seconds: int
    imessage_enabled: bool
    imessage_base_url: str
    imessage_password: str
    imessage_webhook_token: str
    imessage_chat_guid: str
    imessage_bot_handle: str
    imessage_poll_enabled: bool
    imessage_poll_seconds: int
    imessage_send_method: str
    browser_enabled: bool
    browser_timeout_seconds: int
    browser_max_sessions: int
    browser_idle_seconds: int
    browser_executable_path: str
    browser_allow_private_network: bool
    rich_render_enabled: bool
    codesnap_enabled: bool
    codesnap_executable_path: str
    codesnap_config_path: str
    codesnap_font_family: str
    codesnap_theme: str
    codesnap_timeout_seconds: int
    codesnap_cache_entries: int
    sandbox_enabled: bool
    sandbox_backend: str
    sandbox_image: str
    sandbox_vm_image: str
    sandbox_vm_root: str
    sandbox_nix_cache_volume: str
    sandbox_allowed_users: set[int]
    sandbox_max_per_user: int
    sandbox_max_total: int
    sandbox_timeout_seconds: int
    sandbox_max_file_bytes: int
    enabled_groups: set[int]
    disabled_groups: set[int]
    groups_require_allowlist: bool = False
    sandbox_runner_socket: str = ""
    sandbox_runner_token: str = field(default="", repr=False)
    sandbox_allow_all: bool = False
    sandbox_cpus: int = 2
    sandbox_pids_limit: int = 256
    sandbox_memory_mib: int = 8192
    sandbox_network: str = "isolated"
    feedback_positive_emoji_ids: tuple[str, ...] = ()
    feedback_negative_emoji_ids: tuple[str, ...] = ()
    tool_intent_enabled: bool = True
    tool_intent_timeout_seconds: float = 3.0
    search_provider: str = "scrape"
    search_api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        environment = dict(os.environ if environ is None else environ)
        integer = partial(_get_int, environ=environment)
        floating = partial(_get_float, environ=environment)
        resource_limit = partial(_get_resource_limit, environ=environment)
        boolean = partial(_get_bool, environ=environment)
        group_ids = partial(_get_group_ids, environ=environment)
        csv = partial(_get_csv, environ=environment)
        group_models = partial(_get_group_model_profiles, environ=environment)
        sandbox_max_file_mb = integer("AI_SANDBOX_MAX_FILE_MB", 20)
        intent_timeout = floating("AI_TOOL_INTENT_TIMEOUT_SECONDS", 3.0)
        intent_timeout = min(max(intent_timeout, 0.1), 10.0) if math.isfinite(intent_timeout) else 3.0
        search_provider = environment.get("AI_SEARCH_PROVIDER", "").strip().lower() or "scrape"
        if search_provider not in {"scrape", "tavily", "bocha"}:
            raise ValueError("AI_SEARCH_PROVIDER must be scrape, tavily or bocha")
        return cls(
            tool_intent_enabled=boolean("AI_TOOL_INTENT_ENABLED", True),
            tool_intent_timeout_seconds=intent_timeout,
            feedback_positive_emoji_ids=csv("AI_FEEDBACK_POSITIVE_EMOJI_IDS"),
            feedback_negative_emoji_ids=csv("AI_FEEDBACK_NEGATIVE_EMOJI_IDS"),
            deepseek_api_key=environment.get("DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=environment.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            deepseek_model=environment.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
            deepseek_thinking=environment.get("DEEPSEEK_THINKING", "disabled").strip().lower(),
            model_default_profile=(
                environment.get("AI_MODEL_DEFAULT_PROFILE", "deepseek").strip()
                or "deepseek"
            ),
            model_simple_chat_profile=environment.get(
                "AI_SIMPLE_CHAT_PROFILE",
                "",
            ).strip(),
            model_profiles_json=environment.get("AI_MODEL_PROFILES_JSON", "").strip(),
            local_model_profile=environment.get("AI_LOCAL_MODEL_PROFILE", "qwen-local").strip(),
            local_model_probe_interval_seconds=max(integer("AI_LOCAL_MODEL_PROBE_INTERVAL_SECONDS", 15), 1),
            local_model_probe_timeout_seconds=min(max(integer("AI_LOCAL_MODEL_PROBE_TIMEOUT_SECONDS", 2), 1), 10),
            qwen_control_url=environment.get("AI_QWEN_CONTROL_URL", "").strip(),
            qwen_control_token=environment.get("AI_QWEN_CONTROL_TOKEN", "").strip(),
            model_fallback_enabled=boolean("AI_MODEL_FALLBACK_ENABLED", True),
            model_circuit_failure_threshold=min(
                max(integer("AI_MODEL_CIRCUIT_FAILURE_THRESHOLD", 2), 1),
                10,
            ),
            model_circuit_cooldown_seconds=min(
                max(integer("AI_MODEL_CIRCUIT_COOLDOWN_SECONDS", 120), 10),
                3600,
            ),
            model_circuit_long_cooldown_seconds=min(
                max(
                    integer(
                        "AI_MODEL_CIRCUIT_LONG_COOLDOWN_SECONDS",
                        3600,
                    ),
                    60,
                ),
                86400,
            ),
            group_model_profiles=group_models(
                "AI_GROUP_MODEL_PROFILES_JSON"
            ),
            system_prompt=environment.get(
                "AI_SYSTEM_PROMPT",
                "你是 gaoji，QQ群里的友好群友。回答要简洁、准确、有帮助；不知道就说不知道。",
            ).strip(),
            max_context_turns=integer("AI_MAX_CONTEXT_TURNS", 6),
            group_context_messages=integer("AI_GROUP_CONTEXT_MESSAGES", 40),
            group_context_chars=integer("AI_GROUP_CONTEXT_CHARS", 4000),
            ledger_enabled=boolean("AI_LEDGER_ENABLED", True),
            context_lifecycle_enabled=boolean(
                "AI_CONTEXT_LIFECYCLE_ENABLED",
                True,
            ),
            context_input_budget_tokens=min(
                max(integer("AI_CONTEXT_INPUT_BUDGET_TOKENS", 32768), 1000),
                64000,
            ),
            context_high_watermark_tokens=min(
                max(integer("AI_CONTEXT_HIGH_WATERMARK_TOKENS", 32768), 500),
                64000,
            ),
            context_low_watermark_tokens=min(
                max(integer("AI_CONTEXT_LOW_WATERMARK_TOKENS", 16384), 250),
                32000,
            ),
            context_compartment_target_tokens=min(
                max(
                    integer("AI_CONTEXT_COMPARTMENT_TARGET_TOKENS", 1200),
                    250,
                ),
                8000,
            ),
            context_raw_tail_min_messages=min(
                max(integer("AI_CONTEXT_RAW_TAIL_MIN_MESSAGES", 8), 1),
                100,
            ),
            context_max_compartments=min(
                max(integer("AI_CONTEXT_MAX_COMPARTMENTS", 12), 1),
                50,
            ),
            turn_journal_enabled=boolean("AI_TURN_JOURNAL_ENABLED", True),
            turn_recent_hours=min(
                max(integer("AI_TURN_RECENT_HOURS", 24), 1), 168
            ),
            turn_recent_limit=min(
                max(integer("AI_TURN_RECENT_LIMIT", 5), 1), 20
            ),
            turn_archive_ttl_days=min(
                max(integer("AI_TURN_ARCHIVE_TTL_DAYS", 14), 0), 90
            ),
            turn_archive_max_per_scope=min(
                max(integer("AI_TURN_ARCHIVE_MAX_PER_SCOPE", 50), 0),
                500,
            ),
            turn_archive_max_bytes=min(
                max(integer("AI_TURN_ARCHIVE_MAX_KB", 512), 64),
                4096,
            )
            * 1024,
            turn_event_max_chars=min(
                max(integer("AI_TURN_EVENT_MAX_CHARS", 12000), 1000),
                100000,
            ),
            turn_expand_max_chars=min(
                max(integer("AI_TURN_EXPAND_MAX_CHARS", 10000), 1000),
                50000,
            ),
            turn_replay_enabled=boolean("AI_TURN_REPLAY_ENABLED", True),
            turn_replay_max_chars=min(
                max(integer("AI_TURN_REPLAY_MAX_CHARS", 40000), 4000),
                200000,
            ),
            turn_replay_max_segments=min(
                max(integer("AI_TURN_REPLAY_MAX_SEGMENTS", 3), 1),
                10,
            ),
            memory_max_entries=min(
                max(integer("AI_MEMORY_MAX_ENTRIES", 30), 1), 100
            ),
            memory_max_chars=min(
                max(integer("AI_MEMORY_MAX_CHARS", 300), 50), 1000
            ),
            max_input_chars=integer("AI_MAX_INPUT_CHARS", 1500),
            max_reply_chars=integer("AI_MAX_REPLY_CHARS", 3000),
            reply_chunk_delay_seconds=max(
                floating("AI_REPLY_CHUNK_DELAY_SECONDS", 0.6),
                0.0,
            ),
            stream_enabled=boolean("AI_STREAM_ENABLED", True),
            tool_max_rounds=min(
                max(integer("AI_TOOL_MAX_ROUNDS", 30), 1), 100
            ),
            tool_simple_max_rounds=min(
                max(integer("AI_TOOL_SIMPLE_MAX_ROUNDS", 5), 1), 20
            ),
            tool_max_calls_per_round=min(
                max(integer("AI_TOOL_MAX_CALLS_PER_ROUND", 4), 1), 20
            ),
            tool_max_total_calls=min(
                max(integer("AI_TOOL_MAX_TOTAL_CALLS", 60), 1),
                200,
            ),
            tool_max_result_chars=min(
                max(integer("AI_TOOL_MAX_RESULT_CHARS", 12000), 1000),
                100000,
            ),
            tool_max_context_chars=min(
                max(integer("AI_TOOL_MAX_CONTEXT_CHARS", 60000), 5000),
                500000,
            ),
            search_enabled=boolean("AI_SEARCH_ENABLED", True),
            search_auto_enabled=boolean("AI_SEARCH_AUTO_ENABLED", True),
            search_max_results=integer("AI_SEARCH_MAX_RESULTS", 5),
            search_timeout_seconds=integer("AI_SEARCH_TIMEOUT_SECONDS", 10),
            search_provider=search_provider,
            search_api_key=environment.get("AI_SEARCH_API_KEY", "").strip(),
            ocr_enabled=boolean("AI_OCR_ENABLED", True),
            ocr_max_images=max(integer("AI_OCR_MAX_IMAGES", 2), 1),
            ocr_max_chars=max(integer("AI_OCR_MAX_CHARS", 4000), 200),
            ocr_timeout_seconds=max(
                integer("AI_OCR_TIMEOUT_SECONDS", 30), 5
            ),
            ocr_recent_image_seconds=max(
                integer("AI_OCR_RECENT_IMAGE_SECONDS", 300), 30
            ),
            media_enabled=boolean("AI_MEDIA_ENABLED", False),
            media_root=environment.get("AI_MEDIA_ROOT", "").strip(),
            vision_profile=(
                environment.get("AI_VISION_PROFILE", "gpt-5.6-luna").strip()
                or "gpt-5.6-luna"
            ),
            vision_auto_describe=boolean(
                "AI_VISION_AUTO_DESCRIBE",
                False,
            ),
            vision_cache_seconds=max(
                integer("AI_VISION_CACHE_SECONDS", 600),
                0,
            ),
            vision_cache_entries=min(
                max(integer("AI_VISION_CACHE_ENTRIES", 256), 1),
                2048,
            ),
            media_max_source_bytes=max(
                integer("AI_MEDIA_MAX_SOURCE_MB", 100), 1
            )
            * 1024
            * 1024,
            media_max_vision_bytes=max(
                integer("AI_VISION_MAX_IMAGE_MB", 20), 1
            )
            * 1024
            * 1024,
            media_prepare_threshold_bytes=max(
                integer("AI_MEDIA_PREPARE_THRESHOLD_MB", 1), 0
            )
            * 1024
            * 1024,
            media_max_edge_pixels=min(
                max(integer("AI_MEDIA_MAX_EDGE_PX", 1568), 256), 4096
            ),
            media_timeout_seconds=min(
                max(integer("AI_VISION_TIMEOUT_SECONDS", 180), 5), 600
            ),
            media_max_attempts=min(
                max(integer("AI_MEDIA_MAX_ATTEMPTS", 5), 1), 20
            ),
            media_lease_seconds=min(
                max(integer("AI_MEDIA_LEASE_SECONDS", 240), 30), 1800
            ),
            media_batch_size=min(
                max(integer("AI_MEDIA_BATCH_SIZE", 4), 1), 20
            ),
            media_worker_concurrency=min(
                max(integer("AI_MEDIA_WORKER_CONCURRENCY", 2), 1), 8
            ),
            video_deep_enabled=boolean("AI_VIDEO_DEEP_ENABLED", False),
            video_whisper_model_path=environment.get(
                "AI_VIDEO_WHISPER_MODEL_PATH", ""
            ).strip(),
            video_frame_count=min(
                max(integer("AI_VIDEO_FRAME_COUNT", 12), 4), 12
            ),
            video_max_download_bytes=max(
                integer("AI_VIDEO_MAX_DOWNLOAD_MB", 1024), 10
            )
            * 1024
            * 1024,
            video_max_duration_seconds=max(
                integer("AI_VIDEO_MAX_DURATION_MINUTES", 60), 1
            )
            * 60,
            video_timeout_seconds=min(
                max(integer("AI_VIDEO_TIMEOUT_SECONDS", 1800), 60), 1800
            ),
            video_whisper_threads=min(
                max(integer("AI_VIDEO_WHISPER_THREADS", 8), 1), 32
            ),
            video_cache_seconds=max(
                integer("AI_VIDEO_CACHE_SECONDS", 86400), 60
            ),
            video_recent_seconds=max(
                integer("AI_VIDEO_RECENT_SECONDS", 300), 30
            ),
            archive_enabled=boolean("AI_ARCHIVE_ENABLED", False),
            archive_root=environment.get("AI_ARCHIVE_ROOT", "").strip(),
            archive_media_retention_days=max(
                integer("AI_ARCHIVE_MEDIA_RETENTION_DAYS", 30), 1
            ),
            archive_delivery_retention_days=max(
                integer("AI_ARCHIVE_DELIVERY_RETENTION_DAYS", 7), 1
            ),
            archive_delivery_min_bytes=max(
                integer("AI_ARCHIVE_DELIVERY_MIN_MB", 1), 1
            )
            * 1024
            * 1024,
            archive_interval_seconds=max(
                integer("AI_ARCHIVE_INTERVAL_SECONDS", 60), 15
            ),
            archive_batch_size=min(
                max(integer("AI_ARCHIVE_BATCH_SIZE", 20), 1), 200
            ),
            voice_enabled=boolean("AI_VOICE_ENABLED", True),
            voice_provider=(
                environment.get("AI_VOICE_PROVIDER", "edge").strip().lower()
                or "edge"
            ),
            voice_name=(
                environment.get(
                    "AI_VOICE_NAME",
                    "zh-CN-YunxiaNeural",
                ).strip()
                or "zh-CN-YunxiaNeural"
            ),
            voice_rate=environment.get("AI_VOICE_RATE", "+0%").strip() or "+0%",
            voice_pitch=environment.get("AI_VOICE_PITCH", "+0Hz").strip() or "+0Hz",
            voice_local_name=(
                environment.get("AI_VOICE_LOCAL_NAME", "Tingting").strip()
                or "Tingting"
            ),
            voice_local_rate=min(
                max(integer("AI_VOICE_LOCAL_RATE", 210), 80), 400
            ),
            voice_max_chars=min(
                max(integer("AI_VOICE_MAX_CHARS", 350), 50), 1000
            ),
            voice_timeout_seconds=max(
                integer("AI_VOICE_TIMEOUT_SECONDS", 45), 5
            ),
            voice_recent_seconds=max(
                integer("AI_VOICE_RECENT_SECONDS", 300), 30
            ),
            proactive_enabled=boolean("AI_PROACTIVE_ENABLED", False),
            proactive_adaptive=boolean("AI_PROACTIVE_ADAPTIVE", False),
            proactive_adaptive_groups=group_ids("AI_PROACTIVE_ADAPTIVE_GROUPS"),
            proactive_feedback_seconds=min(max(integer("AI_PROACTIVE_FEEDBACK_SECONDS", 300), 60), 3600),
            proactive_interest_threshold=min(
                max(integer("AI_PROACTIVE_INTEREST_THRESHOLD", 98), 0), 100
            ),
            proactive_gate_percent=min(
                max(integer("AI_PROACTIVE_GATE_PERCENT", 10), 0), 100
            ),
            proactive_max_checks_per_hour=max(
                integer("AI_PROACTIVE_MAX_CHECKS_PER_HOUR", 6), 0
            ),
            proactive_classifier_profile=(
                environment.get("AI_PROACTIVE_CLASSIFIER_PROFILE", "deepseek").strip()
                or "deepseek"
            ),
            proactive_voice_percent=min(
                max(integer("AI_PROACTIVE_VOICE_PERCENT", 60), 0), 100
            ),
            proactive_max_reply_chars=max(
                integer("AI_PROACTIVE_MAX_REPLY_CHARS", 180), 20
            ),
            reminders_enabled=boolean("AI_REMINDERS_ENABLED", True),
            reminder_check_seconds=max(
                integer("AI_REMINDER_CHECK_SECONDS", 20),
                5,
            ),
            reminder_max_per_scope=min(
                max(integer("AI_REMINDER_MAX_PER_SCOPE", 50), 1),
                200,
            ),
            outbox_enabled=boolean("AI_OUTBOX_ENABLED", True),
            routines_enabled=boolean("AI_ROUTINES_ENABLED", False),
            routine_check_seconds=min(max(integer("AI_ROUTINE_CHECK_SECONDS", 15), 5), 300),
            routine_max_per_owner=min(max(integer("AI_ROUTINE_MAX_PER_OWNER", 5), 1), 20),
            routine_timeout_seconds=min(max(integer("AI_ROUTINE_TIMEOUT_SECONDS", 600), 30), 3600),
            projects_enabled=boolean("AI_PROJECTS_ENABLED", False),
            project_instance_id=environment.get("AI_PROJECT_INSTANCE_ID", "").strip() or environment.get("AI_APP_INSTANCE_ID", "").strip(),
            project_idle_stop_minutes=integer("AI_PROJECT_IDLE_STOP_MINUTES", 30),
            project_hibernate_days=integer("AI_PROJECT_HIBERNATE_DAYS", 7),
            project_archive_days=integer("AI_PROJECT_ARCHIVE_DAYS", 60),
            project_version_keep_recent=integer("AI_PROJECT_VERSION_KEEP_RECENT", 10),
            outbox_check_seconds=max(
                integer("AI_OUTBOX_CHECK_SECONDS", 5),
                1,
            ),
            outbox_lease_seconds=max(
                integer("AI_OUTBOX_LEASE_SECONDS", 90),
                10,
            ),
            outbox_max_attempts=min(
                max(integer("AI_OUTBOX_MAX_ATTEMPTS", 5), 1),
                50,
            ),
            durable_jobs_enabled=boolean("AI_DURABLE_JOBS_ENABLED", True),
            durable_job_poll_seconds=max(
                floating("AI_DURABLE_JOB_POLL_SECONDS", 2.0),
                0.1,
            ),
            durable_job_lease_seconds=min(
                max(integer("AI_DURABLE_JOB_LEASE_SECONDS", 300), 10),
                3600,
            ),
            durable_job_max_attempts=min(
                max(integer("AI_DURABLE_JOB_MAX_ATTEMPTS", 3), 1),
                50,
            ),
            durable_job_concurrency=min(
                max(integer("AI_DURABLE_JOB_CONCURRENCY", 2), 1),
                32,
            ),
            subagents_enabled=boolean("AI_SUBAGENTS_ENABLED", True),
            subagent_entry_enabled=boolean("AI_SUBAGENT_ENTRY_ENABLED", True),
            subagent_max_steps=min(
                max(integer("AI_SUBAGENT_MAX_STEPS", 8), 1),
                12,
            ),
            subagent_max_parallelism=min(
                max(integer("AI_SUBAGENT_MAX_PARALLELISM", 3), 1),
                6,
            ),
            subagent_max_tool_rounds=min(
                max(integer("AI_SUBAGENT_MAX_TOOL_ROUNDS", 20), 1),
                32,
            ),
            subagent_timeout_seconds=min(
                max(integer("AI_SUBAGENT_TIMEOUT_SECONDS", 600), 30),
                3600,
            ),
            subagent_retention_seconds=min(
                max(integer("AI_SUBAGENT_RETENTION_SECONDS", 3600), 0),
                3600,
            ),
            subagent_profiles_json=environment.get(
                "AI_SUBAGENT_PROFILES_JSON", "{}"
            ).strip(),
            quota_enabled=boolean("AI_QUOTA_ENABLED", True),
            quota_daily_calls=max(
                integer("AI_QUOTA_DAILY_CALLS", 0),
                0,
            ),
            quota_daily_input_tokens=max(
                integer("AI_QUOTA_DAILY_INPUT_TOKENS", 0),
                0,
            ),
            quota_daily_output_tokens=max(
                integer("AI_QUOTA_DAILY_OUTPUT_TOKENS", 0),
                0,
            ),
            postgres_schema=(
                environment.get("AI_POSTGRES_SCHEMA", "qq_bot").strip()
                or "qq_bot"
            ),
            postgres_pool_min_size=min(
                max(integer("AI_POSTGRES_POOL_MIN_SIZE", 1), 1),
                20,
            ),
            postgres_pool_max_size=min(
                max(integer("AI_POSTGRES_POOL_MAX_SIZE", 24), 1),
                100,
            ),
            postgres_pool_timeout_seconds=max(
                integer("AI_POSTGRES_POOL_TIMEOUT_SECONDS", 10),
                1,
            ),
            postgres_health_check_interval_seconds=max(
                floating("AI_POSTGRES_HEALTH_CHECK_INTERVAL_SECONDS", 5.0),
                0.5,
            ),
            postgres_node_names=csv("AI_POSTGRES_NODE_NAMES"),
            semantic_enabled=boolean("AI_SEMANTIC_ENABLED", False),
            postgres_dsn=environment.get("AI_POSTGRES_DSN", "").strip(),
            embedding_base_url=environment.get(
                "AI_EMBEDDING_BASE_URL",
                "http://127.0.0.1:11434/v1",
            ).strip(),
            embedding_api_key=environment.get(
                "AI_EMBEDDING_API_KEY",
                "",
            ).strip(),
            embedding_model=environment.get(
                "AI_EMBEDDING_MODEL",
                "bge-m3",
            ).strip(),
            embedding_dimensions=min(
                max(integer("AI_EMBEDDING_DIMENSIONS", 1024), 8),
                8192,
            ),
            embedding_timeout_seconds=max(
                integer("AI_EMBEDDING_TIMEOUT_SECONDS", 30),
                5,
            ),
            semantic_index_seconds=max(
                integer("AI_SEMANTIC_INDEX_SECONDS", 60),
                10,
            ),
            semantic_batch_size=min(
                max(integer("AI_SEMANTIC_BATCH_SIZE", 32), 1),
                100,
            ),
            recall_router_model_enabled=boolean(
                "AI_RECALL_ROUTER_MODEL_ENABLED",
                True,
            ),
            recall_router_profile=environment.get(
                "AI_RECALL_ROUTER_PROFILE",
                "deepseek",
            ).strip(),
            recall_router_timeout_seconds=min(
                max(floating("AI_RECALL_ROUTER_TIMEOUT_SECONDS", 8.0), 1.0),
                30.0,
            ),
            evidence_guard_enabled=boolean(
                "AI_EVIDENCE_GUARD_ENABLED",
                True,
            ),
            historian_enabled=boolean("AI_HISTORIAN_ENABLED", False),
            historian_profile=environment.get("AI_HISTORIAN_PROFILE", "").strip(),
            historian_model=environment.get("AI_HISTORIAN_MODEL", "").strip(),
            historian_check_seconds=max(
                integer("AI_HISTORIAN_CHECK_SECONDS", 60),
                15,
            ),
            historian_max_scopes=min(
                max(integer("AI_HISTORIAN_MAX_SCOPES", 20), 1),
                200,
            ),
            historian_idle_seconds=max(
                integer("AI_HISTORIAN_IDLE_SECONDS", 600),
                60,
            ),
            historian_max_attempts=min(
                max(integer("AI_HISTORIAN_MAX_ATTEMPTS", 4), 2),
                10,
            ),
            group_wiki_enabled=boolean("AI_GROUP_WIKI_ENABLED", False),
            group_wiki_profile=environment.get("AI_GROUP_WIKI_PROFILE", "").strip(),
            group_wiki_hour=resource_limit("AI_GROUP_WIKI_HOUR", 3, 0, 23),
            group_wiki_timezone=environment.get("AI_GROUP_WIKI_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai",
            group_wiki_check_seconds=resource_limit("AI_GROUP_WIKI_CHECK_SECONDS", 300, 30, 3600),
            mcp_enabled=boolean("AI_MCP_ENABLED", False),
            mcp_config_file=environment.get("AI_MCP_CONFIG_FILE", "").strip(),
            dream_enabled=boolean("AI_DREAM_ENABLED", False),
            dream_profile=environment.get("AI_DREAM_PROFILE", "").strip(),
            dream_model=environment.get("AI_DREAM_MODEL", "").strip(),
            dream_hour=min(
                max(integer("AI_DREAM_HOUR", 4), 0),
                23,
            ),
            dream_min_entries=min(
                max(integer("AI_DREAM_MIN_ENTRIES", 15), 2),
                100,
            ),
            dream_check_seconds=max(
                integer("AI_DREAM_CHECK_SECONDS", 300),
                30,
            ),
            admin_enabled=boolean("AI_ADMIN_ENABLED", False),
            admin_secret_file=environment.get("AI_ADMIN_SECRET_FILE", "").strip(),
            admin_origin=environment.get("AI_ADMIN_ORIGIN", "").strip().rstrip("/"),
            admin_bot_id=environment.get("AI_ADMIN_BOT_ID", "").strip(),
            admin_path=(
                environment.get("AI_ADMIN_PATH", "/bot-admin").strip()
                or "/bot-admin"
            ),
            admin_user_ids=group_ids("AI_ADMIN_USER_IDS"),
            observability_enabled=boolean("AI_OBSERVABILITY_ENABLED", True),
            metrics_path=(
                environment.get("AI_METRICS_PATH", "/metrics").strip() or "/metrics"
            ),
            prometheus_url=environment.get("AI_PROMETHEUS_URL", "").strip().rstrip("/"),
            alertmanager_url=environment.get("AI_ALERTMANAGER_URL", "").strip().rstrip("/"),
            alert_notify_enabled=boolean("AI_ALERT_NOTIFY_ENABLED", False),
            alert_notify_group_id=max(
                integer("AI_ALERT_NOTIFY_GROUP_ID", 0),
                0,
            ),
            alert_notify_check_seconds=max(
                integer("AI_ALERT_NOTIFY_CHECK_SECONDS", 30),
                10,
            ),
            cluster_enabled=boolean("AI_CLUSTER_ENABLED", False),
            cluster_control_url=environment.get(
                "AI_CLUSTER_CONTROL_URL", "http://127.0.0.1:8091"
            ).strip().rstrip("/"),
            cluster_control_token_file=environment.get(
                "AI_CLUSTER_CONTROL_TOKEN_FILE", ""
            ).strip(),
            cluster_control_timeout_seconds=min(
                max(integer("AI_CLUSTER_CONTROL_TIMEOUT_SECONDS", 12), 1),
                30,
            ),
            fleet_allowed_groups=group_ids("AI_FLEET_ALLOWED_GROUPS"),
            fleet_log_allowed_groups=group_ids(
                "AI_FLEET_LOG_ALLOWED_GROUPS"
            ),
            otel_service_name=(
                environment.get("OTEL_SERVICE_NAME", "gaoji").strip()
                or "gaoji"
            ),
            otel_exporter_otlp_endpoint=environment.get(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "",
            ).strip(),
            mirror_routes_json=environment.get("AI_MIRROR_ROUTES_JSON", "").strip(),
            bridge_path=(
                environment.get("AI_BRIDGE_PATH", "/bot-bridge").strip()
                or "/bot-bridge"
            ),
            matrix_enabled=boolean("AI_MATRIX_ENABLED", False),
            matrix_homeserver=environment.get("AI_MATRIX_HOMESERVER", "").strip(),
            matrix_access_token=environment.get("AI_MATRIX_ACCESS_TOKEN", "").strip(),
            matrix_user_id=environment.get("AI_MATRIX_USER_ID", "").strip(),
            matrix_appservice_token=environment.get(
                "AI_MATRIX_APPSERVICE_TOKEN", ""
            ).strip(),
            matrix_sync_timeout_ms=min(
                max(integer("AI_MATRIX_SYNC_TIMEOUT_MS", 30000), 1000),
                120000,
            ),
            matrix_sync_retry_seconds=min(
                max(integer("AI_MATRIX_SYNC_RETRY_SECONDS", 5), 1),
                300,
            ),
            imessage_enabled=boolean("AI_IMESSAGE_ENABLED", False),
            imessage_base_url=environment.get("AI_IMESSAGE_BASE_URL", "").strip(),
            imessage_password=environment.get("AI_IMESSAGE_PASSWORD", "").strip(),
            imessage_webhook_token=environment.get(
                "AI_IMESSAGE_WEBHOOK_TOKEN", ""
            ).strip(),
            imessage_chat_guid=environment.get("AI_IMESSAGE_CHAT_GUID", "").strip(),
            imessage_bot_handle=environment.get("AI_IMESSAGE_BOT_HANDLE", "").strip(),
            imessage_poll_enabled=boolean("AI_IMESSAGE_POLL_ENABLED", False),
            imessage_poll_seconds=min(max(integer("AI_IMESSAGE_POLL_SECONDS", 5), 2), 300),
            imessage_send_method=environment.get("AI_IMESSAGE_SEND_METHOD", "private-api").strip(),
            browser_enabled=boolean("AI_BROWSER_ENABLED", False),
            browser_timeout_seconds=min(
                max(integer("AI_BROWSER_TIMEOUT_SECONDS", 30), 5), 120
            ),
            browser_max_sessions=min(
                max(integer("AI_BROWSER_MAX_SESSIONS", 3), 1), 10
            ),
            browser_idle_seconds=max(
                integer("AI_BROWSER_IDLE_SECONDS", 1800), 60
            ),
            browser_executable_path=environment.get(
                "AI_BROWSER_EXECUTABLE_PATH", ""
            ).strip(),
            browser_allow_private_network=boolean(
                "AI_BROWSER_ALLOW_PRIVATE_NETWORK", False
            ),
            rich_render_enabled=boolean("AI_RICH_RENDER_ENABLED", True),
            codesnap_enabled=boolean("AI_CODESNAP_ENABLED", True),
            codesnap_executable_path=environment.get(
                "AI_CODESNAP_EXECUTABLE_PATH", "codesnap"
            ).strip(),
            codesnap_config_path=environment.get(
                "AI_CODESNAP_CONFIG_PATH", ""
            ).strip(),
            codesnap_font_family=environment.get(
                "AI_CODESNAP_FONT_FAMILY", "Sarasa Mono SC"
            ).strip(),
            codesnap_theme=environment.get(
                "AI_CODESNAP_THEME", "candy"
            ).strip(),
            codesnap_timeout_seconds=min(
                max(integer("AI_CODESNAP_TIMEOUT_SECONDS", 12), 3), 60
            ),
            codesnap_cache_entries=min(
                max(integer("AI_CODESNAP_CACHE_ENTRIES", 256), 16), 2048
            ),
            sandbox_enabled=boolean("AI_SANDBOX_ENABLED", False),
            sandbox_allow_all=boolean("AI_SANDBOX_ALLOW_ALL", False),
            sandbox_cpus=resource_limit("AI_SANDBOX_CPUS", 2, 1, 16),
            sandbox_pids_limit=resource_limit("AI_SANDBOX_PIDS_LIMIT", 256, 32, 4096),
            sandbox_memory_mib=resource_limit("AI_SANDBOX_MEMORY_MIB", 8192, 256, 8192),
            sandbox_network=environment.get("AI_SANDBOX_NETWORK", "isolated").strip().lower(),
            sandbox_backend=environment.get("AI_SANDBOX_BACKEND", "oci").strip().lower(),
            sandbox_image=environment.get("AI_SANDBOX_IMAGE", "").strip(),
            sandbox_vm_image=environment.get("AI_SANDBOX_VM_IMAGE", "").strip(),
            sandbox_vm_root=environment.get("AI_SANDBOX_VM_ROOT", "").strip(),
            sandbox_nix_cache_volume=(
                environment.get("AI_SANDBOX_NIX_CACHE_VOLUME", "gaoji-nix-v2").strip()
                or "gaoji-nix-v2"
            ),
            sandbox_allowed_users=group_ids(
                "AI_SANDBOX_ALLOWED_USERS"
            ),
            sandbox_max_per_user=min(
                max(integer("AI_SANDBOX_MAX_PER_USER", 2), 1), 5
            ),
            sandbox_max_total=min(
                max(integer("AI_SANDBOX_MAX_TOTAL", 8), 1), 20
            ),
            sandbox_timeout_seconds=min(
                max(integer("AI_SANDBOX_TIMEOUT_SECONDS", 120), 5), 300
            ),
            sandbox_max_file_bytes=(
                0
                if sandbox_max_file_mb <= 0
                else min(sandbox_max_file_mb, 100) * 1024 * 1024
            ),
            enabled_groups=group_ids("AI_ENABLED_GROUPS"),
            disabled_groups=group_ids("AI_DISABLED_GROUPS"),
            groups_require_allowlist=boolean("AI_GROUPS_REQUIRE_ALLOWLIST", False),
            sandbox_runner_socket=environment.get("AI_SANDBOX_RUNNER_SOCKET", "").strip(),
            sandbox_runner_token=environment.get("AI_SANDBOX_RUNNER_TOKEN", "").strip(),
        )

    def is_group_enabled(self, group_id: int) -> bool:
        return group_id not in self.disabled_groups and (
            (not self.enabled_groups and not self.groups_require_allowlist)
            or group_id in self.enabled_groups
        )

    def is_scope_enabled(self, scope: ConversationScope) -> bool:
        """Deployment default only; official admission additionally requires binding."""
        if scope.kind != "group":
            raise ValueError("group settings require a group scope")
        if scope.platform != "onebot-v11":
            return False
        return self.is_group_enabled(int(scope.native_conversation_id))

    def model_profile_for_scope(self, scope: ConversationScope) -> str | None:
        if scope.kind != "group" or scope.platform != "onebot-v11":
            return None
        return self.group_model_profiles.get(int(scope.native_conversation_id))

    def is_sandbox_user_allowed(self, user_id: int) -> bool:
        return self.sandbox_enabled and (
            self.sandbox_allow_all
            or user_id in self.sandbox_allowed_users
        )


settings = Settings.from_env()
